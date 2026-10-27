"""Editorial management of the ONE central character analysis.

The representative player FK is navigation metadata, not an identity key. All
writes lock the central row; AI evidence/results/leases are never editable here.
Public readers must separately apply publication and AI freshness gates.
"""
import hashlib
import json
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.core.validators import URLValidator
from django.db import IntegrityError, transaction
from django.db.models import BooleanField, Case, Count, Func, JSONField, Max, OuterRef, Q, Subquery, Value, When
from django.db.models.fields.json import KeyTransform
from django.utils import timezone

from botend.constants.wow import CLASS_CN, SPEC_CN, canonical_class_spec

from botend.models import PlayerObservationAnalysis, PlayerSpecTopPlayer, RaiderIOCharacterActivity, SeasonMeta
from botend.raiderio_identity import activity_identity_digest
from botend.services.player_raiderio_activity import player_identity

PLAYER_FIELDS = ('id', 'character_name', 'region', 'realm', 'class_name', 'spec_name', 'season_id')
REVIEW_STATES = ('pending', 'approved', 'rejected')
EDIT_FIELDS = {'manual', 'review_status', 'is_published', 'review_note'}
URL_VALIDATOR = URLValidator(schemes=['http', 'https'])


class AnalysisConflict(Exception):
    def __init__(self, message='分析已存在或已被修改，请刷新后重试。', existing_id=None):
        super().__init__(message)
        self.existing_id = existing_id
        self.analysis_id = existing_id


def _object(value, fields, name):
    if not isinstance(value, dict) or set(value) - set(fields):
        raise ValidationError({name: '必须为对象，且不能包含未知字段。'})


def _text(value, name, maximum, *, required=False):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValidationError({name: f'必须为不超过 {maximum} 字符的文本。'})
    return value  # Plain text; never mark_safe, strip markup, or render rich HTML.


def _evidence(value):
    if not isinstance(value, list) or len(value) > 50:
        raise ValidationError({'evidence': '证据必须为数组，最多50条。'})
    result = []
    for item in value:
        _object(item, {'text', 'url'}, 'evidence')
        text = _text(item.get('text', ''), 'evidence.text', 10000, required=True)
        url = _text(item.get('url', ''), 'evidence.url', 2000)
        if url:
            URL_VALIDATOR(url)
            if urlsplit(url).scheme.lower() not in ('http', 'https'):
                raise ValidationError({'url': '仅支持http/https链接。'})
        result.append({'text': text, 'url': url})
    return result


def normalize_manual(value):
    if value is None:
        return {}
    _object(value, {'tags', 'conclusion', 'evidence'}, 'manual')
    tags = value.get('tags', [])
    if not isinstance(tags, list) or len(tags) > 20:
        raise ValidationError({'tags': '标签必须为数组，最多20条。'})
    cleaned = []
    for tag in tags:
        _object(tag, {'label', 'value', 'reason', 'evidence'}, 'tags')
        cleaned.append({
            'label': _text(tag.get('label', ''), 'label', 200, required=True),
            'value': _text(tag.get('value', ''), 'value', 500, required=True),
            'reason': _text(tag.get('reason', ''), 'reason', 10000),
            'evidence': _evidence(tag.get('evidence', [])),
        })
    conclusion = _text(value.get('conclusion', ''), 'conclusion', 20000)
    evidence = _evidence(value.get('evidence', []))
    return {'tags': cleaned, 'conclusion': conclusion, 'evidence': evidence} if cleaned or conclusion.strip() or evidence else {}


def source_kind(row):
    # List projections deliberately defer the potentially huge manual document.
    has_manual = row.summary_has_manual if hasattr(row, 'summary_has_manual') else bool(row.manual_json)
    if has_manual:
        return 'manual'
    metadata = row.result_json.get('metadata', {}) if isinstance(row.result_json, dict) else {}
    return 'local_deterministic' if metadata.get('source_kind') == 'local_deterministic' else 'ai'


def _date(value):
    return value.isoformat() if value else None


