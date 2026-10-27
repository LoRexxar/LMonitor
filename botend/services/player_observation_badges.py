"""榜单高置信标签：后台生成小投影，公开请求只批量核对发布和来源状态。"""
from datetime import timedelta

from django.db.models import F, Func, JSONField, Value
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from botend.models import PlayerObservationAnalysis, PlayerSpecTopPlayer, RaiderIOCharacterActivity, RaiderIORunRoster
from botend.services.player_raiderio_activity import player_identity
from botend.raiderio_identity import activity_identity_digest


class _SourceValue(Func):
    """只读取指定来源元数据，统一 SQLite 和 MySQL 的 JSON 标量返回。"""
    function = 'JSON_EXTRACT'
    output_field = JSONField()

    def as_sqlite(self, compiler, connection, **extra_context):
        sql, params = self.as_sql(compiler, connection, **extra_context)
        return f'JSON_QUOTE({sql})', params


def _date(value):
    try:
        result = parse_datetime(value) if isinstance(value, str) else value
        return result if result and timezone.is_aware(result) else None
    except (ValueError, TypeError, AttributeError):
        return None


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
        # 证据等级单独表达，不在标签内重复长说明。
        label = item.get('value', '').split(' · ')[0]
        if label:
            badges.append({'code': item['code'], 'label': label, 'confidence': '高',
                'scope': strength.get('scope') or '仅限近期公开样本，不代表长期身份或组队动机。'})
    return badges


def attach_badge_snapshots(players):
    """仅供后台榜单构建使用；复用详情页完整的冻结校验和来源核验。"""
    from botend.services.player_observation import project_player_observation
    objects = PlayerSpecTopPlayer.objects.in_bulk([p['id'] for p in players])
    for item in players:
        item.pop('observation_badges', None)
        item.pop('_observation_guard', None)
        player = objects.get(item['id'])
        if not player:
            continue
        identity = player_identity(player)
        if not identity:
            continue
        publication = PlayerObservationAnalysis.objects.filter(activity__season_id=player.season_id,
            activity__identity_digest=activity_identity_digest(**identity), status='ready', is_published=True,
            last_error='', manual_json={}).exclude(review_status='rejected').values(
                'id', 'revision', 'evidence_hash', 'activity_id', 'activity__identity_digest').first()
        if not publication:
            continue
        view = project_player_observation(player)
        badges = high_confidence_badges(view)
        if not badges or view['analysis_id'] != publication['id']:
            continue
        stamps = view['source_stamps']
        dates = [_date((stamps.get('module_fetched_at') or {}).get('mythic_plus_recent_runs')
                       or stamps.get('activity_fetched_at'))]
        dates += [stamp for ref in stamps.get('scores', []) if (stamp := _date(ref.get('fetched_at')))
                  and timezone.now() - timedelta(hours=48) <= stamp <= timezone.now()]
        # 过期后自动不展示，等待后台重新核验，而非让旧快照永久占据榜单。
        expires = min(value for value in dates if value) + timedelta(hours=48) if any(dates) else timezone.now()
        if expires <= timezone.now():
            continue
        item['observation_badges'] = badges
        item['_observation_guard'] = {**publication, 'season_id': player.season_id,
            'expires_at': expires.isoformat(), 'source_stamps': stamps,
            'player_identity': {field: getattr(player, field) for field in ('region', 'realm', 'character_name', 'profile_url')}}


