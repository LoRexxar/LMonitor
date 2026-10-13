"""Mythicstats 唯一后台采集发布服务；公开请求只读取已发布文件。"""
from copy import deepcopy
from pathlib import Path
import re
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from botend.models import MonitorTask, PortalMythicstatsDpsRow
from botend.portal import mythicstats as source
from botend.services.simc_benchmark_result_snapshot import _load, _lock, _write

ROLES = ('damage', 'tank', 'healer')
SCHEMA = 1


def snapshot_root():
    return Path(getattr(settings, 'MYTHICSTATS_SNAPSHOT_ROOT', Path(settings.BASE_DIR) / 'var' / 'mythicstats'))


def _manifest():
    data = _load(snapshot_root() / 'index.json', {})
    if (not isinstance(data, dict) or data.get('schema') != SCHEMA
            or not isinstance(data.get('seasons'), dict) or not isinstance(data.get('current_season'), str)):
        return {'schema': SCHEMA, 'current_season': '', 'seasons': {}}
    return data


def _shard(entry):
    filename = (entry or {}).get('file', '')
    if not re.fullmatch(r'[0-9a-f]{32}\.json', filename):
        return None
    data = _load(snapshot_root() / filename)
    return data if isinstance(data, dict) and isinstance(data.get('roles'), dict) else None


def _number(value, default=0):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def read_snapshot(*, season='', dungeon_id=0, period_id=None):
    """读取不查询数据库、不联网、不登记采集任务。"""
    manifest = _manifest()
    season = str(season or '').strip()
    if season in ('', 'auto', 'season-mn-1'):
        season = manifest['current_season']
    dungeon_id = _number(dungeon_id)
    meta = manifest['seasons'].get(season, {})
    periods = meta.get('periods', [])
    active_period = _number(period_id) or (periods[0]['id'] if periods else None)
    entry = meta.get('shards', {}).get(f'{dungeon_id}:{active_period}', {})
    data = _shard(entry)
    # 不允许错误索引将另一个副本或周次的内容当作当前结果。
    if data and (data.get('season'), data.get('dungeon_id'), data.get('active_period')) != (season, dungeon_id, active_period):
        data = None
    state = 'stale' if data and entry.get('error') else 'ready' if data else 'pending'
    return {
        'season': season, 'seasons': sorted(manifest['seasons']), 'dungeon_id': dungeon_id,
        'periods': periods, 'active_period': active_period,
        'dungeons': meta.get('dungeons') or [{'id': 0, 'name': '全部副本'}],
        'source_url': 'https://mythicstats.com/dps', 'source_note': '', 'key_min': None, 'key_max': None,
        'roles': {role: [] for role in ROLES}, **(data or {}),
        'snapshot': {'state': state, 'generated_at': (data or {}).get('generated_at'),
                     'message': '本次更新暂未成功，正在展示上次数据。' if state == 'stale' else
                                '该范围的数据尚未准备好，请稍后查看。' if state == 'pending' else ''},
    }


def _serialize(row):
    url = (row.spec_url or '').strip()
    if url.startswith('/'):
        url = 'https://mythicstats.com' + url
    elif url and not re.match(r'^https?://', url, flags=re.I):
        url = 'https://mythicstats.com/' + url.lstrip('/')
    return {'rank': row.rank, 'diff_raw': row.diff_raw, 'diff_value': row.diff_value,
            'tier': row.tier, 'avg': row.avg_text, 'avg_value': row.avg_value,
            'top': row.top_text, 'top_value': row.top_value, 'runs': row.runs_text,
            'spec_name': row.spec_name, 'spec_slug': row.spec_slug, 'spec_url': url,
            'week': row.week, 'updated_at': timezone.localtime(row.updated_at).strftime('%Y-%m-%d %H:%M:%S'),
            **source.mythicstats_spec_identity(row.spec_slug)}


def _rows_payload(season, dungeon_id, period_id, rows, metadata):
    roles = {role: [] for role in ROLES}
    for row in rows:
        if row.role in roles and len(roles[row.role]) < 80:
            roles[row.role].append(_serialize(row))
    return {'season': season, 'dungeon_id': dungeon_id, 'active_period': period_id, 'roles': roles,
            'source_note': metadata.get('source_note') or '', 'key_min': metadata.get('key_min'),
            'key_max': metadata.get('key_max'), 'generated_at': timezone.now().isoformat()}


