"""大秘境筛选维度：身份、地区周期和查询参数的统一校验。"""
from datetime import datetime, timedelta
from copy import deepcopy
from zoneinfo import ZoneInfo
import math
import re
from django.utils import timezone

from botend.constants.wow import SPEC_IDENTITY_MAP
from botend.wow_i18n import cn_class_from_slug, cn_spec_from_slug

CLASSES = {1: 'Warrior', 2: 'Paladin', 3: 'Hunter', 4: 'Rogue', 5: 'Priest',
           6: 'DeathKnight', 7: 'Shaman', 8: 'Mage', 9: 'Warlock', 10: 'Monk',
           11: 'Druid', 12: 'DemonHunter', 13: 'Evoker'}
CLASS_IDS = {name: identity for identity, name in CLASSES.items()}
ID_FIELDS = {
    'include_classes': 'includeClassIds', 'exclude_classes': 'excludeClassIds',
    'include_specs': 'includeSpecIds', 'exclude_specs': 'excludeSpecIds',
    'death_classes': 'includePlayerDeathClassIds', 'death_specs': 'includePlayerDeathSpecIds',
    'exclude_death_classes': 'excludePlayerDeathClassIds', 'exclude_death_specs': 'excludePlayerDeathSpecIds',
    'include_affixes': 'includeAffixIds', 'exclude_affixes': 'excludeAffixIds',
}
RANGE_FIELDS = {
    'item_min': ('minItemLevel', 0, 5000), 'item_max': ('maxItemLevel', 0, 5000),
    'deaths_min': ('minPlayerDeaths', 0, 5000), 'deaths_max': ('maxPlayerDeaths', 0, 5000),
    'timer_min': ('minTimerFraction', 0, 10), 'timer_max': ('maxTimerFraction', 0, 10),
}
PERIODS = {'season': '赛季累计', 'last-reset': '最近完整周', 'this-reset': '本周（未结束）',
           'last-2-resets': '最近 2 个完整周', 'last-3-resets': '最近 3 个完整周',
           'last-4-resets': '最近 4 个完整周', 'custom': '自选周区间'}


def slug(value):
    return re.sub(r'(?<!^)(?=[A-Z])', '-', value).lower()


def identities():
    classes = [{'value': str(i), 'label': cn_class_from_slug(slug(name))} for i, name in CLASSES.items()]
    specs = [{'value': str(i), 'class_id': str(CLASS_IDS[cls]),
              'label': f'{cn_spec_from_slug(slug(spec))}（{cn_class_from_slug(slug(cls))}）'}
             for i, (cls, spec) in SPEC_IDENTITY_MAP.items()]
    return {'classes': classes, 'specs': specs}


def integer(value, minimum, maximum):
    if isinstance(value, bool) or not re.fullmatch(r'\d{1,5}', str(value)):
        raise ValueError('请选择有效的整数范围')
    result = int(value)
    if not minimum <= result <= maximum:
        raise ValueError('筛选数值超出范围')
    return result


def normalize_filters(params, meta, layer):
    result = {}
    for key in ID_FIELDS:
        raw = params.get(key)
        if raw in (None, '', []):
            continue
        values = raw if isinstance(raw, list) else str(raw).split(',')
        if len(values) > 5:
            raise ValueError('同一条件最多选择五项')
        values = sorted({integer(v, 1, 100000) for v in values})
        allowed = (set(CLASSES) if 'classes' in key else set(SPEC_IDENTITY_MAP) if 'specs' in key
                   else {int(a['id']) for a in meta.get('affixes', [])})
        if not set(values) <= allowed:
            raise ValueError('筛选身份不属于当前资料')
        if 'death_' in key and layer != 'player_death':
            raise ValueError('死亡者筛选只适用于死亡位置')
        result[key] = values
    for positive, negative in [('include_classes', 'exclude_classes'), ('include_specs', 'exclude_specs'),
                               ('death_classes', 'exclude_death_classes'), ('death_specs', 'exclude_death_specs'),
                               ('include_affixes', 'exclude_affixes')]:
        if set(result.get(positive, [])) & set(result.get(negative, [])):
            raise ValueError('同一身份不能同时包含和排除')
    for key, (_, lower, upper) in RANGE_FIELDS.items():
        raw = params.get(key)
        if raw in (None, ''):
            continue
        if isinstance(raw, bool):
            raise ValueError('筛选数值无效')
        try:
            value = float(raw)
        except (ValueError, TypeError):
            raise ValueError('请输入有效的筛选数值') from None
        if not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError('筛选数值超出范围')
        if key.startswith('deaths_') and value != int(value):
            raise ValueError('死亡数必须是整数')
        # 默认用时上限为时限的 100%，不制造与旧快照等价的新键。
        if key == 'timer_max' and value == 1:
            continue
        if key.endswith('_min') and value == 0:
            continue
        result[key] = int(value) if value == int(value) else value
    for prefix in ('item', 'deaths', 'timer'):
        lower, upper = result.get(prefix + '_min', 0), result.get(prefix + '_max', 1 if prefix == 'timer' else 5000)
        if lower > upper:
            raise ValueError('筛选下限不能大于上限')
    if str(params.get('zero', '0')) == '1' and (result.get('deaths_min', 0) > 0 or result.get('deaths_max', 0) > 0):
        raise ValueError('零死亡条件与死亡数范围冲突')
    return result


