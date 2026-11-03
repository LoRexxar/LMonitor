"""Top20 统一已确认角色标签；M+ last-good 与独立规则各守自己的门禁。"""
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

from django.db.models import BooleanField, Case, F, Value, When
from django.db.models.fields.json import KeyTransform
from django.utils import timezone

from botend.models import PlayerObservationAnalysis, PlayerSpecTopPlayer, RaiderIOCharacterActivity
from botend.services.player_character_labels import (confirmed_character_badges,
    frozen_badge_judgements)
from botend.services.player_external_links import REGION_LOCALES
from botend.raiderio_identity import activity_identity_digest, normalized_name


_IDENTITY_FIELDS = ('region', 'realm', 'character_name', 'profile_url')
_SCOPE = '仅限近期公开样本，不代表长期身份或组队动机。'


def high_confidence_badges(view, *, include_stale=False):
    """Compatibility wrapper; default stale/manual behavior remains unchanged."""
    if not view or view.get('source_kind') == 'manual':
        return []
    return confirmed_character_badges(view, include_stale=include_stale)


def _frozen_badges(row):
    """Resolve selected IDs against their own frozen labels (as frozen_ai does).

    This is a published-result read model, NOT the generation validator. Older
    publications need not implement today's rule/version contract. Keep their
    literal confidence; when a frozen strength exists it must also be strong.
    Neutral team structure follows _display_judgements' independent semantics.
    """
    from botend.services.player_observation import BEHAVIOR_LABELS
    labels, observations = row['badge_labels'], row['badge_observations']
    if not isinstance(labels, list) or not isinstance(observations, list):
        return []
    selected = {item['code']: item for item in observations
        if isinstance(item, dict) and isinstance(item.get('code'), str)}
    badges, seen = [], set()
    for label in labels:
        if not isinstance(label, dict):
            continue
        code, title = label.get('code'), label.get('label')
        if not isinstance(code, str) or code in seen or not isinstance(title, str) or not title:
            continue
        if code == 'carrying':
            from botend.services.player_observation_carrying_policy import assess_carrying_frequency
            review = row.get('badge_carrying_review') or {
                'carrying_frequency': row.get('badge_carrying_frequency'),
                'structure': {'recent_runs': row.get('badge_recent_runs'),
                              'recent_run_ids': row.get('badge_recent_run_ids')},
                'witnesses': {'carrying': row.get('badge_carrying_witnesses')},
                'evidence_strengths': {'carrying': row.get('badge_carrying_strength')},
            }
            if not assess_carrying_frequency(review)['supported']:
                continue
        strength = label.get('evidence_strength')
        structural = code in ('fixed_team', 'rotating_core')
        if not structural and code not in BEHAVIOR_LABELS:
            continue
        item = selected.get(code)
        if item is not None:
            if item.get('confidence') != '高':
                continue
        elif not structural or not strength:
            continue
        if label.get('confidence_max') != '高':
            continue
        if strength is not None and (not isinstance(strength, dict)
                or strength.get('level') != 'strong' or strength.get('confidence') != '高'):
            continue
        seen.add(code)
        badges.append({'code': code, 'label': title, 'confidence': '高',
            'scope': (strength or {}).get('scope') or _SCOPE})
    return badges


def _profile_identity(player):
    """The canonical profile URL boundary, without external-link realm lookups.

    Mirror build_player_external_links' URL checks; absent/ambiguous URLs do not
    get guessed from display realm names. No armory/WCL directory is needed.
    """
    region = str(player.region or '').lower()
    name = str(player.character_name or '').strip()
    if normalized_name(name) == 'anonymous':
        return None
    try:
        url = urlsplit(str(player.profile_url or '').strip())
        parts = url.path.strip('/').split('/')
        if (region not in REGION_LOCALES or not name or url.scheme != 'https'
                or url.netloc not in ('raider.io', 'www.raider.io') or len(parts) != 4
                or parts[0] != 'characters' or parts[1] != region
                or unquote(parts[3]).casefold() != name.casefold()):
            return None
        realm = unquote(parts[2])
        if not realm or '/' in realm or '\\' in realm or realm.lower() in ('.', '..', 'anonymous'):
            return None
        return {'region': region, 'realm_slug': realm, 'character_name': normalized_name(name)}
    except ValueError:
        return None