def _write_shard(payload, origin):
    filename = uuid.uuid4().hex + '.json'
    _write(snapshot_root() / filename, payload)
    return {'file': filename, 'origin': origin, 'updated_at': payload['generated_at']}


def _validate(payload, season, dungeon_id, period_id):
    if (payload.get('season'), payload.get('dungeon_id'), payload.get('period_id')) != (season, dungeon_id, period_id):
        raise ValueError('来源赛季、副本或周次与采集范围不一致')
    rankings = payload.get('rankings')
    if not isinstance(rankings, dict) or any(not isinstance(rankings.get(role), list) for role in ROLES):
        raise ValueError('来源职责数据不完整')
    # 解析器没有提供“该职责确实无数据”的证据；空列表也可能是解析失败。
    if any(not rankings[role] for role in ROLES):
        raise ValueError('来源职责榜单为空，无法确认完整性，保留旧版')
    for role in ROLES:
        slugs = [row.get('spec_slug') for row in rankings[role]]
        if any(not slug for slug in slugs) or len(set(slugs)) != len(slugs):
            raise ValueError('来源专精标识缺失或重复')


def _collect_shard(payload, season, dungeon, period):
    did, pid = dungeon['id'], period['id']
    _validate(payload, season, did, pid)
    # 三种职责一起提交；任何一项失败都不能留下半份数据库榜单。
    with transaction.atomic():
        for role in ROLES:
            source.upsert_mythicstats_dps_rows(season=season, period_id=pid, period_label=period['label'],
                dungeon_id=did, dungeon_name=dungeon['name'], role=role,
                rows=payload['rankings'][role], replace_batch=True)
        rows = list(PortalMythicstatsDpsRow.objects.filter(season=season, dungeon_id=did, period_id=pid).order_by('role', 'rank'))
        result = _rows_payload(season, did, pid, rows, payload)
        # 先准备完整文件，数据库事务失败时该文件不会被索引引用。
        entry = _write_shard(result, 'collector')
    return entry