def upstream_filters(filters):
    result = {ID_FIELDS[k]: ','.join(map(str, v)) for k, v in filters.items() if k in ID_FIELDS}
    result.update({RANGE_FIELDS[k][0]: v for k, v in filters.items() if k in RANGE_FIELDS})
    return result


def stamp(value):
    return datetime.fromisoformat(str(value).replace('Z', '+00:00'))


def project_periods(meta, now=None):
    """利用已发布的下一周期跨过当天重置，不把凌晨缓存的旧周标成最新完整周。"""
    rows = deepcopy(meta.get('periods', []))
    now = now or timezone.now()
    for row in rows:
        current = row.get('current')
        if not current:
            continue
        try:
            begin, end = stamp(current['start']), stamp(current['end'])
            if begin.tzinfo is None or end.tzinfo is None or begin >= end:
                raise ValueError
            if now < begin:
                row['stale'] = True
            elif now >= end:
                future = row.get('next') or {}
                if (future.get('period') != current['period'] + 1 or stamp(future['start']) != end
                        or not stamp(future['start']) <= now < stamp(future['end'])):
                    row['stale'] = True
                else:
                    row.update(previous=current, current=future, next=None, projected=True)
        except (ValueError, KeyError, TypeError):
            row['stale'] = True
    return dict(meta, periods=rows)


def week_options(meta, region):
    target = 'us' if region == 'world' else region
    current = next((p.get('current') for p in meta.get('periods', []) if p.get('region') == target and not p.get('stale')), None)
    season = next((s for s in meta.get('seasons', []) if s['slug'] == meta.get('current_season')), {})
    start = season.get('starts', {}).get(target)
    if not current or not start:
        return []
    current_start, season_start = stamp(current['start']), stamp(start)
    # 夏令时边界可能相差一小时，不能向下取整导致整个赛季周编号偏移。
    elapsed = round((current_start - season_start).total_seconds() / 604800)
    if elapsed < 0:
        return []
    first_period = int(current['period']) - elapsed
    result = []
    for index in range(min(elapsed + 1, 150)):
        begin = current_start - timedelta(weeks=elapsed - index)
        end = begin + timedelta(weeks=1)
        local_begin, local_end = begin.astimezone(ZoneInfo('Asia/Shanghai')), end.astimezone(ZoneInfo('Asia/Shanghai'))
        result.append({'value': str(index + 1), 'period': first_period + index,
                       'label': f'第 {index + 1} 周（{local_begin:%m/%d}–{local_end:%m/%d}）',
                       'date': local_begin.strftime('%Y-%m-%d'),
                       'start': begin.isoformat(), 'end': end.isoformat()})
    return result


def add_period(coordinate, params, meta):
    period = params.get('period', 'season')
    if period not in PERIODS:
        raise ValueError('未知周区间')
    coordinate['period'] = period
    if period == 'season':
        return
    target = 'us' if coordinate['region'] == 'world' else coordinate['region']
    reference = next((p for p in meta.get('periods', []) if p.get('region') == target), None)
    if not reference or reference.get('stale'):
        raise ValueError('尚未取得地区周期')
    if period == 'last-reset':
        coordinate['period_id'] = int(reference['previous']['period'])
        return
    weeks = week_options(meta, coordinate['region'])
    if not weeks:
        raise ValueError('当前赛季的周区间尚未准备好')
    if period == 'custom':
        first = integer(params.get('week_start', ''), 1, len(weeks))
        last = integer(params.get('week_end', ''), 1, len(weeks))
        if first > last:
            raise ValueError('起始周不能晚于结束周')
        low, high = weeks[first - 1]['period'], weeks[last - 1]['period']
    elif period == 'this-reset':
        low = high = int(reference['current']['period'])
    else:
        count = int(period.split('-')[1])
        high = int(reference['previous']['period'])
        low = max(weeks[0]['period'], high - count + 1)
    if high < weeks[0]['period']:
        raise ValueError('当前赛季还没有完整周')
    coordinate.update(min_period=low, max_period=high)


def period_parameters(coordinate):
    if coordinate.get('period_id') is not None:
        return {'minPeriod': coordinate['period_id'], 'maxPeriod': coordinate['period_id']}
    if coordinate.get('min_period') is not None:
        return {'minPeriod': coordinate['min_period'], 'maxPeriod': coordinate['max_period']}
    return {}