def _season_name(season_id):
    # PlayerSpecTopPlayer stores an integer, not a ForeignKey to SeasonMeta.
    return Subquery(SeasonMeta.objects.filter(pk=OuterRef(season_id)).values('season_name')[:1])


def _player(player, season_name=None):
    if not player:
        return None
    pair = canonical_class_spec(player.class_name, player.spec_name)
    class_name, spec_name = pair or (player.class_name, player.spec_name)
    if season_name is None:
        if hasattr(player, 'display_season_name'):
            season_name = player.display_season_name
        else:  # Single-object create/update responses, never a per-list-row lookup.
            season_name = SeasonMeta.objects.filter(pk=player.season_id).values_list('season_name', flat=True).first()
    return {**{field: getattr(player, field) for field in PLAYER_FIELDS},
            'class_label': CLASS_CN.get(class_name, class_name),
            'spec_label': SPEC_CN.get(spec_name, spec_name) if pair else spec_name,
            'season_name': season_name or ''}


def _base_query():
    return PlayerObservationAnalysis.objects.select_related('player', 'activity__season').only(
        *[field.name for field in PlayerObservationAnalysis._meta.concrete_fields],
        *['player__' + field for field in PLAYER_FIELDS],
        'activity__id', 'activity__season_id', 'activity__region', 'activity__realm_slug', 'activity__character_name',
        'activity__season__id', 'activity__season__season_name',
    ).annotate(player_season_name=_season_name('player__season_id'))


