"""Raider.IO 公开统计采集与事务快照；公开读取不联网、不登记采集任务。"""
from __future__ import annotations

from datetime import datetime, timedelta
from copy import deepcopy
from email.utils import parsedate_to_datetime
import hashlib
import json
import logging
import math
from pathlib import Path
from functools import lru_cache
import re
from uuid import uuid4
from zoneinfo import ZoneInfo

import requests
from django.conf import settings
from django.utils import timezone

from botend.services.simc_benchmark_result_snapshot import _lock, _load
from botend.wow_i18n import cn_dungeon_from_slug
from botend.services.rio_statistics_scope import active_scope, public_scope, ScopeUnavailable
from botend.services.rio_statistics_localization import localize_catalog
from django.db import DatabaseError
from botend.services.rio_statistics_dimensions import (PERIODS, identities, normalize_filters,
    upstream_filters, week_options, add_period, period_parameters, integer, ID_FIELDS, RANGE_FIELDS, project_periods)
from botend.services.rio_statistics_storage import SnapshotStore, StorageError

logger = logging.getLogger(__name__)
REGIONS = {'cn': '国服', 'world': '全球', 'us': '美服', 'eu': '欧服', 'kr': '韩服', 'tw': '台服'}
LEVELS = {'all': (2, 99, '全部层数'), '10-plus': (10, 99, '10 层及以上'),
          '20-plus': (20, 99, '20 层及以上'),
          '2-5': (2, 5, '2–5 层'), '6-9': (6, 9, '6–9 层'),
          '10-14': (10, 14, '10–14 层'), '15-19': (15, 19, '15–19 层'),
          '20-24': (20, 24, '20–24 层'), '25-plus': (25, 99, '25 层及以上'),
          **{f'level-{level}': (level, level, f'{level} 层') for level in range(2, 31)}}
DEFAULT_LEVELS = tuple(LEVELS)
DAILY_REQUEST_BUDGET = 6000
MAX_REQUEST_BUDGET = 8000
LAYERS = {'player_death': '死亡位置', 'player_spell': '嗜血位置', 'npc_death': '怪物击杀'}
DIFFICULTIES = {'mythic': '史诗', 'heroic': '英雄', 'normal': '普通'}
LUST_SPELLS = '2825,32182,80353,264667,390386'
MODULES = ('overview', 'runs', 'raids', 'heatmaps')
SCHEMA = 1
LOCAL_TZ = ZoneInfo('Asia/Shanghai')


def _next_daily():
    return (timezone.now().astimezone(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1)).isoformat()


def root():
    return Path(getattr(settings, 'RIO_STATISTICS_ROOT', Path(settings.BASE_DIR) / 'var/rio-statistics'))


@lru_cache(maxsize=16)
def _store_for(path):
    return SnapshotStore(path)


def store():
    return _store_for(str(root().resolve()))


def _write(path, value):
    """调用方持发布锁；替换前完整编码，不清理其他文件。"""
    content = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(path.name + '.' + uuid4().hex + '.next')
    with staging.open('w', encoding='utf-8') as stream:
        stream.write(content)
        stream.flush()
        import os
        os.fsync(stream.fileno())
    staging.replace(path)


def _now():
    return timezone.now().isoformat()


def _stamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def _index():
    return store().index()


def key_for(coordinate):
    return hashlib.sha256(json.dumps(coordinate, sort_keys=True).encode()).hexdigest()