def collect_snapshots(*, req=None, season_hint=''):
    """定时任务和手工刷新共同使用；只采集后台发现的范围。"""
    root = snapshot_root()
    with _lock(root / 'worker.lock', blocking=False) as acquired:
        if not acquired:
            return {'busy': True, 'built': 0, 'failed': 0}
        manifest = deepcopy(_manifest())
        try:
            # 不用调用者提供的赛季给上游最新数据贴标签，以实际周次归属为准。
            base = source.fetch_mythicstats_dps(req=req, season='', dungeon_id=0, period_id=None)
            season = base.get('season') or ''
            if not season or season == 'unknown':
                raise ValueError('无法确定来源赛季，保留已发布数据')
            if source._parse_season(season_hint) not in ('', season):
                raise ValueError('配置赛季与来源当前赛季不一致，请使用 auto 或当前赛季')
            raw_periods = base.get('periods') or []
            candidates = sorted({int(p['id']): p.get('label') or str(p['id']) for p in raw_periods if _number(p.get('id'))}.items(), reverse=True)[:3]
            if not candidates or base.get('period_id') != candidates[0][0]:
                raise ValueError('来源缺少最新周次或周次不一致')
            _validate(base, season, 0, candidates[0][0])
            if not base.get('dungeons'):
                raise ValueError('来源缺少副本目录，保留已发布数据')
            dungeons = {0: {'id': 0, 'name': '全部副本'}}
            for row in base['dungeons']:
                did = _number(row.get('id'))
                if did:
                    dungeons[did] = {'id': did, 'name': str(row.get('name') or did)}
            periods = []
            for pid, label in candidates:
                # 赛季交界不能把前一赛季周次写入新赛季。
                owner = season if pid == base['period_id'] else source.fetch_period_season_slug(req=req, period_id=pid)[0]
                if not owner:
                    raise ValueError(f'无法确定周次 {pid} 的所属赛季，保留已发布数据')
                if owner == season:
                    periods.append({'id': pid, 'label': label})
        except Exception as exc:
            # 发现失败时无法信任新范围，只标记正在展示的当前赛季最新周。
            # 保留历史周、文件指针及数据库事实；下次成功采集会清除对应错误。
            current = manifest['seasons'].get(manifest['current_season'], {})
            published_periods = current.get('periods') or []
            if published_periods:
                attempted_at = timezone.now().isoformat()
                for key, entry in current.get('shards', {}).items():
                    if key.endswith(f":{published_periods[0]['id']}"):
                        entry.update(error=str(exc)[:500], attempted_at=attempted_at)
                manifest['updated_at'] = attempted_at
                _write(root / 'index.json', manifest)
            raise
        previous = manifest['seasons'].get(season, {})
        meta = {**previous, 'periods': periods, 'dungeons': list(dungeons.values()),
                'shards': dict(previous.get('shards', {}))}
        manifest['seasons'][season] = meta
        built = failed = 0
        latest_published = False
        # 优先构建最新周；历史周只补缺失或曾失败的分片，最后统一发布索引。
        for period in periods:
            for dungeon in dungeons.values():
                pid, did = period['id'], dungeon['id']
                key = f'{did}:{pid}'
                old = meta['shards'].get(key, {})
                if pid != periods[0]['id'] and old.get('origin') == 'collector' and not old.get('error') and _shard(old):
                    continue
                try:
                    payload = base if did == 0 and pid == base['period_id'] else source.fetch_mythicstats_dps(
                        req=req, season=season, dungeon_id=did, period_id=pid)
                    entry = _collect_shard(payload, season, dungeon, period)
                    meta['shards'][key] = entry
                    built += 1
                    if did == 0 and pid == periods[0]['id']:
                        latest_published = True
                except Exception as exc:
                    meta['shards'][key] = {**old, 'error': str(exc)[:500], 'attempted_at': timezone.now().isoformat()}
                    failed += 1
        if latest_published:
            manifest['current_season'] = season
        manifest['updated_at'] = timezone.now().isoformat()
        _write(root / 'index.json', manifest)
        return {'busy': False, 'season': season, 'period_id': periods[0]['id'],
                'built': built, 'failed': failed, 'latest_published': latest_published}


def publish_database_snapshots():
    """显式迁移已有数据库榜单；只在后台运行，永不由页面触发。"""
    with _lock(snapshot_root() / 'worker.lock', blocking=False) as acquired:
        if not acquired:
            raise BlockingIOError('已有采集发布任务运行，请稍后重试')
        manifest = deepcopy(_manifest())
        groups = {}
        for row in PortalMythicstatsDpsRow.objects.exclude(season__in=['unknown', 'season-mn-1']).order_by('season', 'period_id', 'dungeon_id', 'role', 'rank').iterator():
            groups.setdefault((row.season, row.dungeon_id, row.period_id), []).append(row)
        built = 0
        for (season, did, pid), rows in groups.items():
            meta = manifest['seasons'].setdefault(season, {'dungeons': [], 'periods': [], 'shards': {}})
            key = f'{did}:{pid}'
            if _shard(meta['shards'].get(key)):
                continue
            if not any(d['id'] == did for d in meta['dungeons']):
                meta['dungeons'].append({'id': did, 'name': rows[0].dungeon_name or ('全部副本' if not did else str(did))})
            if not any(p['id'] == pid for p in meta['periods']):
                meta['periods'].append({'id': pid, 'label': rows[0].period_label or str(pid)})
            metadata = source.get_mythicstats_source_cache(season=season, dungeon_id=did, period_id=pid)
            meta['shards'][key] = _write_shard(_rows_payload(season, did, pid, rows, metadata), 'database')
            meta['periods'].sort(key=lambda p: p['id'], reverse=True)
            built += 1
        if not manifest['current_season']:
            # 复用旧定时任务成功记录；不把任意历史行猜作当前赛季。
            flag = MonitorTask.objects.filter(name='PortalMythicstatsDpsMonitor').values_list('flag', flat=True).first() or ''
            recorded = flag.rsplit('@', 1)[0]
            if recorded in manifest['seasons']:
                manifest['current_season'] = recorded
        _write(snapshot_root() / 'index.json', manifest)
        return built
