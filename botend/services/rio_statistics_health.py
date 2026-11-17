"""统计缓存、每日批次、地图和导入来源的运维检查，不触发上游请求。"""
import json
import shutil

from django.db import DatabaseError
from django.conf import settings
from django.utils import timezone

from botend.services.rio_statistics_scope import active_scope, ScopeUnavailable
from botend.services.rio_statistics_storage import StorageError


def check_health(*, deep=False, initialize=False, backup=False):
    from botend.services.rio_statistics import store, root, catalog, _stamp, _load, REGIONS, LEVELS, key_for, freshness_window
    cache = store()
    if initialize:
        cache.ensure()
    if backup:
        cache.backup()
    report = cache.audit(deep=deep)
    issues = []
    if report['state'] == 'legacy':
        issues.append('缓存仍为旧文件布局，尚未迁移')
    elif report['state'] != 'ok':
        issues.append('缓存完整性检查未通过')
    try:
        scope = active_scope()
        report['scope'] = {'token': scope['token'], 'season': scope['rio_season'], 'build': scope['game_build']}
    except (DatabaseError, ScopeUnavailable) as exc:
        scope = None
        report['scope'] = None
        issues.append(str(exc) if isinstance(exc, ScopeUnavailable) else '业务数据库当前不可用')
    meta = catalog()
    if scope and (meta.get('scope') or {}).get('token') != scope['token']:
        issues.append('数据库版本与已采集目录尚未对齐')
    if scope:
        from botend.services.rio_statistics_views import audit_views
        report['views'] = audit_views(scope['token'], deep=deep)
        if report['views']['state'] != 'ok':
            issues.append('页面展示中间文件未就绪或校验失败，需要后台重建')
    missing_maps = []
    if scope:
        for slug, atlas in meta.get('maps', {}).items():
            if atlas.get('scope_token') != scope['token']:
                continue
            for floor in atlas.get('floors', {}).values():
                if not (root() / 'maps' / (floor['image_key'] + '.webp')).is_file():
                    missing_maps.append(f"{slug}:{floor['id']}")
    if missing_maps:
        issues.append('部分已登记地图文件缺失')
    report['missing_maps'] = missing_maps
    latest = report.get('last_nightly')
    if latest:
        details = json.loads(latest['details'])
        report['last_nightly']['details'] = details
        if not details.get('full_plan'):
            issues.append('最近夜间检查仅覆盖部分范围')
        if latest['state'] != 'complete':
            issues.append('最近夜间批次未完成')
        if scope and latest.get('scope') != scope['token']:
            issues.append('最近夜间批次不属于数据库当前版本')
        checked = _stamp(latest.get('finished_at')) or _stamp(latest['started_at'])
        if not checked or (timezone.now() - checked).total_seconds() > 36 * 3600:
            issues.append('夜间批次超过三十六小时未完成检查')
    else:
        issues.append('尚无已记录的真实完整夜间批次')
    folder = root()
    report['disk_bytes'] = sum(p.stat().st_size for p in folder.rglob('*') if p.is_file()) if folder.exists() else 0
    report['free_bytes'] = shutil.disk_usage(folder if folder.exists() else folder.parent).free
    report['maximum_bytes'] = int(getattr(settings, 'RIO_STATISTICS_MAX_STORAGE_BYTES', 10 * 1024 ** 3))
    report['minimum_free_bytes'] = int(getattr(settings, 'RIO_STATISTICS_MIN_FREE_BYTES', 256 * 1024 ** 2))
    if report['disk_bytes'] >= report['maximum_bytes'] or report['free_bytes'] < report['minimum_free_bytes']:
        issues.append('缓存容量或可用磁盘空间不足')
    report['cooldown_until'] = _load(root() / 'cooldown.json', {}).get('until')
    report['runs_source_mode'] = 'raiderio-statistics'
    report['runs_source_note'] = '每日采集 Raider.IO 网站统计接口（非公开 v1 稳定 API），同时保留文件导入'
    if scope:
        coverage = {'expected': len(REGIONS) * len(LEVELS), 'ready': 0, 'missing': 0, 'stale': 0, 'failed': 0}
        entries = cache.index()['entries']
        now = timezone.now()
        invalid = set(report.get('invalid_payloads', []))
        for region in REGIONS:
            for level in LEVELS:
                c = {'module': 'runs', 'season': scope['rio_season'], 'region': region, 'scope': scope['token'], 'level': level}
                key = key_for(c)
                entry = entries.get(key, {})
                fetched = _stamp(entry.get('fetched_at'))
                state = ('failed' if entry.get('error') or key in invalid else 'missing' if not fetched
                         else 'stale' if now - fetched > freshness_window(c) else 'ready')
                coverage[state] += 1
        report['runs_coverage'] = coverage
        if coverage['ready'] != coverage['expected']:
            issues.append('完成记录自动采集覆盖未齐：缺失{missing}、失败{failed}、过期{stale}（共{expected}个范围）'.format(**coverage))
    report['issues'] = issues
    broken = report['state'] == 'error' or report.get('views', {}).get('state') == 'error'
    report['status'] = 'ok' if not issues else 'error' if broken else 'warning'
    report['checked_at'] = timezone.now().isoformat()
    return report
