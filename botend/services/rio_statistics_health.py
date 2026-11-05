"""统计缓存、每日批次、地图和导入来源的运维检查，不触发上游请求。"""
import json
import shutil

from django.db import DatabaseError
from django.conf import settings
from django.utils import timezone

from botend.services.rio_statistics_scope import active_scope, ScopeUnavailable
from botend.services.rio_statistics_storage import StorageError


def check_health(*, deep=False, initialize=False, backup=False):
    from botend.services.rio_statistics import store, root, catalog, _stamp, _load
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
    report['runs_source_mode'] = 'file-import'
    report['runs_source_note'] = '完成记录只维护导入响应；自动获取上游统计的来源尚未配置'
    issues.append('完成记录尚未配置自动上游来源，无法保证每日产生新统计')
    report['issues'] = issues
    broken = report['state'] == 'error' or report.get('views', {}).get('state') == 'error'
    report['status'] = 'ok' if not issues else 'error' if broken else 'warning'
    report['checked_at'] = timezone.now().isoformat()
    return report