def _character_label_views(season, digests):
    """One bounded source/editorial metadata SELECT, including rule-only rows.

    Only the independent raid module and character-ID scalar cross this read
    boundary. Manual badges need two scalar tag columns, not manual evidence.
    The left join is essential: an activity need not have an AI publication.
    """
    from botend.services.player_analysis_management import _ManualTagColumn
    from botend.services.player_raid_observation import project_raid_observation

    def manual_column(key):
        expression = _ManualTagColumn(key)
        sources = expression.get_source_expressions()
        expression.set_source_expressions([F('observation_analysis__manual_json'), *sources[1:]])
        return expression

    rows = RaiderIOCharacterActivity.objects.filter(season_id=season.pk,
        identity_digest__in=digests).annotate(
        raid_state=KeyTransform('raid_first_kills', 'payload_json'),
        scored_character=KeyTransform('characterId', KeyTransform('ui', 'scored_runs_json')),
        has_manual=Case(
            When(observation_analysis__pk__isnull=True, then=Value(False)),
            When(observation_analysis__manual_json={}, then=Value(False)),
            default=Value(True), output_field=BooleanField()),
        manual_labels=manual_column('label'), manual_values=manual_column('value'),
    ).values('id', 'identity_digest', 'region', 'realm_slug', 'character_name',
        'raid_state', 'scored_character', 'has_manual', 'manual_labels', 'manual_values',
        'observation_analysis__id', 'observation_analysis__is_published',
        'observation_analysis__review_status')
    views, now = {}, timezone.now()
    for row in rows:
        identity = {key: row[key] for key in ('region', 'realm_slug', 'character_name')}
        digest = row['identity_digest']
        if activity_identity_digest(**identity) != digest:
            continue  # A corrupted/reused activity row must fail closed too.
        analysis_id = row['observation_analysis__id']
        if analysis_id is not None and (row['observation_analysis__is_published'] is not True
                or row['observation_analysis__review_status'] == 'rejected'):
            continue
        if row['has_manual']:
            if row['observation_analysis__review_status'] != 'approved':
                continue
            labels, values = row['manual_labels'], row['manual_values']
            tags = [{'label': label, 'value': values[index]}
                    for index, label in enumerate(labels if isinstance(labels, list) else [])
                    if isinstance(values, list) and index < len(values)
                    and isinstance(label, str) and label]
            view = {'status': 'ready', 'source_kind': 'manual', 'manual': {'tags': tags}}
        else:
            activity = SimpleNamespace(identity_digest=digest,
                payload_json={'raid_first_kills': row['raid_state']},
                scored_runs_json={'ui': {'characterId': row['scored_character']}})
            view = {'status': 'ready', 'display_judgements': [],
                'raid_judgement': project_raid_observation(activity, season, identity, now=now)}
        views[digest] = (analysis_id, view)
    return views


