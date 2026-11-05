"""完成记录聚合响应的严格导入，不自动抓取未公开统计端点。"""
import hashlib
import json
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from botend.services.rio_statistics_dimensions import integer, week_options


class StatisticsPending(ValueError):
    pass


def normalize_runs(source, meta):
    from botend.services.rio_statistics import LEVELS, coordinate, _stamp, number
    if not isinstance(source, dict) or source.get('type') != 'runs-over-time':
        raise ValueError('不是完成记录统计响应')
    if source.get('status') == 'generating':
        raise StatisticsPending('来源正在生成统计，已有数据保持不变')
    if source.get('status') != 'ready' or not isinstance(source.get('data'), list):
        raise ValueError('来源统计尚未就绪')
    query = source.get('query', {})
    allowed = {'type', 'season', 'region', 'groupBy', 'minMythicLevel', 'maxMythicLevel',
               'seasonWeekStart', 'seasonWeekEnd', 'version', 'force', 'uniqueCharacters', 'timedOnly', 'viewTypePosition'}
    if (not isinstance(query, dict) or set(query) - allowed or query.get('type') != 'runs-over-time'
            or query.get('groupBy') != 'period' or query.get('uniqueCharacters') is not False
            or query.get('timedOnly') is not False or query.get('version') != 4
            or query.get('season') != meta.get('current_season')):
        raise ValueError('来源分组、去重或统计版本与完成记录口径不符')
    low = integer(query.get('minMythicLevel'), 2, 99)
    high = integer(query.get('maxMythicLevel'), low, 99)
    level = next((key for key, value in LEVELS.items() if value[:2] == (low, high)), 'custom')
    params = {'season': query.get('season'), 'region': query.get('region') or 'world',
              'level': level, 'min_level': low, 'max_level': high}
    c = coordinate('runs', params, meta=meta)
    weeks = week_options(meta, c['region'])
    if not weeks:
        raise ValueError('赛季周资料尚未就绪')
    first_week = integer(query.get('seasonWeekStart'), 1, len(weeks))
    last_week = integer(query.get('seasonWeekEnd'), first_week, len(weeks))
    by_period = {w['period']: w for w in weeks[first_week - 1:last_week]}
    rows = []
    seen = set()
    for row in source['data']:
        period = integer(row.get('period'), 1, 99999)
        if period not in by_period or period in seen:
            raise ValueError('来源周编号越界或重复')
        seen.add(period)
        total, timed, non_timed = (number(row.get(k)) for k in ('quantity', 'timedRuns', 'nonTimedRuns'))
        if any(v != int(v) for v in (total, timed, non_timed)) or total != timed + non_timed:
            raise ValueError('完成次数与限时、非限时次数不一致')
        week = by_period[period]
        rows.append({'period': period, 'week': int(week['value']), 'date': week['date'],
                     'start': week['start'], 'end': week['end'], 'label': week['label'],
                     'quantity': int(total), 'timed': int(timed), 'non_timed': int(non_timed),
                     'timed_rate': timed / total * 100 if total else None})
    updated = _stamp(source.get('aggregated_at'))
    if not updated or updated > timezone.now() + timedelta(minutes=10):
        raise ValueError('来源聚合时间无效')
    payload = {'rows': sorted(rows, key=lambda r: r['period']), 'coverage': {'first_week': first_week, 'last_week': last_week},
               'source_kind': 'imported-statistics', 'min_level': low, 'max_level': high}
    return c, payload, updated.isoformat()


def inbox():
    from botend.services.rio_statistics import root
    return Path(getattr(settings, 'RIO_STATISTICS_RUNS_SOURCE_DIR', root() / 'imports' / 'runs'))


def import_file(path, meta, *, retain=True):
    from botend.services.rio_statistics import _publish, _write
    path = Path(path)
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('来源文件过大')
    raw = path.read_bytes()
    source = json.loads(raw)
    c, payload, updated = normalize_runs(source, meta)
    # 发布前再次确认数据库版本，导入也不能绕过范围门禁。
    from botend.services.rio_statistics_scope import active_scope
    if active_scope()['token'] != c['scope']:
        raise ValueError('导入期间数据库赛季版本发生变化')
    if retain:
        _write(inbox() / (hashlib.sha256(raw).hexdigest() + '.json'), source)
    return _publish(c, payload, upstream_at=updated, newer_only=True)


def import_feeds(meta):
    result = {'imported': 0, 'failed': 0, 'pending': 0, 'skipped': 0}
    directory = inbox()
    if not directory.is_dir():
        return result
    files = sorted(directory.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:50]
    for path in files:
        try:
            changed = import_file(path, meta, retain=False)
            result['imported' if changed else 'skipped'] += 1
        except StatisticsPending:
            result['pending'] += 1
        except (ValueError, KeyError, TypeError, OSError):
            result['failed'] += 1
    return result