def read_entry(coordinate, *, meta=None):
    """读取者容忍文件缺失；不修改状态或产生网络请求。"""
    key = key_for(coordinate)
    cache_error = ''
    try:
        data, entry = store().read(coordinate)
    except StorageError as exc:
        data, entry, cache_error = {}, {}, str(exc)
    if not isinstance(data, dict) or data.get('coordinate') != coordinate or data.get('schema') != SCHEMA:
        data = {}
    stamp = _stamp(data.get('fetched_at'))
    stale = bool(data) and (bool(entry.get('error')) or not stamp or timezone.now() - stamp > timedelta(hours=36))
    if data and coordinate['module'] == 'heatmaps':
        meta = meta or current_catalog()
        maps = meta.get('maps', {}).get(coordinate['dungeon'], {})
        valid_maps = maps.get('floors', {}) if maps.get('scope_token') == coordinate.get('scope') else {}
        data = deepcopy(data)
        for i, floor in enumerate(data.get('data', {}).get('floors', []), 1):
            atlas = valid_maps.get(str(floor['id']), {})
            floor['name'] = atlas.get('name') or f'第 {i} 层（地图待更新）'
            floor['map'] = {k: atlas[k] for k in ('image_url', 'image_key', 'bounds') if k in atlas}
            if atlas.get('image_key') and not (root() / 'maps' / (atlas['image_key'] + '.webp')).is_file():
                floor['map'] = {}
    if data and coordinate['module'] == 'runs':
        meta = meta or current_catalog()
        weeks = {w['period']: w for w in week_options(meta, coordinate['region'])}
        data = deepcopy(data)
        for row in data.get('data', {}).get('rows', []):
            week = weeks.get(row['period'])
            if week:
                row.update(date=week['date'], label=week['label'])
    return {**data, 'snapshot': {
        'state': 'error' if cache_error else 'stale' if stale else 'ready' if data else 'pending',
        'fetched_at': data.get('fetched_at'), 'upstream_at': data.get('upstream_at'),
        'last_attempt_at': entry.get('attempted_at'),
        'message': cache_error or ('本次更新未完成，保留上次数据。' if stale else
                   '该筛选范围尚未采集，请选择其他范围或稍后查看。' if not data else ''
        ),
    }}


def _publish(coordinate, payload, *, upstream_at=None, newer_only=False):
    return store().publish(coordinate, payload, fetched_at=_now(), upstream_at=upstream_at,
                           next_at=_next_daily(), newer_only=newer_only)


def _failure(coordinate, message):
    store().failure(coordinate, message, attempted_at=_now(),
                    next_at=(timezone.now() + timedelta(minutes=30)).isoformat())


def number(value, *, nullable=False):
    if nullable and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('来源数值不合法')
    return value


def normalize_overview(data, season, region):
    if not isinstance(data, dict) or data.get('season') != season or data.get('region') != region:
        raise ValueError('来源赛季或地区不匹配')
    populations = data.get('populations')
    history = data.get('history')
    if not isinstance(populations, list) or not populations or not isinstance(history, list):
        raise ValueError('来源缺少角色或历史数据')
    groups = []
    for row in populations:
        percentiles = [{'percentile': number(p['percentile']), 'score': number(p.get('cutoffScore'), nullable=True)}
                       for p in row.get('percentiles', [])]
        groups.append({'faction': row.get('faction', 'all'), 'population': number(row['population']),
                       'percentiles': percentiles})
    rows = []
    for row in history:
        datetime.strptime(row['date'], '%Y-%m-%d')
        rows.append({'date': row['date'], 'population': number(row.get('population'), nullable=True),
                     'cutoffs': [{'percentile': number(p['percentile']), 'score': number(p.get('score'), nullable=True)}
                                 for p in row.get('cutoffs', [])]})
    if len({r['date'] for r in rows}) != len(rows):
        raise ValueError('历史日期重复')
    return {'season': season, 'region': region, 'snapshot_date': data.get('snapshotDate'),
            'populations': groups, 'history': sorted(rows, key=lambda r: r['date'])}


def normalize_raid(data):
    if not isinstance(data, dict) or not isinstance(data.get('progression'), list):
        raise ValueError('来源团本进度无效')
    rows = [{'progress': int(number(row['progress'])), 'guilds': int(number(row['totalGuilds']))}
            for row in data['progression']]
    if len({r['progress'] for r in rows}) != len(rows):
        raise ValueError('进度档位重复')
    return {'progression': sorted(rows, key=lambda r: r['progress'])}


def normalize_heatmap(data, floors):
    if not isinstance(data, dict) or not isinstance(data.get('gridsByFloor'), dict):
        raise ValueError('来源空间网格无效')
    floor_index = {str(row['id']): row for row in floors}
    if any(floor_id not in floor_index or not isinstance(grid, dict)
           for floor_id, grid in data['gridsByFloor'].items()):
        raise ValueError('来源楼层不匹配')
    result = []
    # 单层样本稀少时来源可能不返回任何网格；保留真实楼层，显示空数据底图。
    for floor_id, floor in floor_index.items():
        grid = data['gridsByFloor'].get(floor_id, {})
        bounds = [floor[k] for k in ('ingameMinX', 'ingameMinY', 'ingameMaxX', 'ingameMaxY')]
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in bounds) or bounds[0] == bounds[2] or bounds[1] == bounds[3]:
            raise ValueError('来源坐标边界无效')
        points = []
        for location, quantity in grid.items():
            coords = location.split(',')
            if len(coords) != 2:
                raise ValueError('来源坐标无效')
            x, y = map(float, coords)
            if not math.isfinite(x) or not math.isfinite(y):
                raise ValueError('来源坐标非有限数')
            points.append([x, y, number(quantity)])
        result.append({'id': floor_id, 'name': str(floor.get('name', floor_id)),
                       'default': bool(floor.get('default')), 'ui_map_id': floor.get('uiMapId'),
                       'bounds': bounds, 'points': points})
    return {'num_runs': number(data.get('numRuns')), 'min_samples': number(data.get('minSamplesRequired', 0)),
            'max_samples': number(data.get('maxSamplesInGrid', 0)), 'floors': result}