def public_badge_players(players, class_name, spec_name):
    """旧快照不做额外查询；新投影批量核验，不读取证据正文或现场调用分析器。"""
    result = [{key: value for key, value in p.items() if key not in ('_observation_guard', 'observation_badges')}
              for p in players]
    candidates = [(i, p, p.get('_observation_guard')) for i, p in enumerate(players)
                  if p.get('observation_badges') and isinstance(p.get('_observation_guard'), dict)]
    if not candidates:
        return result
    now = timezone.now()
    candidates = [(i, p, g) for i, p, g in candidates if (expiry := _date(g.get('expires_at'))) and now < expiry]
    if not candidates:
        return result
    # 身份必须仍是同赛季、同角色，不能跟随复用的排名行或代表专精发生漂移。
    from botend.services.spec_stats_service import SpecStatsService
    season = SpecStatsService.get_active_season()
    if not season:
        return result
    identities = {}
    for player in PlayerSpecTopPlayer.objects.filter(pk__in=[p['id'] for _, p, _ in candidates],
            season_id=season.pk, class_name=class_name, spec_name=spec_name, rank__gte=1, rank__lte=20).only(
                'id', 'region', 'realm', 'character_name', 'profile_url'):
        identities[player.pk] = {field: getattr(player, field) for field in ('region', 'realm', 'character_name', 'profile_url')}
    publications = {row['id']: row for row in PlayerObservationAnalysis.objects.filter(
        pk__in=[g['id'] for _, _, g in candidates], status='ready', is_published=True, last_error='',
        manual_json={}).exclude(review_status='rejected').values('id', 'revision', 'evidence_hash',
            'activity_id', 'activity__identity_digest', 'activity__fetched_at', 'activity__source_updated_at',
            'activity__module_fetched_at', 'activity__last_error', 'activity__status')}
    sources = [g.get('source_stamps') or {} for _, _, g in candidates]
    roster_ids = {r['run_id'] for s in sources for r in s.get('rosters', [])}
    rosters = {r['run_id']: r for r in RaiderIORunRoster.objects.filter(
        rio_season=season.rio_season, run_id__in=roster_ids).values('run_id', 'fetched_at', 'roster_visible', 'status', 'last_error')}
    activity_ids = {r['activity_id'] for s in sources for kind in ('scores', 'histories') for r in s.get(kind, [])}
    dungeon_ids = {r['dungeon_id'] for s in sources for r in s.get('histories', [])}
    annotations = {}
    for did in dungeon_ids:
        for field in ('fetched_at', 'status', 'private', 'last_error'):
            # 副本 ID 是对象键；Django 的数字 KeyTransform 会误当数组下标。
            annotations[f'h{did}_{field}'] = _SourceValue(F('payload_json'),
                Value(f'$."mythic_plus_scored_history"."{int(did)}"."{field}"'))
    activities = {r['id']: r for r in RaiderIOCharacterActivity.objects.filter(
        pk__in=activity_ids, season_id=season.pk).annotate(**annotations).values(
            'id', 'scored_runs_fetched_at', *annotations)}
    for index, player, guard in candidates:
        row = publications.get(guard.get('id'))
        if not row or guard.get('season_id') != season.pk or identities.get(player['id']) != guard.get('player_identity'):
            continue
        if any(row[key] != guard.get(key) for key in ('revision', 'evidence_hash', 'activity_id', 'activity__identity_digest')):
            continue
        stamps = guard['source_stamps']
        if (row['activity__last_error'] or row['activity__status'] != 'ready'
                or row['activity__fetched_at'] != _date(stamps.get('activity_fetched_at'))
                or row['activity__source_updated_at'] != _date(stamps.get('source_updated_at'))
                or row['activity__module_fetched_at'] != stamps.get('module_fetched_at', {})):
            continue
        valid = True
        for ref in stamps.get('rosters', []):
            source = rosters.get(ref['run_id'], {})
            valid &= bool(source.get('roster_visible') and source.get('status') == 'ready'
                and not source.get('last_error') and source.get('fetched_at') == _date(ref['fetched_at']))
        for ref in stamps.get('scores', []):
            valid &= activities.get(ref['activity_id'], {}).get('scored_runs_fetched_at') == _date(ref['fetched_at'])
        for ref in stamps.get('histories', []):
            source, prefix = activities.get(ref['activity_id'], {}), f"h{ref['dungeon_id']}_"
            valid &= bool(_date(source.get(prefix + 'fetched_at')) == _date(ref['fetched_at'])
                and source.get(prefix + 'status') not in ('private', 'error', 'fetching')
                and not source.get(prefix + 'private') and not source.get(prefix + 'last_error'))
        if valid:
            result[index]['observation_badges'] = [b for b in player['observation_badges']
                if isinstance(b, dict) and b.get('confidence') == '高' and b.get('label')]
    return result
