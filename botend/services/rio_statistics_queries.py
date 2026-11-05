"""有限筛选订阅：只登记本地意向，由凌晨任务处理，不在访客请求中回源。"""
from datetime import timedelta
import hashlib
import hmac

from django.conf import settings
from django.utils import timezone

from botend.services.simc_benchmark_result_snapshot import _lock, _load

MAX_QUERIES = 128
MAX_NEW_PER_DAY = 12


def subscribe(coordinate, *, client=''):
    from botend.services.rio_statistics import root, _write, key_for, _stamp, active_scope
    now = timezone.now()
    identity = key_for(coordinate)
    owner = hmac.new(settings.SECRET_KEY.encode(), str(client).encode(), hashlib.sha256).hexdigest()
    path = root() / 'queries.json'
    with _lock(root() / 'queries.lock'):
        if active_scope()['token'] != coordinate.get('scope'):
            raise ValueError('赛季版本已变化，请刷新页面后重新选择。')
        data = _load(path, {})
        rows = {k: v for k, v in data.get('queries', {}).items()
                if _stamp(v.get('requested_at')) and _stamp(v['requested_at']) > now - timedelta(days=31)
                and v.get('coordinate', {}).get('scope') == coordinate.get('scope')}
        if identity not in rows:
            if len(rows) >= MAX_QUERIES:
                raise ValueError('待更新范围较多，请稍后再试。')
            recent = sum(1 for v in rows.values() if v.get('owner') == owner
                         and _stamp(v['requested_at']) > now - timedelta(days=1))
            if recent >= MAX_NEW_PER_DAY:
                raise ValueError('今天新增的筛选范围较多，请明天再试。')
            rows[identity] = {'coordinate': coordinate, 'owner': owner, 'requested_at': now.isoformat()}
        else:
            rows[identity]['requested_at'] = now.isoformat()
        _write(path, {'queries': rows})
    return {'state': 'queued', 'message': '已加入凌晨更新。'}


def pending_queries(meta):
    from botend.services.rio_statistics import root, coordinate, _stamp
    rows = _load(root() / 'queries.json', {}).get('queries', {})
    now = timezone.now()
    result = []
    for row in sorted(rows.values(), key=lambda v: v.get('requested_at', '')):
        saved = row.get('coordinate', {})
        requested = _stamp(row.get('requested_at'))
        if (not requested or requested <= now - timedelta(days=31)
                or saved.get('scope') != (meta.get('scope') or {}).get('token')):
            continue
        # 自选周保留真实编号；本周、最近完整周等相对条件随重置更新身份。
        params = {k: v for k, v in saved.items() if k not in ('scope', 'module', 'filters', 'period_id', 'min_period', 'max_period')}
        params.update(saved.get('filters', {}))
        try:
            checked = coordinate(saved['module'], params, meta=meta)
        except (ValueError, KeyError, TypeError):
            continue
        if saved.get('period') == 'custom':
            if checked.get('min_period') != saved.get('min_period') or checked.get('max_period') != saved.get('max_period'):
                continue
        result.append(checked)
    return result[:MAX_QUERIES]