def catalog():
    data = _load(root() / 'catalog.json', {})
    if not isinstance(data, dict) or data.get('schema') != SCHEMA:
        data = {'schema': SCHEMA, 'seasons': [], 'raids': [], 'current_season': '', 'floors': {}, 'periods': []}
    return data


def public_catalog():
    data = current_catalog()
    return describe_catalog(data, store().available((data.get('scope') or {}).get('token')))


def describe_catalog(data, available):
    """组装展示目录，调用方可以使用后台已发布的可用范围。"""
    return {k: data.get(k) for k in ('seasons', 'raids', 'current_season', 'updated_at', 'scope', 'scope_state', 'scope_message')} | {
        'regions': [{'value': k, 'label': v} for k, v in REGIONS.items()],
        'levels': [{'value': k, 'label': v[2], 'min': v[0], 'max': v[1],
                    'group': '单层' if k.startswith('level-') else '层数区间' if k in ('2-5', '6-9', '10-14', '15-19', '20-24', '25-plus') else '汇总范围'}
                   for k, v in LEVELS.items()] + [{'value': 'custom', 'label': '自选层数区间', 'group': '自选范围'}],
        'periods': [{'value': k, 'label': v} for k, v in PERIODS.items()],
        'layers': [{'value': k, 'label': v} for k, v in LAYERS.items()],
        **identities(), 'affixes': data.get('affixes', []),
        'weeks': {region: week_options(data, region) for region in REGIONS},
        'available': available,
    }


def current_catalog():
    """数据库变化立即门禁；数据和底图等到夜间任务按新版本发布。"""
    data = catalog()
    try:
        scope = active_scope()
    except (ScopeUnavailable, DatabaseError) as exc:
        message = str(exc) if isinstance(exc, ScopeUnavailable) else '当前赛季资料暂不可用，请稍后查看。'
        return {**data, 'scope': None, 'scope_state': 'unconfigured', 'scope_message': message,
                'current_season': '', 'seasons': [], 'raids': [], 'affixes': []}
    if (data.get('scope') or {}).get('token') != scope['token']:
        return {**data, 'scope': public_scope(scope), 'scope_state': 'updating',
                'scope_message': '数据库赛季或版本已更新，新版本数据将在夜间长任务中准备。',
                'current_season': scope['rio_season'], 'raids': [], 'maps': {}, 'affixes': [],
                'seasons': [{'slug': scope['rio_season'], 'name': public_scope(scope)['season_name'], 'dungeons': []}]}
    data = localize_catalog(deepcopy(data), scope)
    return {**project_periods(data), 'scope': public_scope(scope), 'scope_state': 'ready', 'scope_message': ''}


def _add_level(result, params, default):
    level = params.get('level', default)
    if level not in (*LEVELS, 'custom'):
        raise ValueError('层数区间无效')
    result['level'] = level
    if level == 'custom':
        low = integer(params.get('min_level', ''), 2, 99)
        high = integer(params.get('max_level', ''), low, 99)
        equivalent = next((k for k, v in LEVELS.items() if v[:2] == (low, high)), None)
        if equivalent:
            result['level'] = equivalent
        else:
            result.update(min_level=low, max_level=high)