def _result_hash(row):
    return hashlib.sha256(json.dumps(row.result_json, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def _common(row):
    from botend.services.player_analysis_recheck import state
    from botend.services.player_observation import PROMPT_VERSION
    return {
        'recheck': state(row),
        'id': row.pk, 'revision': row.revision, 'player_id': row.player_id,
        'player': _player(row.player, (row.player_season_name or '') if hasattr(row, 'player_season_name') else None),
        'activity_id': row.activity_id,
        'character': {'character_name': row.activity.character_name, 'region': row.activity.region,
                      'realm': row.activity.realm_slug, 'season_id': row.activity.season_id,
                      'season_name': row.activity.season.season_name},
        'status': row.status, 'review_status': row.review_status,
        'prompt_version': row.prompt_version, 'rules_current': row.prompt_version == PROMPT_VERSION,
        # Compatibility is not validation. Lists must not load frozen evidence.
        'publication_version_supported': row.prompt_version in (7, 8),
        'review_note': row.review_note, 'reviewed_by_id': row.reviewed_by_id,
        'reviewed_at': _date(row.reviewed_at), 'reviewed_evidence_hash': row.reviewed_evidence_hash,
        'reviewed_result_hash': row.reviewed_result_hash,
        'review_matches_ai': bool(row.reviewed_at and row.reviewed_evidence_hash == row.evidence_hash
                                  and row.reviewed_result_hash == _result_hash(row)),
        'is_published': row.is_published, 'source_kind': source_kind(row),
        'generated_at': _date(row.generated_at), 'updated_at': _date(row.updated_at),
    }


def frozen_ai(row):
    """Render identifiers against their OWN frozen publication, never today's facts."""
    from botend.services.player_observation import (
        InvalidResult, _validate_frozen_publication, _project_frozen_investigation,
    )
    audit = {'result_json': row.result_json, 'evidence_json': row.evidence_json}
    empty = {'tags': [], 'conclusion': '', 'evidence': [], 'rendered_investigation': None, 'label_reviews': []}
    if not row.result_json:
        return {**audit, **empty, 'validation_status': 'missing'}
    try:
        result = _validate_frozen_publication(row)
        rendered = _project_frozen_investigation(row, validated=result)
    except (InvalidResult, ValueError, KeyError, TypeError):
        # Invalid/unsupported/mixed publications remain available only for audit.
        return {**audit, **empty, 'validation_status': 'invalid'}
    evidence = row.evidence_json
    labels = {item['code']: item for item in evidence.get('eligible_labels', []) if isinstance(item, dict) and 'code' in item}
    refs = {item['id']: {'text': item.get('text', ''), 'url': item.get('run_url', item.get('url', ''))}
            for item in evidence.get('evidence', []) if isinstance(item, dict) and 'id' in item}
    tags = []
    for item in result.get('observations', []):
        if not isinstance(item, dict):
            continue
        label = labels.get(item.get('code'), {})
        tags.append({'code': item.get('code', ''), 'label': label.get('label', item.get('code', '')),
            'value': item.get('confidence', ''),
            'reason': label.get('reason_options', {}).get(item.get('reason_id'), ''),
            'evidence': [refs[key] for key in item.get('evidence_ids', []) if key in refs]})
    option = evidence.get('conclusion_options', {}).get(result.get('conclusion_id'), {})
    from botend.services.player_observation_resolution import render_label_reviews
    return {'tags': tags, 'conclusion': option.get('text', '') if isinstance(option, dict) else '',
            'evidence': list(refs.values()), **audit,
            'validation_status': 'valid', 'rendered_investigation': rendered,
            'label_reviews': render_label_reviews(evidence)}


def _detail(row):
    ai = frozen_ai(row)
    manual = row.manual_json or None
    return {**_common(row), 'manual': manual, 'ai': ai,
            'effective': manual or {key: ai[key] for key in ('tags', 'conclusion', 'evidence')}}


def get_analysis(id):
    return _detail(_base_query().get(pk=_positive_id(id)))


def _positive_id(value):
    if type(value) is not int or value <= 0:
        raise ValidationError({'id': 'ID必须为正整数。'})
    return value


def _page(params):
    values = []
    for field, default in (('page', 1), ('page_size', 20)):
        value = params.get(field, default)
        if isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit() or int(value) < 1:
            raise ValidationError({field: '必须为正整数。'})
        values.append(int(value))
    return values[0], min(values[1], 50)


def _paginate(query, params, render, *, total=None):
    page, page_size = _page(params)
    total = query.count() if total is None else total
    start = (page - 1) * page_size
    return {'items': [render(row) for row in query[start:start + page_size]],
            'total': total, 'page': page, 'page_size': page_size,
            'total_pages': (total + page_size - 1) // page_size}


def _choice(params, key, allowed):
    value = params.get(key, '')
    if value and value not in allowed:
        raise ValidationError({key: '无效筛选值。'})
    return value


class _ManualTagColumn(Func):
    """Extract scalar tag fields in SQL, never transfer manual reasons/evidence.

    Manual writes normalize every tag with both label and value, so the two
    ordered arrays align. SQLite JSON paths lack MySQL's array wildcard.
    """
    function = 'JSON_EXTRACT'
    output_field = JSONField()

    def __init__(self, key):
        self.key = key
        super().__init__('manual_json', Value(f'$.tags[*].{key}'))

    def as_sqlite(self, compiler, connection, **extra_context):
        document, params = compiler.compile(self.source_expressions[0])
        return (
            '(SELECT JSON_GROUP_ARRAY(JSON_EXTRACT(tag.value, %s)) '
            f"FROM JSON_EACH({document}, '$.tags') tag WHERE tag.type = 'object')",
            [f'$.{self.key}', *params],
        )


def _effective_tags(has_manual, manual_labels, manual_values, labels, observations):
    if has_manual:
        values = manual_values if isinstance(manual_values, list) else []
        tags = [{'label': label, 'value': values[index] if index < len(values) else ''}
                for index, label in enumerate(manual_labels if isinstance(manual_labels, list) else [])]
    else:
        names = {item.get('code'): item.get('label', '') for item in (labels or []) if isinstance(item, dict)}
        tags = [{'label': names.get(item.get('code'), item.get('code', '')), 'value': item.get('confidence', '')}
                for item in (observations or []) if isinstance(item, dict)]
    return [tag for tag in tags if isinstance(tag['label'], str) and tag['label'].strip() and len(tag['label']) <= 200]


class _MatchesTag(Func):
    """在数据库内匹配生效标签，保留人工覆盖与大小写精确匹配。"""
    output_field = BooleanField()

    def __init__(self, tag):
        super().__init__('manual_json', 'evidence_json', 'result_json', Value(tag))

    def _parts(self, compiler):
        return [compiler.compile(value) for value in self.source_expressions]

    def as_sqlite(self, compiler, connection, **extra_context):
        (manual, _), (evidence, _), (result, _), (tag, params) = self._parts(compiler)
        return (f"CASE WHEN {manual} != '{{}}' THEN EXISTS (SELECT 1 FROM JSON_EACH({manual}, '$.tags') t "
                f"WHERE JSON_EXTRACT(t.value, '$.label') = {tag} COLLATE BINARY) ELSE EXISTS "
                f"(SELECT 1 FROM JSON_EACH({result}, '$.observations') o WHERE COALESCE("
                f"(SELECT JSON_EXTRACT(l.value, '$.label') FROM JSON_EACH({evidence}, '$.eligible_labels') l "
                "WHERE JSON_EXTRACT(l.value, '$.code') = JSON_EXTRACT(o.value, '$.code') LIMIT 1), "
                f"JSON_EXTRACT(o.value, '$.code')) = {tag} COLLATE BINARY) END", params * 2)

    def as_mysql(self, compiler, connection, **extra_context):
        (manual, _), (evidence, _), (result, _), (tag, params) = self._parts(compiler)
        return (f"CASE WHEN {manual} != JSON_OBJECT() THEN EXISTS (SELECT 1 FROM JSON_TABLE({manual}, "
                "'$.tags[*]' COLUMNS(label VARCHAR(200) PATH '$.label')) t "
                f"WHERE BINARY t.label = BINARY {tag}) ELSE EXISTS (SELECT 1 FROM JSON_TABLE({result}, "
                "'$.observations[*]' COLUMNS(code VARCHAR(200) PATH '$.code')) o "
                f"LEFT JOIN JSON_TABLE({evidence}, '$.eligible_labels[*]' COLUMNS("
                "code VARCHAR(200) PATH '$.code', label VARCHAR(200) PATH '$.label')) l "
                f"ON BINARY l.code = BINARY o.code WHERE BINARY COALESCE(l.label, o.code) = BINARY {tag}) END", params * 2)


def list_analyses(params):
    # Deliberately do NOT call _detail/frozen_ai/build_evidence on list pages.
    page, page_size = _page(params)
    tag = _text(params.get('tag', ''), 'tag', 200)  # Exact label, not a substring or trimmed alias.
    query = _base_query().defer('evidence_json', 'manual_json').annotate(
        summary_has_manual=Case(When(manual_json={}, then=Value(False)), default=Value(True), output_field=BooleanField()),
        summary_manual_labels=_ManualTagColumn('label'),
        summary_manual_values=_ManualTagColumn('value'),
        summary_strengths=KeyTransform('evidence_strengths', KeyTransform('behavior_review', KeyTransform('metrics', 'evidence_json'))),
        summary_labels=KeyTransform('eligible_labels', 'evidence_json'),
        summary_observations=KeyTransform('observations', 'result_json'))
    q = _text(params.get('q', ''), 'q', 200).strip()
    if q:
        condition = (Q(player__character_name__icontains=q) | Q(player__realm__icontains=q)
                     | Q(activity__character_name__icontains=q) | Q(activity__realm_slug__icontains=q))
        if q.isascii() and q.isdigit():
            condition |= Q(pk=int(q)) | Q(player_id=int(q)) | Q(activity_id=int(q))
        query = query.filter(condition)
    for key, allowed in (('status', ('pending', 'ready', 'error')), ('review_status', REVIEW_STATES)):
        value = _choice(params, key, allowed)
        if value:
            query = query.filter(**{key: value})
    source = _choice(params, 'source', ('manual', 'ai', 'local_deterministic'))
    if source == 'manual':
        query = query.exclude(manual_json={})
    elif source:
        query = query.filter(manual_json={})
        local = Q(result_json__metadata__source_kind='local_deterministic')
        query = query.filter(local) if source == 'local_deterministic' else query.filter(
            Q(result_json__metadata__source_kind__isnull=True) | ~local)
    visibility = _choice(params, 'visibility', ('published', 'hidden'))
    if visibility:
        query = query.filter(is_published=visibility == 'published')

    # 标签菜单与分页分离；同一筛选条件翻页不再扫描全部 JSON。
    options = None
    total = None
    if params.get('include_tag_options', '1') != '0':
        signature = query.aggregate(count=Count('pk'), last_id=Max('pk'), updated=Max('updated_at'))
        if not tag:
            total = signature['count']
        key_data = [q, *[params.get(k, '') for k in ('status', 'review_status', 'source', 'visibility')], signature]
        key = 'player-analysis-tags:' + hashlib.sha256(json.dumps(key_data, default=str, ensure_ascii=False).encode()).hexdigest()
        options = cache.get(key)
        if options is None:
            labels, before = set(), None
            candidates = query.order_by('-id').values('id', 'summary_has_manual',
                'summary_manual_labels', 'summary_labels', 'summary_observations')
            while True:
                batch = list((candidates.filter(pk__lt=before) if before is not None else candidates)[:200])
                for row in batch:
                    labels.update(item['label'] for item in _effective_tags(row['summary_has_manual'],
                        row['summary_manual_labels'], [], row['summary_labels'], row['summary_observations']))
                if len(batch) < 200:
                    break
                before = batch[-1]['id']
            options = sorted(labels)
            cache.set(key, options, 60)
    if tag:
        query = query.annotate(matches_tag=_MatchesTag(tag)).filter(matches_tag=True)

    def summary(row):
        from botend.services.player_observation_resolution import LABELS
        tags = _effective_tags(row.summary_has_manual, row.summary_manual_labels,
            row.summary_manual_values, row.summary_labels, row.summary_observations)
        strengths = row.summary_strengths if isinstance(row.summary_strengths, dict) else {}
        assessments = [] if row.summary_has_manual else [
            {**strength, 'code': code, 'label': LABELS[code], 'strength_label': strength['label']}
            for code, strength in strengths.items() if code in LABELS
            and isinstance(strength, dict) and strength.get('level') in ('excluded', 'unverifiable')]
        return {**_common(row), 'tags': tags, 'assessments': assessments}

    return {**_paginate(query.order_by('-id'), params, summary, total=total),
            **({'tag_options': options} if options is not None else {})}


def list_characters(params):
    query = PlayerSpecTopPlayer.objects.only(*PLAYER_FIELDS).annotate(display_season_name=_season_name('season_id'))
    q = _text(params.get('q', ''), 'q', 200).strip()
    if q:
        condition = Q(character_name__icontains=q) | Q(realm__icontains=q)
        if q.isascii() and q.isdigit():
            condition |= Q(pk=int(q))
        query = query.filter(condition)
    if params.get('season_id'):
        value = params['season_id']
        if not str(value).isascii() or not str(value).isdigit() or int(value) < 1:
            raise ValidationError({'season_id': '必须为正整数。'})
        query = query.filter(season_id=int(value))
    return _paginate(query.order_by('id'), params, _player)


def _apply(row, payload, user):
    if not user or not user.is_authenticated or not user.pk:
        raise ValidationError({'user': '必须提供实际审核用户。'})
    reviewed = 'review_status' in payload
    if 'manual' in payload:
        manual = normalize_manual(payload['manual'])
        if manual != row.manual_json:
            reviewed = True
            row.review_status = 'pending'
            # Changing editorial text cannot inherit approval. Explicitly approving
            # it in this same transaction is permitted; publication is still checked.
            if 'is_published' not in payload and manual:
                row.is_published = False
        row.manual_json = manual
    if 'review_status' in payload:
        if payload['review_status'] not in REVIEW_STATES:
            raise ValidationError({'review_status': '无效审核状态。'})
        row.review_status = payload['review_status']
    if 'is_published' in payload:
        if type(payload['is_published']) is not bool:
            raise ValidationError({'is_published': '必须为布尔值。'})
        row.is_published = payload['is_published']
    if 'review_note' in payload:
        row.review_note = _text(payload['review_note'], 'review_note', 10000)
    if row.is_published and (row.review_status == 'rejected' or (row.manual_json and row.review_status != 'approved')):
        raise ValidationError({'is_published': '人工内容须通过审核；拒绝的记录不可发布。'})
    row.updated_at = timezone.now()
    if reviewed:
        # A note/visibility edit is not approval of a newly generated AI version.
        # Preserve which evidence the actual review concerned until re-reviewed.
        row.reviewed_by = user
        row.reviewed_at = row.updated_at
        row.reviewed_evidence_hash = row.evidence_hash
        row.reviewed_result_hash = _result_hash(row)


@transaction.atomic
def create_analysis(payload, user):
    _object(payload, EDIT_FIELDS | {'player_id'}, 'payload')
    player = PlayerSpecTopPlayer.objects.select_for_update().get(pk=_positive_id(payload.get('player_id')))
    identity = player_identity(player)
    if not identity or identity['character_name'] == 'anonymous':
        raise ValidationError({'player_id': '角色缺少可验证的服务器身份。'})
    if not SeasonMeta.objects.filter(pk=player.season_id).exists():
        raise ValidationError({'player_id': '角色赛季不存在。'})
    digest = activity_identity_digest(**identity)
    # Unique season/digest plus activity lock serializes different spec rows.
    activity, _ = RaiderIOCharacterActivity.objects.get_or_create(
        season_id=player.season_id, identity_digest=digest, defaults=identity)
    activity = RaiderIOCharacterActivity.objects.select_for_update().get(pk=activity.pk)
    existing = PlayerObservationAnalysis.objects.filter(activity=activity).values_list('pk', flat=True).first()
    if existing:
        raise AnalysisConflict(existing_id=existing)
    row = PlayerObservationAnalysis(activity=activity, player=player, is_published=False, review_status='pending')
    _apply(row, payload, user)
    if not row.manual_json:
        raise ValidationError({'manual': '手工新建须包含内容；不能创建冒充AI的空报告。'})
    try:
        with transaction.atomic():
            row.save(force_insert=True)
    except IntegrityError:
        existing = PlayerObservationAnalysis.objects.filter(activity=activity).values_list('pk', flat=True).first()
        if existing:
            raise AnalysisConflict(existing_id=existing) from None
        raise
    return _detail(row)


def _locked(id, payload, allowed):
    _object(payload, allowed | {'revision'}, 'payload')
    revision = payload.get('revision')
    if type(revision) is not int or revision < 0:
        raise ValidationError({'revision': '必须提供非负整数版本。'})
    # Do not select_for_update across nullable joins (PostgreSQL rejects it).
    row = PlayerObservationAnalysis.objects.select_for_update().get(pk=_positive_id(id))
    if row.revision != revision:
        raise AnalysisConflict('版本已变化，请刷新后重试。', existing_id=row.pk)
    return row


@transaction.atomic
def update_analysis(id, payload, user):
    row = _locked(id, payload, EDIT_FIELDS)
    _apply(row, payload, user)
    row.revision += 1
    row.save(update_fields=['manual_json', 'review_status', 'is_published', 'review_note',
        'reviewed_by', 'reviewed_at', 'reviewed_evidence_hash', 'reviewed_result_hash', 'updated_at', 'revision'])
    return _detail(row)


@transaction.atomic
def archive_analysis(id, payload, user):
    row = _locked(id, payload, set())
    _apply(row, {'is_published': False}, user)
    row.revision += 1
    row.save(update_fields=['is_published', 'reviewed_by', 'reviewed_at', 'reviewed_evidence_hash', 'reviewed_result_hash', 'updated_at', 'revision'])
    return _detail(row)
