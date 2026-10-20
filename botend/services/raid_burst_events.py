"""增量读取 WCL 玩家事件，逐页保存进度；完整原始日志可跨聚合版本复用。"""

import re

from botend.services.raid_burst_snapshot import digest, read_json, write_json


RAW_VERSION = 1
META = '''query($code:String!, $ids:[Int]) {reportData {report(code:$code) {
 startTime
 fights(fightIDs:$ids) {id name encounterID difficulty kill startTime endTime phaseTransitions {id startTime}}
 masterData {actors(type:"Player") {id name server subType}}
}}}'''
PAGE = '''query($code:String!, $ids:[Int], $actor:Int, $start:Float, $end:Float, $kind:EventDataType) {
 reportData {report(code:$code) {
 page:events(fightIDs:$ids,dataType:$kind,sourceID:$actor,startTime:$start,endTime:$end,limit:10000) {
 data nextPageTimestamp}
}}}'''
STREAMS = {'casts': 'Casts', 'buffs': 'Buffs', 'combatants': 'CombatantInfo'}


class EventFetchDeferred(RuntimeError):
    def __init__(self, reason, retry_at=None):
        super().__init__(reason)
        self.retry_at = retry_at


def realm_key(value):
    return re.sub(r'[\s-]+', '', value or '').casefold()


def identity(row):
    return {key: row.get(key) for key in
            ('report_code', 'fight_id', 'character_name', 'realm', 'region', 'class_name')}


def cache_path(root, row):
    return root / 'events' / f'{digest(identity(row))}.json'


def fetch_events(root, row, client, guard):
    """身份或分页失败不发布新统计；恢复时接着已成功的页继续。"""
    key = identity(row)
    path = cache_path(root, row)
    envelope = read_json(path)
    state = envelope.get('data') if isinstance(envelope, dict) else None
    if (not isinstance(state, dict) or envelope.get('digest') != digest(state)
            or state.get('identity') != key or state.get('version') != RAW_VERSION):
        state = {'version': RAW_VERSION, 'identity': key, 'streams': {}}

    def save():
        with guard():
            write_json(path, {'data': state, 'digest': digest(state)})

    def request(query, variables):
        with guard():
            pass
        data = client._wcl_graphql(query, variables)
        if data is None:
            raise EventFetchDeferred(client._wcl_last_error or '事件请求失败', client._wcl_retry_at)
        report = (data.get('reportData') or {}).get('report')
        if not isinstance(report, dict):
            raise EventFetchDeferred('日志不可读取')
        return report

    if not row.get('report_code') or not row.get('fight_id'):
        return {'invalid': '缺少日志地址'}
    if 'report' not in state:
        state['report'] = request(META, {'code': row['report_code'], 'ids': [row['fight_id']]})
        save()
    report = state['report']
    fights = [f for f in report.get('fights', []) if f['id'] == row['fight_id']]
    actors = [a for a in (report.get('masterData') or {}).get('actors', [])
              if a.get('name', '').casefold() == row['character_name'].casefold()
              and realm_key(a.get('server')) == realm_key(row.get('realm'))
              and a.get('subType') == row['class_name']]
    if len(fights) != 1 or len(actors) != 1:
        return {'invalid': '战斗或玩家身份无法唯一确认'}
    fight, actor = fights[0], actors[0]
    if not fight.get('kill') or fight.get('encounterID') != row['boss_id'] or fight.get('difficulty') != row['difficulty']:
        return {'invalid': '首领、难度或击杀状态不匹配'}
    for field, kind in STREAMS.items():
        # 战斗人员信息可能在开怪前写入，首个请求仅按 fightIDs 限定。
        initial_cursor = None if field == 'combatants' else fight['startTime']
        stream = state['streams'].setdefault(field, {'data': [], 'cursor': initial_cursor, 'complete': False})
        while not stream['complete']:
            cursor = stream['cursor']
            page = request(PAGE, {'code': row['report_code'], 'ids': [row['fight_id']],
                                 'actor': actor['id'], 'start': cursor,
                                 'end': fight['endTime'], 'kind': kind}).get('page')
            if not isinstance(page, dict) or not isinstance(page.get('data'), list) or 'nextPageTimestamp' not in page:
                raise EventFetchDeferred('事件分页结构不完整')
            next_cursor = page['nextPageTimestamp']
            if next_cursor is not None and (not isinstance(next_cursor, (int, float))
                                            or next_cursor <= (cursor if cursor is not None else -1)
                                            or next_cursor > fight['endTime']):
                raise EventFetchDeferred('事件分页游标异常')
            stream['data'].extend(page['data'])
            stream.update(cursor=next_cursor, complete=next_cursor is None)
            save()
    return {'report': report, 'actor': actor, 'streams': state['streams']}