def coordinate(module, params, *, meta=None):
    """只接受声明的有限筛选；非法范围不会退回其他范围。"""
    if module not in MODULES:
        raise ValueError('未知数据类型')
    data = meta or current_catalog()
    if not data.get('scope'):
        raise ScopeUnavailable(data.get('scope_message', '尚未配置当前赛季。'))
    season = params.get('season') or data.get('current_season', '')
    region = params.get('region', 'cn')
    if season not in {r['slug'] for r in data.get('seasons', [])} or region not in REGIONS:
        raise ValueError('赛季或地区不存在')
    result = {'module': module, 'season': season, 'region': region, 'scope': data['scope']['token']}
    if module == 'runs':
        if set(params) - {'season', 'region', 'level', 'min_level', 'max_level'}:
            raise ValueError('完成记录只支持地区和层数筛选，周区间按已收录记录展示')
        _add_level(result, params, 'all')
    if module == 'raids':
        raid = params.get('raid', '')
        difficulty = params.get('difficulty', 'mythic')
        if raid not in {r['slug'] for r in data.get('raids', [])} or difficulty not in DIFFICULTIES:
            raise ValueError('团本或难度不存在')
        result.update(raid=raid, difficulty=difficulty)
    if module == 'heatmaps':
        allowed = {'season', 'region', 'dungeon', 'layer', 'level', 'period', 'zero', 'week_start', 'week_end',
                   'min_level', 'max_level'} | set(ID_FIELDS) | set(RANGE_FIELDS)
        if set(params) - allowed:
            raise ValueError('存在不支持的筛选字段')
        season_row = next(r for r in data['seasons'] if r['slug'] == season)
        dungeon = params.get('dungeon', '')
        layer, level, period = params.get('layer', 'player_death'), params.get('level', '10-plus'), params.get('period', 'season')
        zero = params.get('zero', '0')
        if (dungeon not in {r['slug'] for r in season_row['dungeons']} or layer not in LAYERS or level not in (*LEVELS, 'custom')
                or period not in PERIODS or str(zero) not in ('0', '1') or (str(zero) == '1' and layer == 'player_death')):
            raise ValueError('热力图筛选范围无效')
        result.update(dungeon=dungeon, layer=layer, level=level, period=period, zero=str(zero))
        _add_level(result, params, '10-plus')
        add_period(result, params, data)
        if period == 'custom':
            result.update(week_start=str(int(params['week_start'])), week_end=str(int(params['week_end'])))
        filters = normalize_filters(params, data, layer)
        if filters:
            result['filters'] = filters
    return result


class RateLimited(Exception):
    pass


