"""从已有大秘境榜单引用和战斗事实汇总角色消耗品。"""
from datetime import timedelta
from urllib.parse import quote

from botend.constants.wow import DUNGEON_CN
from botend.models import SpecDungeonRanking
from botend.raiderio_identity import normalized_name
from botend.services.player_raiderio_activity import _slug
from botend.services.wcl_combatant_store import load_facts, match_combatant, weapon_consumables
from botend.services.wcl_consumables import statistics
from botend.wow_i18n import cn_dungeon_from_slug


RECENT_FIGHTS = 10
# 游戏计时与日志起点可能存在偏差；同时要求副本、层数和毫秒用时一致。
START_TIME_TOLERANCE = timedelta(minutes=2)
FIGHT_FIELDS = ('report_code', 'fight_id', 'class_name', 'spec_name', 'character_name', 'realm',
                'fight_started_at', 'dungeon_id', 'dungeon_name', 'keystone_level', 'clear_time')


def _recent_fights(rankings):
    seen_fights, seen_runs, recent = set(), set(), []
    rows = rankings.order_by('-fight_started_at', '-pk').values(*FIGHT_FIELDS)
    for row in rows.iterator(chunk_size=2 * RECENT_FIGHTS):
        fight = (row['report_code'], row['fight_id'])
        run = (row['dungeon_id'], row['keystone_level'], row['fight_started_at'], row['clear_time'])
        if fight in seen_fights or run in seen_runs:
            continue
        seen_fights.add(fight)
        if row['keystone_level'] and row['clear_time']:
            seen_runs.add(run)
        recent.append(row)
        if len(recent) == RECENT_FIGHTS:
            break
    return recent


def _highest_run(activity):
    runs = (run for key in ('best_runs', 'recent_runs') for run in activity.get(key, {}).get('rows', ()))
    timed = (run for run in runs if run['timed'] is True and run['level'] > 0)
    # 未知完成时间的场次只能按层数比较，同层优先有日期的场次。
    return max(timed, key=lambda run: (run['level'], run['completed_at'] is not None, run['completed_at']),
               default=None)


def _match_run(rankings, run):
    if not run or not run['completed_at'] or not run['clear_time_ms']:
        return None
    estimated_start = run['completed_at'] - timedelta(milliseconds=run['clear_time_ms'])
    candidates = rankings.filter(
        keystone_level=run['level'], clear_time=run['clear_time_ms'],
        fight_started_at__range=(estimated_start - START_TIME_TOLERANCE, estimated_start + START_TIME_TOLERANCE),
    ).order_by('pk').values(*FIGHT_FIELDS)
    matches = {}
    for row in candidates:
        name = row['dungeon_name']
        localized_name = cn_dungeon_from_slug(_slug(name), DUNGEON_CN.get(name, name))
        if normalized_name(localized_name) == normalized_name(run['dungeon_name']):
            matches.setdefault((row['report_code'], row['fight_id']), row)
    return next(iter(matches.values())) if len(matches) == 1 else None


def _fight_usage(rows):
    identities = load_facts(rows, identities_only=True)
    actors = {}
    for row in rows:
        key = (row['report_code'], row['fight_id'])
        event = match_combatant(identities.get(key, ()), row, row['class_name'], row['spec_name'])
        if event:
            actors[key] = (int(event['sourceID']),)
    facts = load_facts(rows, actor_ids_by_fight=actors, include_consumables=True)
    result = {}
    for key, events in facts.items():
        event = events[0]
        usage = dict(event['consumables']) if isinstance(event.get('consumables'), dict) else {}
        weapon = weapon_consumables(event.get('gear'))
        if weapon is not None:
            usage['weapon'] = weapon
        result[key] = dict(report_code=key[0], fight_id=key[1], wcl_actor_id=event['sourceID'], consumables=usage)
    return result


def project_player_consumables(player, activity):
    """只读本地战斗缓存。最高限时以赛季内已收录的角色成绩为准。"""
    region = (player.region or '').strip()
    realm = (player.realm or '').strip()
    name = (player.character_name or '').strip()
    if not all((player.season_id, region, realm, name)) or 'anonymous' in (realm.casefold(), name.casefold()):
        return None
    rankings = SpecDungeonRanking.objects.filter(
        season_id=player.season_id, region__in={region.lower(), region.upper()}, realm__iexact=realm,
        character_name__iexact=name, class_name=player.class_name, fight_id__gt=0, fight_started_at__isnull=False,
    ).exclude(report_code__isnull=True).exclude(report_code='')
    recent = _recent_fights(rankings)
    recent_keys = [(row['report_code'], row['fight_id']) for row in recent]
    highest = _highest_run(activity)
    highest_fight = _match_run(rankings, highest)
    highest_key = (highest_fight['report_code'], highest_fight['fight_id']) if highest_fight else None
    targets = recent if highest_key is None or highest_key in recent_keys else [*recent, highest_fight]
    usage = _fight_usage(targets)

    recent_stats = statistics(usage[key] for key in recent_keys if key in usage)
    highest_usage = usage.get(highest_key)
    highest_stats = statistics([highest_usage]) if highest_usage else []
    return {
        'recent': {
            'sample_size': len(recent),
            'groups': recent_stats if any(group['samples'] for group in recent_stats) else [],
        },
        'highest': {
            'run': highest,
            'log_url': (f'https://www.warcraftlogs.com/reports/{quote(highest_key[0], safe="")}'
                        f'#fight={highest_key[1]}&source={highest_usage["wcl_actor_id"]}') if highest_usage else '',
            'groups': highest_stats if any(group['samples'] for group in highest_stats) else [],
        },
    }
