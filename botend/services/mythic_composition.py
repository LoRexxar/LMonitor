"""管理员按五个位置查找当前赛季公开限时记录。"""
import re
from collections import Counter
from functools import lru_cache

from django.core.cache import cache
from django.utils.dateparse import parse_datetime

from botend.constants.wow import CLASS_CN, CLASS_COLOR, DUNGEON_CN, SLOT_CN, SPEC_CN, SPEC_IDENTITY_MAP, SPEC_ROLE
from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
from botend.mythic_planner.mdt_route_codec import decode_mdt2_table
from botend.mythic_planner.rio_routes import RIO
from botend.services.player_external_links import LOCALIZED_WCL, _localized_slug
from botend.services.raiderio_run_roster import validate_details

ROLES = ('tank', 'healer', 'dps', 'dps', 'dps')
PAGE_SIZE = 40


def selected_specs(values):
    if not isinstance(values, list) or len(values) != len(ROLES):
        raise ValueError('请选择五个位置，空位可选不限。')
    for role, value in zip(ROLES, values):
        if type(value) is not int or (value != 0 and SPEC_ROLE.get(SPEC_IDENTITY_MAP.get(value)) != role):
            raise ValueError('所选专精与位置不匹配。')
    return Counter(value for value in values if value)


def matches(ids, required):
    return (Counter(SPEC_ROLE.get(SPEC_IDENTITY_MAP.get(i)) for i in ids) == Counter(ROLES)
            and not required - Counter(ids))


def candidates(source, season, required, offset, complete=False):
    params = {'type': 'mythic_plus_runs', 'season[0][eq]': season, 'sort[score]': 'desc',
              'numChests[0][gte]': 1, 'limit': PAGE_SIZE, 'offset': offset}
    if complete:
        params['hasAutoRoute[0][eq]'] = 1
    for index, spec in enumerate(required.elements()):
        params[f'memberSpecIds[{index}][eq]'] = spec
    data = source.rio('/api/search-advanced', **params)
    rows = data['matches']
    ids = []
    for match in rows:
        run = match['data']
        if (run['season'] == season and run['numChests'] > 0 and (not complete or run.get('hasAutoRoute'))
                and matches([m['specId'] for m in run['members']], required)):
            ids.append(run['id'])
    next_offset = offset + len(rows)
    total = data.get('total')
    exact = total.get('value') if isinstance(total, dict) and total.get('relation') == 'eq' else total
    finished = len(rows) < PAGE_SIZE or (type(exact) is int and next_offset >= exact)
    return {'ids': ids, 'next': None if finished else next_offset}


def character_identity(name, region, slug):
    return (name or '').casefold(), (region or '').casefold(), (slug or '').casefold()


@lru_cache(maxsize=1)
def token_provider():
    # 同一进程共用已有加锁的 OAuth 缓存，每次查询的错误与限流状态仍独立。
    return SpecDetailBase(None, None)


def wcl_link(detail, encounter):
    """只接受副本、层数、官方用时、时间窗口和五名角色均相符的公开战斗。"""
    variables = {'encounter': encounter}
    declarations = ['$encounter:Int!']
    fields = []
    identities = []
    for index, member in enumerate(detail['roster']):
        char = member['character']
        realm = char['realm']
        slug = realm['slug']
        if realm.get('locale') in LOCALIZED_WCL and realm.get('altName'):
            slug = _localized_slug(realm['altName']).replace('й', 'и')
        name, region = char['name'], char['region']['slug']
        identities.append({character_identity(name, region, candidate)
                           for candidate in (slug, realm['slug'], realm.get('altSlug') or slug)})
        for prefix, value in [('n', name), ('r', region), ('s', slug)]:
            variables[f'{prefix}{index}'] = value
            declarations.append(f'${prefix}{index}:String!')
        fields.append(f'p{index}:character(name:$n{index},serverRegion:$r{index},serverSlug:$s{index})'
                      '{encounterRankings(encounterID:$encounter,includeOtherPlayers:true,includePrivateLogs:false)}')
    query = 'query(' + ','.join(declarations) + '){characterData{' + ' '.join(fields) + '}}'
    client = SpecDetailBase(None, None)
    client._get_wcl_token = token_provider()._get_wcl_token
    response = client._wcl_graphql(query, variables)
    if response is None:
        raise RuntimeError(f'WCL 暂时无法查询（{client._wcl_last_error}）。')
    end = parse_datetime(detail['completed_at']).timestamp() * 1000
    duration = detail['clear_time_ms']
    for char in response['characterData'].values():
        ranks = (char or {}).get('encounterRankings') or {}
        for row in ranks.get('ranks', []):
            start = row.get('startTime')
            if (row.get('bracketData') != detail['mythic_level'] or row.get('duration') != duration
                    or not start or not end - duration - 60000 <= start < end):
                continue
            players = set()
            for player in row.get('allCharacters') or []:
                server = (player.get('canonical') or {}).get('server') or {}
                players.add(character_identity(player.get('name'), server.get('region'), server.get('slug')))
            if len(players) != 5 or any(not options.intersection(players) for options in identities):
                continue
            report = row.get('report') or {}
            code, fight = report.get('code'), report.get('fightID')
            if re.fullmatch('[A-Za-z0-9]+', code or '') and type(fight) is int and fight > 0:
                return f'https://www.warcraftlogs.com/reports/{code}#fight={fight}'
    return None


