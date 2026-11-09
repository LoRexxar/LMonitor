"""只读日报投影：复用已公开 RIO 事实，日报只持久化中央快照引用。"""
from datetime import date, datetime, timedelta
import hashlib
import json
import logging
from urllib.parse import urlencode

from django.urls import reverse

from botend.services import rio_statistics as rio
from botend.services import rio_statistics_views
from botend.services.rio_statistics_storage import LOCAL_TZ, SCHEMA, StorageError, encoded, epoch, key_for

logger = logging.getLogger(__name__)
NOTICE = '来源 Raider.IO；统计有赛季分数的角色，非独立玩家，国服与全球不相加。变化比较数据日前一日及7日前，缺失不补零。'
WEEKLY_NOTICE = '来源 Raider.IO；统计有赛季分数的角色，非独立玩家，国服与全球不相加。按北京时间上周三至本周二完整7天观察角色数量净变化，非新增玩家；窗口缺失不补零、不以邻日代替。'


def _date(value):
    if not isinstance(value, str):
        raise StorageError('日报统计历史日期不合法')
    try:
        result = date.fromisoformat(value)
    except ValueError:
        raise StorageError('日报统计历史日期不合法') from None
    if result.isoformat() != value:
        raise StorageError('日报统计历史日期不规范')
    return result


def _history(body):
    """只接受中央原始历史；不从当前 populations 拼接或合计任何地区。"""
    c, data = body['coordinate'], body['data']
    if data.get('season') != c['season'] or data.get('region') != c['region']:
        raise StorageError('日报统计历史身份不匹配')
    rows = data.get('history')
    if not isinstance(rows, list):
        raise StorageError('日报统计历史结构不合法')
    values = {}
    for row in rows:
        if not isinstance(row, dict):
            raise StorageError('日报统计历史行不合法')
        day = _date(row.get('date'))
        if day in values:
            raise StorageError('日报统计历史日期重复')
        if any(field in row and row[field] != c[field] for field in ('season', 'region')):
            raise StorageError('日报统计历史行身份不匹配')
        population = row.get('population')
        values[day] = population if type(population) is int and population >= 0 else None
    return values


def _item(body, reference, day, *, weekly=False):
    values = _history(body)
    source_day = _date(reference['data_date'])
    population = values.get(source_day)
    if source_day > day or population is None:
        raise StorageError('日报统计引用的来源日期没有有效历史值')
    previous, week = source_day - timedelta(days=1), source_day - timedelta(days=7)
    c = reference['coordinate']
    path = reverse('portal_rio_statistics')
    def url(tab):
        return path + '?' + urlencode({'tab': tab, 'season': c['season'], 'region': c['region'], 'days': 14})
    item = {'region': c['region'], 'region_label': rio.REGIONS[c['region']],
        'season': c['season'], 'season_name': reference['season_name'],
        'data_date': source_day.isoformat(), 'population': population,
        'previous_date': previous.isoformat(),
        'day_delta': population - values[previous] if values.get(previous) is not None else None,
        'week_date': week.isoformat(),
        'week_delta': population - values[week] if values.get(week) is not None else None,
        'fetched_at': body['fetched_at'], 'upstream_at': body['upstream_at'],
        'stale': reference['stale'], 'url': url('overview'), 'heatmap_url': url('heatmaps'),
        'reference': reference}
    if weekly:
        from .rio_weekly import project_weekly
        item['weekly'] = project_weekly(body, day)
    return item


def _current_reference(region, day, *, weekly=False):
    envelope = json.loads(rio_statistics_views.read_public('overview', {'region': region}).raw)['data']
    snapshot = envelope.get('snapshot') or {}
    if snapshot.get('state') not in ('ready', 'stale') or not isinstance(envelope.get('data'), dict):
        return None
    c, scope = envelope.get('coordinate'), envelope.get('scope') or {}
    if (not isinstance(c, dict) or c.get('module') != 'overview' or c.get('region') != region
            or c.get('scope') != scope.get('token') or c.get('season') != scope.get('rio_season')):
        raise StorageError('日报公开统计身份不匹配')
    body = {'schema': SCHEMA, 'coordinate': c, 'fetched_at': snapshot.get('fetched_at'),
        'upstream_at': snapshot.get('upstream_at'), 'data': envelope['data']}
    values = _history(body)
    eligible = [d for d, population in values.items() if d <= day and population is not None]
    if not eligible:
        return None
    fetched = body['fetched_at']
    if not isinstance(fetched, str) or epoch(fetched) is None:
        raise StorageError('日报公开统计采集时间不合法')
    reference = {'kind': 'rio_statistics_snapshot', 'coordinate': c, 'key': key_for(c),
        'digest': hashlib.sha256(encoded(body)).hexdigest(),
        'archive_day': datetime.fromisoformat(fetched.replace('Z', '+00:00')).astimezone(LOCAL_TZ).date().isoformat(),
        'data_date': max(eligible).isoformat(), 'season_name': scope.get('season_name'),
        'stale': snapshot['state'] == 'stale'}
    # 发布文件和中央写入可能交错；必须先证明这一精确源仍在中央，而非读取 private 新值。
    return _item(rio.store().read_reference(reference), reference, day, weekly=weekly)


def collect_rio_section(day, published_ext=None, *, weekly=False):
    """首次只读公开 Service；已发布栏目只认冻结引用，缺失则阻止覆盖旧 HTML。"""
    if weekly and day.weekday() != 1:
        raise ValueError('周三特刊的内容日期必须为周二')
    result = {'key': 'rio', 'title': '大秘境一周观察' if weekly else '大秘境参与观察',
        'notice': WEEKLY_NOTICE if weekly else NOTICE, 'items': []}
    sections = published_ext.get('sections', {}) if isinstance(published_ext, dict) else {}
    if isinstance(sections, dict) and 'rio' in sections:
        section = sections['rio']
        references = section.get('references') if isinstance(section, dict) else None
        if (not isinstance(references, list) or len(references) > 2
                or ('count' in section and (type(section['count']) is not int or section['count'] != len(references)))):
            raise StorageError('日报已发布统计引用结构不合法')
        seen = set()
        for reference in references:
            body = rio.store().read_reference(reference)
            region = reference['coordinate']['region']
            if region in seen:
                raise StorageError('日报已发布统计引用地区重复')
            seen.add(region)
            result['items'].append(_item(body, reference, day, weekly=weekly))
        return result
    for region in ('cn', 'world'):
        try:
            item = _current_reference(region, day, weekly=weekly)
        except (StorageError, OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            # 不输出 payload、路径或上游错误文本；首次竞态/缺资料可以没有栏目事实。
            logger.warning('日报 RIO 投影跳过 region=%s reason=%s', region, type(exc).__name__)
            continue
        if item is not None:
            result['items'].append(item)
    return result