class Collector:
    def __init__(self, *, max_requests=DAILY_REQUEST_BUDGET, transport=None, force=False):
        self.max_requests = max(1, min(int(max_requests), MAX_REQUEST_BUDGET))
        self.requests = 0
        self.session = requests.Session() if transport is None else None
        self.transport = transport or self.session.get
        self.force = force
        self.stopped = False
        self.scope = None

    def request(self, url, params=None):
        if self.stopped or self.requests >= self.max_requests:
            raise RuntimeError('本轮采集预算已用完')
        self.requests += 1
        cfg = getattr(settings, 'REQUEST_CONFIG', {})
        try:
            response = self.transport(url, params=params or {}, timeout=(8, 45),
                                      headers={'User-Agent': 'LMonitor/1.0 (Raider.IO community statistics)'},
                                      proxies=cfg.get('proxies') if cfg.get('enable_proxy') else None)
        except (requests.ConnectionError, requests.Timeout):
            # 连接恢复仅重试一次，计入同一夜间任务预算；HTTP 限流绝不走此分支。
            if self.requests >= self.max_requests:
                raise
            self.requests += 1
            response = self.transport(url, params=params or {}, timeout=(8, 45),
                                      headers={'User-Agent': 'LMonitor/1.0 (Raider.IO community statistics)'},
                                      proxies=cfg.get('proxies') if cfg.get('enable_proxy') else None)
        if response.status_code == 429:
            self.stopped = True
            retry = response.headers.get('Retry-After', '300')
            try:
                seconds = max(1, int(retry))
            except (TypeError, ValueError):
                try:
                    seconds = max(1, int((parsedate_to_datetime(retry) - timezone.now()).total_seconds()))
                except (TypeError, ValueError, OverflowError):
                    seconds = 300
            with _lock(root() / 'publish.lock'):
                _write(root() / 'cooldown.json', {'until': (timezone.now() + timedelta(seconds=seconds)).isoformat()})
            raise RateLimited('来源限流，已停止本轮请求并记录退避时间')
        response.raise_for_status()
        return response

    def get(self, path, params=None):
        return self.request('https://raider.io' + path, params).json()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.session:
            self.session.close()

    def get_bytes(self, url):
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc not in ('keystone.guru', 'assets.keystone.guru'):
            raise ValueError('地图来源地址无效')
        root().mkdir(parents=True, exist_ok=True)
        store()._capacity(1024 * 1024)
        return self.request(url).content

    def guard_scope(self):
        if self.scope and active_scope()['token'] != self.scope['token']:
            raise ScopeUnavailable('采集中数据库赛季版本发生变化，停止发布旧范围。')

    def metadata(self):
        self.scope = active_scope()
        scope = self.scope
        previous = catalog()
        stamp = _stamp(previous.get('updated_at'))
        if ((previous.get('scope') or {}).get('token') == scope['token'] and stamp and 'affixes' in previous
                and stamp.astimezone(LOCAL_TZ).date() == timezone.now().astimezone(LOCAL_TZ).date() and not self.force):
            return project_periods(localize_catalog(previous, scope))
        static = self.get('/api/v1/mythic-plus/static-data', {'expansion_id': scope['expansion_id']})
        raids = self.get('/api/v1/raiding/static-data', {'expansion_id': scope['expansion_id']})
        floors = self.get('/api/v1/live-tracking/heatmaps/floors')
        periods = self.get('/api/v1/periods')
        affixes = self.get('/api/v1/mythic-plus/affixes', {'region': 'cn', 'locale': 'cn'})
        if not isinstance(floors, dict) or not isinstance(periods.get('periods'), list):
            raise ValueError('楼层或周周期元数据无效')
        source = next((r for r in static.get('seasons', []) if r.get('slug') == scope['rio_season']), None)
        if not source:
            raise ScopeUnavailable('上游尚未提供数据库所配置的赛季，保留旧快照但不冒充当前数据。')
        dungeons = [{'slug': d['slug'], 'name': cn_dungeon_from_slug(d['slug'], d['name']),
                     'source_name': d['name'], 'challenge_mode_id': int(d['challenge_mode_id'])} for d in source['dungeons']]
        seasons = [{'slug': scope['rio_season'], 'name': scope['season_name'], 'starts': source['starts'],
                    'ends': source['ends'], 'dungeons': dungeons}]
        season_start, season_end = _stamp(source['starts'].get('cn')), _stamp(source['ends'].get('cn'))
        if not season_start or not season_end:
            raise ValueError('所配置赛季的地区时间无效')
        raid_rows = []
        for row in raids.get('raids', []):
            starts, ends = row.get('starts', {}), row.get('ends', {})
            begin, end = _stamp(starts.get('cn')), _stamp(ends.get('cn'))
            if not begin or not end or begin >= season_end or end <= season_start:
                continue
            raid_rows.append({'slug': row['slug'], 'id': row.get('id'), 'name': row['name'],
                              'source_name': row['name'], 'starts': starts, 'ends': ends,
                              'bosses': len(row.get('encounters', []))})
        known_affixes = {int(a['id']): a for a in previous.get('affixes', [])} if (previous.get('scope') or {}).get('token') == scope['token'] else {}
        scoped_affixes = affixes.get('affix_details', []) if scope['rio_season'] in affixes.get('leaderboard_url', '').split('/') else []
        for affix in scoped_affixes:
            if re.search(r'[\u3400-\u9fff]', str(affix.get('name', ''))):
                known_affixes[int(affix['id'])] = {'id': int(affix['id']), 'name': affix['name']}
        result = {'schema': SCHEMA, 'updated_at': _now(), 'seasons': seasons, 'affixes': list(known_affixes.values()),
                  'current_season': scope['rio_season'], 'scope': public_scope(scope),
                  'raids': raid_rows, 'floors': floors, 'periods': periods['periods'],
                  'maps': previous.get('maps', {}) if (previous.get('scope') or {}).get('token') == scope['token'] else {}}
        result = localize_catalog(result, scope)
        self.guard_scope()
        self.save_metadata(result)
        return project_periods(result)

    def save_metadata(self, meta):
        self.guard_scope()
        with _lock(root() / 'publish.lock'):
            _write(root() / 'catalog.json', meta)

    def fetch(self, c, meta):
        if c['module'] == 'overview':
            data = self.get('/api/v1/mythic-plus/cutoff-analysis/summary', {'season': c['season'], 'region': c['region']})
            return normalize_overview(data, c['season'], c['region']), data.get('updatedAt')
        if c['module'] == 'raids':
            data = self.get('/api/v1/raiding/progression', {'raid': c['raid'], 'region': c['region'], 'difficulty': c['difficulty']})
            return normalize_raid(data), None
        season = next(r for r in meta['seasons'] if r['slug'] == c['season'])
        dungeon = next(d for d in season['dungeons'] if d['slug'] == c['dungeon'])
        minimum, maximum = ((c['min_level'], c['max_level']) if c['level'] == 'custom' else LEVELS[c['level']][:2])
        params = {'season': c['season'], 'challengeModeId': dungeon['challenge_mode_id'],
                  'type': c['layer'], 'minMythicLevel': minimum, 'maxMythicLevel': maximum,
                  'dataType': 'enemy_position' if c['layer'] == 'npc_death' else 'player_position',
                  'maxTimerFraction': 1}
        if c['region'] != 'world':
            params['region'] = c['region']
        if c['layer'] == 'player_spell':
            params['includePlayerSpellIds'] = LUST_SPELLS
        if c['zero'] == '1':
            params['maxPlayerDeaths'] = 0
        params.update(period_parameters(c))
        params.update(upstream_filters(c.get('filters', {})))
        data = self.get('/api/v1/live-tracking/heatmaps/grid', params)
        return normalize_heatmap(data, meta['floors'].get(str(dungeon['challenge_mode_id']), [])), None

    def _progress(self, **fields):
        self._report.update(fields)
        self._report.update(requests=self.requests, progress_at=_now())
        store().cycle(self._report, cycle_id=self.cycle_id)

    def run(self, *, season='', regions=('cn', 'world'), modules=MODULES,
            dungeons=(), levels=DEFAULT_LEVELS, periods=('season', 'last-reset'), queries=(), only_queries=False, maintain=False):
        if any(r not in REGIONS for r in regions) or any(m not in MODULES for m in modules):
            raise ValueError('未知地区或模块')
        if any(r not in LEVELS for r in levels) or any(r not in PERIODS for r in periods):
            raise ValueError('未知层数或周范围')
        with _lock(root() / 'collector.lock', blocking=False) as acquired:
            if not acquired:
                return {'state': 'busy', 'requests': 0, 'ready': 0, 'failed': 0}
            cache = store()
            cache.ensure()
            if cache.audit()['integrity'] != 'ok':
                raise StorageError('统计缓存完整性检查失败，停止本次采集')
            cache._capacity(0)
            cache.recover_cycles()
            self.maintain = maintain
            self._report = {'state': 'running', 'stage': 'metadata', 'progress_at': _now(),
                            'kind': 'nightly' if maintain else 'manual', 'day': timezone.now().astimezone(LOCAL_TZ).date().isoformat(),
                            'full_plan': (not only_queries and not dungeons and set(modules) == set(MODULES)
                                          and set(regions) == {'cn', 'world'} and set(levels) == set(DEFAULT_LEVELS)
                                          and set(periods) == {'season', 'last-reset'})}
            self.cycle_id = cache.cycle(self._report)
            try:
                cooldown = _stamp(_load(root() / 'cooldown.json', {}).get('until'))
                if cooldown and cooldown > timezone.now():
                    result = {'state': 'backoff', 'requests': 0, 'ready': 0, 'failed': 0, 'retry_at': cooldown.isoformat()}
                else:
                    result = self._collect(season=season, regions=regions, modules=modules, dungeons=dungeons,
                        levels=levels, periods=periods, queries=queries, only_queries=only_queries)
                try:
                    from botend.services.rio_statistics_views import refresh_views
                    result['views'] = refresh_views()
                    if result['views']['state'] != 'complete':
                        result.update(state='partial', views_error='部分正文未就绪，保留已发布数据')
                except (StorageError, ScopeUnavailable, OSError, ValueError) as exc:
                    result.update(state='partial', views_error=type(exc).__name__)
                    logger.exception('统计展示文件未能发布，保留现有展示')
                if maintain:
                    report = cache.audit(deep=True)
                    entries = cache.index()['entries']
                    invalid_active = [key for key in report['invalid_payloads']
                        if entries[key]['coordinate'].get('scope') == (self.scope or {}).get('token')]
                    result['storage_state'] = 'ok' if report['state'] == 'ok' else 'warning'
                    if invalid_active:
                        result.update(state='partial', maintenance_error='当前版本仍有未修复缓存')
                    try:
                        cache.backup()
                    except StorageError:
                        result.update(state='partial', maintenance_error='备份未完成，原备份保留')
                result['cycle_id'] = self.cycle_id
                cache.cycle(dict(self._report, **result, scope=(self.scope or {}).get('token'), progress_at=_now()), cycle_id=self.cycle_id, final=True)
                return result
            except BaseException as exc:
                if maintain and isinstance(exc, Exception):
                    # 元数据或上游连接失败也要完成本地检查，不能因此漏掉当天备份。
                    try:
                        local_report = cache.audit(deep=True)
                        self._report['storage_state'] = local_report['state']
                        cache.backup()
                    except (StorageError, OSError) as maintenance_exc:
                        self._report['maintenance_error'] = type(maintenance_exc).__name__
                        logger.exception('统计上游失败后的本地维护未完成')
                try:
                    cache.cycle(dict(self._report, state='interrupted' if isinstance(exc, (KeyboardInterrupt, SystemExit)) else 'failed',
                        error=type(exc).__name__, progress_at=_now()), cycle_id=self.cycle_id, final=True)
                except StorageError:
                    logger.exception('统计批次状态未能保存')
                raise

    def _collect(self, *, season, regions, modules, dungeons, levels, periods, queries, only_queries):
        meta = self.metadata()
        self._progress(scope=self.scope['token'], stage='planning')
        if self.maintain:
            report = store().audit(deep=True)
            recovered = store().repair_from_backups(report)
            if recovered:
                report = store().audit(deep=True)
                self._progress(recovered_records=recovered)
            entries = _index()['entries']
            for key in report['invalid_payloads']:
                c = entries[key]['coordinate']
                if c.get('scope') == self.scope['token']:
                    store().failure(c, '缓存正文校验失败，准备重建', attempted_at=_now(), next_at=_now())
        season = season or meta['current_season']
        if season != self.scope['rio_season']:
            raise ScopeUnavailable('只采集数据库中配置的当前赛季。')
        season_row = next((s for s in meta['seasons'] if s['slug'] == season), None)
        if not season_row:
            raise ValueError('未知采集赛季')
        from botend.services.rio_statistics_runs import import_feeds
        feed_result = import_feeds(meta) if 'runs' in modules else {'imported': 0, 'failed': 0}
        if dungeons and not set(dungeons) <= {d['slug'] for d in season_row['dungeons']}:
            raise ValueError('副本不属于该赛季')
        tasks = []
        for region in (() if only_queries else regions):
            if 'overview' in modules:
                tasks.append(coordinate('overview', {'season': season, 'region': region}, meta=meta))
            if 'raids' in modules:
                active = [r for r in meta['raids'] if _stamp(r['starts'].get(region if region != 'world' else 'us'))
                          and _stamp(r['starts'].get(region if region != 'world' else 'us')) <= timezone.now()
                          and _stamp(r['ends'].get(region if region != 'world' else 'us'))
                          and _stamp(r['ends'].get(region if region != 'world' else 'us')) > timezone.now()]
                for raid in sorted(active, key=lambda r: r['starts'].get('cn', ''), reverse=True):
                    for difficulty in DIFFICULTIES:
                        tasks.append(coordinate('raids', {'season': season, 'region': region, 'raid': raid['slug'], 'difficulty': difficulty}, meta=meta))
            if 'heatmaps' in modules:
                for period in periods:
                    for level in levels:
                        for dungeon in season_row['dungeons']:
                            if dungeons and dungeon['slug'] not in dungeons:
                                continue
                            for layer, zero in [('player_death', '0'), ('player_spell', '0'), ('npc_death', '0'), ('player_spell', '1')]:
                                tasks.append(coordinate('heatmaps', {'season': season, 'region': region, 'dungeon': dungeon['slug'],
                                             'layer': layer, 'level': level, 'period': period, 'zero': zero}, meta=meta))
        from botend.services.rio_statistics_queries import pending_queries
        subscribed = [c for c in pending_queries(meta) if c['module'] in modules]
        if queries and ('heatmaps' not in modules or len(queries) > 128 or not all(isinstance(q, dict) for q in queries)):
            raise ValueError('筛选清单最多包含 128 个热力图条件')
        subscribed += [coordinate('heatmaps', q, meta=meta) for q in queries]
        if dungeons:
            subscribed = [c for c in subscribed if c.get('dungeon') in dungeons]
        subscribed_keys = {key_for(c) for c in subscribed}
        tasks = list({key_for(c): c for c in (*tasks, *subscribed)}.values())
        manifest = _index()['entries']
        now = timezone.now()
        deferred = sum(bool(manifest.get(key_for(c), {}).get('error')) and
                       bool(_stamp(manifest.get(key_for(c), {}).get('next_at'))) and
                       _stamp(manifest[key_for(c)]['next_at']) > now for c in tasks) if not self.force else 0
        tasks = [c for c in tasks if self.force or not _stamp(manifest.get(key_for(c), {}).get('next_at'))
                 or _stamp(manifest[key_for(c)]['next_at']) <= now]
        # 总览优先；其余同一模块先处理未尝试组合，避免单一坏组合饿死其他范围。
        tasks.sort(key=lambda c: (c['module'] != 'overview', key_for(c) not in subscribed_keys,
                                  MODULES.index(c['module']), manifest.get(key_for(c), {}).get('attempted_at', '')))
        ready = failed = 0
        consecutive_errors = 0
        self._progress(stage='snapshots', planned=len(tasks), ready=0, failed=0)
        for c in tasks:
            if self.requests >= self.max_requests or self.stopped:
                break
            try:
                payload, stamp = self.fetch(c, meta)
                self.guard_scope()
                _publish(c, payload, upstream_at=stamp)
                ready += 1
                consecutive_errors = 0
            except ScopeUnavailable:
                self.stopped = True
                break
            except Exception as exc:
                _failure(c, '来源限流' if isinstance(exc, RateLimited) else '本轮采集失败')
                failed += 1
                logger.warning('Raider.IO 统计采集失败 %s：%s', c, type(exc).__name__)
                response = getattr(exc, 'response', None)
                transient = (isinstance(exc, (requests.ConnectionError, requests.Timeout, ValueError))
                             or (isinstance(exc, requests.HTTPError) and response is not None and response.status_code >= 500))
                consecutive_errors = consecutive_errors + 1 if transient else 0
                if isinstance(exc, StorageError) or consecutive_errors >= 3:
                    self.stopped = True
                    with _lock(root() / 'publish.lock'):
                        _write(root() / 'cooldown.json', {'until': (timezone.now() + timedelta(minutes=30)).isoformat()})
                    break
            if (ready + failed) % 25 == 0:
                self._progress(ready=ready, failed=failed)
        maps_failed = 0
        query_dungeons = {c.get('dungeon') for c in subscribed}
        map_targets = [d for d in season_row['dungeons'] if (not dungeons or d['slug'] in dungeons)
                       and (not only_queries or d['slug'] in query_dungeons)] if 'heatmaps' in modules else []
        if 'heatmaps' in modules and not self.stopped:
            from botend.services.rio_statistics_maps import collect_maps
            for dungeon in map_targets:
                if self.requests >= self.max_requests or self.stopped:
                    break
                view = dict(meta, seasons=[dict(season_row, dungeons=[dungeon])])
                try:
                    collect_maps(view, self.scope, self.get_bytes, root(), checkpoint=lambda _: self.save_metadata(meta))
                except Exception as exc:
                    maps_failed += 1
                    logger.warning('数据观察地图采集失败 %s：%s', dungeon['slug'], type(exc).__name__)
                self._progress(stage='maps', ready=ready, failed=failed, maps_failed=maps_failed)
        def map_ready(dungeon):
            atlas = meta.get('maps', {}).get(dungeon['slug'], {})
            return (atlas.get('scope_token') == self.scope['token'] and atlas.get('floors')
                    and all((root() / 'maps' / (f['image_key'] + '.webp')).is_file() for f in atlas['floors'].values()))
        maps_remaining = sum(not map_ready(d) for d in map_targets)
        remaining = max(0, len(tasks) - ready - failed) + deferred
        partial = remaining or failed or maps_failed or maps_remaining or feed_result['failed'] or feed_result.get('pending')
        return {'state': 'backoff' if self.stopped else 'partial' if partial else 'complete', 'requests': self.requests,
                'ready': ready, 'failed': failed, 'maps_failed': maps_failed,
                'maps_remaining': maps_remaining,
                'runs_imported': feed_result['imported'], 'runs_failed': feed_result['failed'],
                'runs_pending': feed_result.get('pending', 0),
                'remaining': remaining}



def run_nightly_collection():
    """由夜间长任务队列调用；本服务不再启动独立高频线程。"""
    with Collector(max_requests=DAILY_REQUEST_BUDGET) as collector:
        return collector.run(maintain=True)
