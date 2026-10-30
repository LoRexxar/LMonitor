"""Top20 标签读取中央 last-good 发布，不以新来源或生成状态撤回旧标签。"""
from urllib.parse import unquote, urlsplit

from django.db.models.fields.json import KeyTransform

from botend.models import PlayerObservationAnalysis, PlayerSpecTopPlayer
from botend.services.player_external_links import REGION_LOCALES
from botend.raiderio_identity import activity_identity_digest, normalized_name


_IDENTITY_FIELDS = ('region', 'realm', 'character_name', 'profile_url')
_SCOPE = '仅限近期公开样本，不代表长期身份或组队动机。'


def high_confidence_badges(view):
    """只采纳公开投影的正向强证据，人工维护不继承旧 AI 标签。"""
    if not view or view.get('status') != 'ready' or view.get('source_kind') == 'manual':
        return []
    badges, seen = [], set()
    for item in view.get('display_judgements', []):
        strength = item.get('evidence_strength') or {}
        if (not item.get('supported') or strength.get('level') != 'strong'
                or strength.get('confidence') != '高' or item.get('code') in seen):
            continue
        seen.add(item['code'])
        label = item.get('value', '').split(' · ')[0]
        if label:
            badges.append({'code': item['code'], 'label': label, 'confidence': '高',
                'scope': strength.get('scope') or _SCOPE})
    return badges


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


def _published_badges(players, class_name=None, spec_name=None):
    """Two bounded batch reads after season lookup; no full evidence/source IO.

    generate_observation writes evidence/result/generated_at atomically only on
    success. Its lease acquisition and _fail change status, not this last-good
    result; recheck jobs likewise change control metadata only. Those fields
    must therefore never be publication visibility gates.
    """
    from botend.services.spec_stats_service import SpecStatsService
    season = SpecStatsService.get_active_season()
    if not season or not players:
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
    ).exclude(review_status='rejected').annotate(
        badge_labels=KeyTransform('eligible_labels', 'evidence_json'),
        badge_observations=KeyTransform('observations', 'result_json'),
        frozen_activity=KeyTransform('activity_id', 'evidence_json'),
        frozen_season=KeyTransform('season', 'evidence_json'),
    ).values('id', 'activity_id', 'activity__identity_digest', 'activity__region',
        'activity__realm_slug', 'activity__character_name', 'badge_labels',
        'badge_observations', 'frozen_activity', 'frozen_season')
    publications = {}
    for row in rows:
        digest = row['activity__identity_digest']
        if (row['frozen_activity'] != row['activity_id'] or row['frozen_season'] != season.rio_season
                or activity_identity_digest(row['activity__region'], row['activity__realm_slug'],
                    row['activity__character_name']) != digest):
            continue
        badges = _frozen_badges(row)
        if badges:
            publications[digest] = (row['id'], badges)
    return {key: (publications[digest], objects[pk])
        for key, (pk, digest) in bound.items() if digest in publications}


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
    rows retract immediately. Snapshot badges are never an authority or fallback.
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