def resolve_run(source, season, required, run_id):
    detail = source.rio('/api/v1/mythic-plus/run-details', season=season.rio_season, id=run_id)
    logged = detail.get('logged_details') or {}
    url = f'{RIO}/mythic-plus-runs/{season.rio_season}/{run_id}'
    facts = validate_details(detail, dict(url=url, keystone_run_id=run_id, dungeon=detail['dungeon']['name'],
                                          mythic_level=detail['mythic_level']), season.rio_season)
    if not facts or facts['num_chests'] < 1:
        return None
    team = [member['character'] for member in facts['roster']]
    if not matches([c['spec'].get('id') for c in team], required):
        return None
    name = facts['dungeon']['name']

    def normalized(value):
        return ''.join(c for c in value.casefold() if c.isalnum())

    encounter = next((e['id'] for e in season.mplus_encounters
                      if normalized(e['name']) == normalized(name)), None)
    report = None
    report_status = '暂无同场公开报告' if encounter else '缺少副本映射'
    if encounter:
        key = f'composition:wcl:{season.rio_season}:{encounter}:{run_id}'
        cached = cache.get(key)
        if cached is None:
            try:
                report = wcl_link(detail, encounter)
                cache.set(key, report or '', 300)
            except RuntimeError:
                report_status = 'WCL 暂时无法查询'
        else:
            report = cached or None

    lineup = []
    for member in detail['roster']:
        c = member['character']
        cls, spec = SPEC_IDENTITY_MAP[c['spec']['id']]
        items = member.get('items') or {}
        equipment = [dict(slot=SLOT_CN.get({'mainhand': 'mainHand', 'offhand': 'offHand'}.get(slot, slot), slot),
                          name=item['name'], level=item.get('item_level'))
                     for slot, item in (items.get('items') or {}).items()
                     if isinstance(item, dict) and item.get('name')]
        talent = c.get('talentLoadout') or {}
        talent_code = talent.get('importLoadoutText') if isinstance(talent, dict) else None
        realm = c['realm']
        realm_name = realm.get('altName') if realm.get('locale') == 'zh_CN' else realm.get('name')
        lineup.append(dict(name=c['name'], realm=realm_name or realm['slug'], region=c['region']['slug'].upper(),
                           spec=SPEC_CN[spec], cls=CLASS_CN[cls], role=SPEC_ROLE[cls, spec],
                           color=CLASS_COLOR[cls], guild=(member.get('guild') or {}).get('name'),
                           equipment=equipment, talent=talent_code, ilvl=items.get('item_level_equipped')))
    lineup.sort(key=lambda m: ROLES.index(m['role']))
    encounters = [dict(name=e['boss']['name'], duration=e.get('duration_ms'),
                       end=e.get('approximate_relative_ended_at'), success=e.get('is_success'))
                  for e in logged.get('encounters') or [] if (e.get('boss') or {}).get('name')]
    route = None
    if logged.get('showing_route_authorized') is True and logged.get('route_key'):
        route = [facts['dungeon']['slug'], logged['route_key']]
    return dict(id=run_id, dungeon=DUNGEON_CN.get(name, name), level=facts['mythic_level'],
                score=detail['score'], roster=lineup, io=url, wcl=report, wcl_status=report_status,
                route=route, duration=detail['clear_time_ms'], remaining=detail.get('time_remaining_ms'),
                completed=detail['completed_at'], chests=detail['num_chests'], encounters=encounters,
                affixes=[m['name'] for m in detail.get('weekly_modifiers') or [] if m.get('name')],
                deaths=len(logged['deaths']) if isinstance(logged.get('deaths'), list) else None)


def export_route(source, slug, key):
    cache_key = f'composition:export:{slug}:{key}'
    code = cache.get(cache_key)
    if code:
        return code
    try:
        code = source.export(slug, key)
        preset = decode_mdt2_table(code)
    except ValueError:
        return None
    if isinstance(preset, dict) and isinstance(preset.get('value'), dict) and preset['value'].get('pulls'):
        cache.set(cache_key, code, 300)
        return code
    return None