def _published_badges(players, class_name=None, spec_name=None):
    """Five bounded batch reads after season lookup; no full evidence/source IO.

    Flat roster/history privacy metadata queries share the detail withdrawal
    gate; neither loads full source payloads or rebuilds evidence per player.

    generate_observation writes evidence/result/generated_at atomically only on
    success. Its lease acquisition and _fail change status, not this last-good
    result; recheck jobs likewise change control metadata only. Those fields
    must therefore never be publication visibility gates.
    """
    from botend.services.spec_stats_service import SpecStatsService
    from botend.services.player_observation import (READABLE_PUBLICATION_VERSIONS,
        publication_scope_matches, withdrawn_roster_publications)
    season = SpecStatsService.get_active_season()
    if not season or not season.rio_season or not players:
        return {}
    query = PlayerSpecTopPlayer.objects.filter(pk__in=[p.get('id') for p in players],
        season_id=season.id, rank__gte=1, rank__lte=20)
    if class_name is not None:
        query = query.filter(class_name=class_name, spec_name=spec_name)
    objects = {p.pk: p for p in query.only('id', 'season_id', *_IDENTITY_FIELDS)}
    bound = {}
    for item in players:
        player = objects.get(item.get('id'))
        if player is None:
            continue
        # Old empty snapshots have no guard, but contain the original identity.
        # Never attach a current row's labels to a reused ID in an old snapshot.
        guard = item.get('_observation_guard')
        original = guard.get('player_identity') if isinstance(guard, dict) else item
        if (not isinstance(original, dict)
                or any(original.get(field) != getattr(player, field) for field in _IDENTITY_FIELDS)
                or (isinstance(guard, dict) and guard.get('season_id') != season.id)):
            continue
        identity = _profile_identity(player)
        if identity:
            bound[id(item)] = (player.pk, activity_identity_digest(**identity))
    if not bound:
        return {}
    rows = PlayerObservationAnalysis.objects.filter(
        activity__season_id=season.id, activity__identity_digest__in={digest for _, digest in bound.values()},
        is_published=True, manual_json={}, generated_at__isnull=False,
        prompt_version__in=READABLE_PUBLICATION_VERSIONS,
    ).exclude(review_status='rejected').annotate(
        badge_labels=KeyTransform('eligible_labels', 'evidence_json'),
        badge_observations=KeyTransform('observations', 'result_json'),
        frozen_activity=KeyTransform('activity_id', 'evidence_json'),
        frozen_season=KeyTransform('season', 'evidence_json'),
        frozen_version=KeyTransform('prompt_version', 'evidence_json'),
        frozen_hash=KeyTransform('evidence_hash', 'evidence_json'),
        frozen_rosters=KeyTransform('rosters', KeyTransform('source_stamps', 'evidence_json')),
        frozen_histories=KeyTransform('histories', KeyTransform('source_stamps', 'evidence_json')),
        badge_carrying_frequency=KeyTransform('carrying_frequency', KeyTransform('behavior_review', KeyTransform('metrics', 'evidence_json'))),
        badge_recent_run_ids=KeyTransform('recent_run_ids', KeyTransform('structure', KeyTransform('behavior_review', KeyTransform('metrics', 'evidence_json')))),
        badge_recent_runs=KeyTransform('recent_runs', KeyTransform('structure', KeyTransform('behavior_review', KeyTransform('metrics', 'evidence_json')))),
        badge_carrying_witnesses=KeyTransform('carrying', KeyTransform('witnesses', KeyTransform('behavior_review', KeyTransform('metrics', 'evidence_json')))),
        badge_carrying_strength=KeyTransform('carrying', KeyTransform('evidence_strengths', KeyTransform('behavior_review', KeyTransform('metrics', 'evidence_json')))),
    ).values('id', 'activity_id', 'activity__identity_digest', 'activity__region',
        'activity__realm_slug', 'activity__character_name', 'badge_labels',
        'badge_observations', 'frozen_activity', 'frozen_season', 'frozen_version',
        'frozen_hash', 'evidence_hash', 'prompt_version', 'frozen_rosters', 'frozen_histories',
        'badge_carrying_frequency', 'badge_recent_run_ids', 'badge_recent_runs',
        'badge_carrying_witnesses', 'badge_carrying_strength')
    rows = list(rows)
    withdrawn = withdrawn_roster_publications([
        (row['id'], season.rio_season, row['frozen_rosters'], row['frozen_histories']) for row in rows])
    publications = {}
    for row in rows:
        digest = row['activity__identity_digest']
        if (row['id'] in withdrawn or type(row['frozen_version']) is not int
                or row['frozen_version'] != row['prompt_version']
                or not row['evidence_hash'] or row['frozen_hash'] != row['evidence_hash']
                or not publication_scope_matches(activity_id=row['activity_id'],
                    frozen_activity=row['frozen_activity'], rio_season=season.rio_season,
                    frozen_season=row['frozen_season'], digest=digest,
                    region=row['activity__region'], realm_slug=row['activity__realm_slug'],
                    character_name=row['activity__character_name'])):
            continue
        badges = _frozen_badges(row)
        if badges:
            publications[digest] = (row['id'], badges)
    views = _character_label_views(season, {digest for _, digest in bound.values()})
    merged = {}
    for digest, (analysis_id, view) in views.items():
        publication = publications.get(digest)
        if publication and view.get('source_kind') != 'manual':
            view['display_judgements'] = frozen_badge_judgements(publication[1])
        badges = confirmed_character_badges(view)
        if badges:
            merged[digest] = (analysis_id, badges)
    return {key: (merged[digest], objects[pk])
        for key, (pk, digest) in bound.items() if digest in merged}


def attach_badge_snapshots(players):
    """后台正常榜单刷新也补齐旧空快照；不重建事实或触发生成。"""
    publications = _published_badges(players)
    for item in players:
        item.pop('observation_badges', None)
        item.pop('_observation_guard', None)
        publication = publications.get(id(item))
        if publication:
            (analysis_id, badges), player = publication
            item['observation_badges'] = badges
            item['_observation_guard'] = {'id': analysis_id, 'season_id': player.season_id,
                'player_identity': {field: getattr(player, field) for field in _IDENTITY_FIELDS}}


def public_badge_players(players, class_name, spec_name):
    """Always read the central publication, including for pre-badge snapshots.

    A successful replacement takes effect immediately; hidden/rejected/manual
    draft rows retract immediately. Approved manual tags replace AI/rule tags.
    Snapshot badges are never an authority or fallback.
    """
    publications = _published_badges(players, class_name, spec_name)
    result = []
    for item in players:
        public = {key: value for key, value in item.items()
            if key not in ('_observation_guard', 'observation_badges')}
        publication = publications.get(id(item))
        if publication:
            public['observation_badges'] = publication[0][1]
        result.append(public)
    return result
