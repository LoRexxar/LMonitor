"""新闻轻量读模型：后台统一发布，首页及历史列表只读取文件。"""
from collections import Counter, deque
from functools import lru_cache
import hashlib
import json
import logging
from math import ceil
from pathlib import Path
import re
import threading
import time
import uuid

from bs4 import BeautifulSoup
from django.conf import settings
from django.db import close_old_connections, transaction
from django.db.models import Case, CharField, Value, When
from django.db.models.functions import Substr
from django.db.models.signals import post_delete, post_save
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.http import parse_etags

from botend.models import WowArticle
from botend.services.simc_benchmark_result_snapshot import _load, _lock, _write

logger = logging.getLogger(__name__)
SCHEMA = 1
CHUNK_SIZE = 500
SOURCE_LABELS = {
    'blizzard_cn': '魔兽世界中国', 'blizzard_tracker': 'Blizzard Tracker',
    'bilibili': 'B 站视频', 'exwind': 'Exwind 新闻', 'lhfszs': '老黄蜂说芝士',
    'nga': 'NGA', 'unknown': '其他来源', 'wowhead': 'Wowhead',
}
_thread = None
_thread_lock = threading.Lock()


def snapshot_root():
    return Path(getattr(settings, 'NEWS_SNAPSHOT_ROOT', Path(settings.BASE_DIR) / 'var/news'))


def _digest(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def lightweight_rows():
    """只在发布端读取元数据和有限摘要，绝不将正文大字段拉入 Python。"""
    fields = ('id', 'title', 'title_cn', 'url', 'author', 'source', 'category',
              'publish_time', 'reply_count', 'nga_board_id')
    return (WowArticle.objects.filter(is_active=True)
            .annotate(content_flag=Substr('content', 1, 1), translation_flag=Substr('content_cn', 1, 1),
                      preview=Case(When(source='nga', reply_count__gt=20, then=Substr('content', 1, 600)),
                                   default=Value(''), output_field=CharField()))
            .values(*fields, 'content_flag', 'translation_flag', 'preview')
            .order_by('-publish_time', '-id').iterator(chunk_size=1000))


def _project(row):
    url = (row['url'] or '').strip()
    if url in ('-', '#'):
        url = ''
    if url.startswith('/static/portal/reports/'):
        url = '/portal/reports/' + url[len('/static/portal/reports/'):]
    author = (row['author'] or '').strip()
    published = row['publish_time']
    return {
        'id': row['id'], 'title': row['title'] or '', 'title_cn': row['title_cn'] or '',
        'url': url, 'article_url': f'/portal/article/{row["id"]}/',
        'author': '' if author == 'LMonitor' else author,
        'source': row['source'] or '', 'category': row['category'] or '',
        'publish_time': timezone.localtime(published).strftime('%Y-%m-%d %H:%M:%S') if published else '',
        'reply_count': int(row['reply_count'] or 0),
        'has_content': bool(row['content_flag']), 'has_translation': bool(row['translation_flag']),
        '_published': published.timestamp() if published else None,
        '_author': row['author'] or '', '_board': row['nga_board_id'],
        '_preview': '',
    }


def _public(row, *, nga=False):
    result = {key: value for key, value in row.items() if not key.startswith('_')}
    if nga:
        result['content_preview'] = row['_preview']
    return result


def _save_content(body):
    filename = _digest(body) + '.json'
    path = snapshot_root() / filename
    # 文件被意外修改或损坏时也能由后台修复，不在公开请求中补写。
    if _load(path) != body:
        _write(path, body)
    return filename


def request_news_refresh():
    """合并保存通知；事务提交失败不会留下对应的刷新请求。"""
    def enqueue():
        try:
            _write(snapshot_root() / 'revision.json', {'revision': uuid.uuid4().hex})
        except Exception:
            logger.exception('新闻索引更新通知失败，等待周期补偿')
    transaction.on_commit(enqueue)


def register_news_signals():
    def changed(sender, **kwargs):
        request_news_refresh()
    post_save.connect(changed, sender=WowArticle, weak=False, dispatch_uid='news-snapshot-save')
    post_delete.connect(changed, sender=WowArticle, weak=False, dispatch_uid='news-snapshot-delete')


def refresh_news_snapshot(*, poll=False):
    """管理命令及后台共用；全部分片完成后才切换唯一索引。"""
    root = snapshot_root()
    with _lock(root / 'publish.lock', blocking=False) as acquired:
        if not acquired:
            return False
        revision = _load(root / 'revision.json', {})
        checked = _load(root / 'checked.json', {})
        now = time.time()
        if poll:
            if now - _load(root / 'attempt.json', {}).get('at', 0) < 15:
                return False
            if (root / 'index.json').exists() and revision == checked.get('revision') and now - checked.get('at', 0) < 300:
                return False
        _write(root / 'attempt.json', {'at': now})
        try:
            shards, chunk, month = [], [], None
            source_counts = Counter()
            home = {key: [] for key in ('blueposts', 'exwind', 'wowhead', 'nga_preview', 'nga_hot', 'nga_fallback')}
            def save_chunk():
                counts = dict(Counter(row['source'] for row in chunk))
                shards.append({'month': month, 'file': _save_content({'schema': SCHEMA, 'items': chunk}),
                               'count': len(chunk), 'sources': counts})
            for raw in lightweight_rows():
                row = _project(raw)
                flags = {
                    'blueposts': row['category'] == 'bluepost',
                    'exwind': row['source'] in ('exwind', 'blizzard_cn'),
                    'wowhead': row['source'] == 'wowhead' and row['category'] == 'news',
                    'nga_preview': row['source'] == 'nga' and (row['_board'] == '310' or
                                   row['_board'] == '' and row['category'] == 'nga' and row['_author'] == 'nga前瞻区'),
                    'nga_hot': row['source'] == 'nga' and row['category'] == 'hot' and row['reply_count'] > 20,
                    'nga_fallback': row['source'] == 'nga' and row['reply_count'] > 20,
                }
                # 仅为实际显示的热帖生成摘要，历史归档不逐篇解析 HTML。
                if any(flags[key] and len(home[key]) < 40 for key in ('nga_hot', 'nga_fallback')):
                    row['_preview'] = BeautifulSoup(raw['preview'] or '', 'html.parser').get_text(' ', strip=True)[:200]
                for key, eligible in flags.items():
                    if eligible and len(home[key]) < (40 if key in ('nga_hot', 'nga_fallback') else 60):
                        home[key].append(row)
                # 历史列表保留原先排除空标题的规则，首页仍允许来源自身的空标题条目。
                if not row['title']:
                    continue
                source_counts[row['source']] += 1
                row_month = timezone.localtime(raw['publish_time']).strftime('%Y-%m') if raw['publish_time'] else 'undated'
                if chunk and row_month != month:
                    save_chunk()
                    chunk = []
                month = row_month
                chunk.append({key: value for key, value in row.items()
                              if key not in ('_preview', '_board', '_published')})
                if len(chunk) == CHUNK_SIZE:
                    save_chunk()
                    chunk = []
            if chunk:
                save_chunk()
            body = {'schema': SCHEMA, 'shards': shards, 'sources': dict(source_counts),
                    'home': _save_content({'schema': SCHEMA, 'sections': home})}
            generation = _digest(body)
            previous = _load(root / 'index.json', {})
            if revision != _load(root / 'revision.json', {}):
                return False
            if previous.get('generation') != generation or any(previous.get(key) != value for key, value in body.items()):
                _write(root / 'index.json', {**body, 'generation': generation,
                                           'generated_at': timezone.now().isoformat()})
            _write(root / 'checked.json', {'revision': revision, 'at': now})
            _write(root / 'failure.json', {})
            return True
        except Exception:
            _write(root / 'failure.json', {'message': '新闻更新暂未完成，正在展示上次数据。'})
            raise


@lru_cache(maxsize=16)
def _cached_file(path, identity):
    body = _load(Path(path))
    if not isinstance(body, dict) or body.get('schema') != SCHEMA:
        raise ValueError('新闻数据文件不可用')
    if Path(path).name != _digest(body) + '.json':
        raise ValueError('新闻数据文件校验失败')
    return body


def _content(filename):
    if not isinstance(filename, str) or not re.fullmatch(r'[a-f0-9]{64}\.json', filename):
        raise ValueError('新闻数据文件名称无效')
    path = snapshot_root() / filename
    stat = path.stat()
    return _cached_file(str(path), (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino))


def _index():
    path = snapshot_root() / 'index.json'
    if not path.exists():
        return None
    index = _load(path)
    if not isinstance(index, dict) or index.get('schema') != SCHEMA:
        raise ValueError('新闻索引不可用')
    body = {key: index[key] for key in ('schema', 'shards', 'sources', 'home')}
    if _digest(body) != index.get('generation'):
        raise ValueError('新闻索引校验失败')
    return index


def _state(index):
    failure = _load(snapshot_root() / 'failure.json', {})
    return {'state': 'stale' if failure else 'ready', 'generation': index['generation'],
            'generated_at': index['generated_at'], 'message': failure.get('message', '')}


def _integer(value, default):
    try:
        return int(value or default)
    except (ValueError, TypeError):
        return default


def read_news_page(params):
    index = _index()
    q = (params.get('q') or '').strip()
    source = (params.get('source') or '').strip()
    exclude = (params.get('exclude_source') or '').strip()
    page = max(1, _integer(params.get('page'), 1))
    size = max(10, min(60, _integer(params.get('page_size'), 30)))
    keyword = q.casefold()
    base = {'status': 'success', 'data': [], 'sources': [],
            'meta': {'q': q, 'source': source, 'page': 1, 'page_size': size, 'total': 0,
                     'total_pages': 1, 'has_next': False, 'has_previous': False}}
    if index is None:
        return {**base, 'snapshot': {'state': 'pending', 'message': '新闻数据正在准备，请稍后重试。'}}
    def count(shard):
        counts = shard['sources']
        if source:
            return counts.get(source, 0)
        return shard['count'] - counts.get(exclude, 0) if exclude else shard['count']
    def matches(row):
        if (source and row['source'] != source) or (not source and exclude and row['source'] == exclude):
            return False
        return not keyword or any(keyword in str(row[key]).casefold()
                            for key in ('title', 'title_cn', '_author', 'source', 'category'))
    # 无关键词时用分片计数跳过无关历史，仅读取当前页；关键词只搜索轻量字段。
    if q:
        rows, total, tail = [], 0, deque(maxlen=size)
        requested_offset = (page - 1) * size
        for shard in index['shards']:
            if not count(shard):
                continue
            for row in _content(shard['file'])['items']:
                if not matches(row):
                    continue
                if requested_offset <= total < requested_offset + size:
                    rows.append(row)
                tail.append(row)
                total += 1
    else:
        total = sum(count(shard) for shard in index['shards'])
    pages = max(1, ceil(total / size))
    requested_page = page
    page = min(page, pages)
    offset = (page - 1) * size
    if q:
        if requested_page > pages:
            rows = list(tail)[-(total - offset):] if total else []
    else:
        rows = []
        for shard in index['shards']:
            amount = count(shard)
            if offset >= amount:
                offset -= amount
                continue
            candidates = [row for row in _content(shard['file'])['items'] if matches(row)]
            rows.extend(candidates[offset:offset + size - len(rows)])
            offset = 0
            if len(rows) == size:
                break
    sources = []
    for key, amount in sorted(index['sources'].items()):
        normalized = (key or 'unknown').strip() or 'unknown'
        sources.append({'key': key or 'unknown', 'label': SOURCE_LABELS.get(normalized, normalized), 'count': amount})
    return {**base, 'data': [_public(row) for row in rows], 'sources': sources,
            'meta': {**base['meta'], 'page': page, 'total': total, 'total_pages': pages,
                     'has_next': page < pages, 'has_previous': page > 1}, 'snapshot': _state(index)}


def read_home_news(section):
    index = _index()
    if index is None:
        return {'status': 'success', 'data': [],
                'snapshot': {'state': 'pending', 'message': '新闻数据正在准备，请稍后重试。'}}
    sections = _content(index['home'])['sections']
    nga = section == 'nga'
    rows = (sections['nga_hot'] or sections['nga_fallback']) if nga else sections[section]
    if section in ('blueposts', 'exwind', 'wowhead'):
        since = timezone.now().timestamp() - 7 * 86400
        rows = [row for row in rows if row['_published'] is not None and row['_published'] >= since]
    return {'status': 'success', 'data': [_public(row, nga=nga) for row in rows], 'snapshot': _state(index)}


def news_response(request, section=None):
    try:
        payload = read_home_news(section) if section else read_news_page(request.GET)
    except (OSError, ValueError, KeyError, TypeError):
        logger.warning('新闻已发布文件不可用', exc_info=True)
        response = JsonResponse({'status': 'error', 'message': '新闻数据暂不可用，请稍后重试。'}, status=503)
        response['Cache-Control'] = 'no-store'
        return response
    pending = payload['snapshot']['state'] == 'pending'
    etag = '"' + _digest(payload) + '"'
    tags = [tag.removeprefix('W/') for tag in parse_etags(request.headers.get('If-None-Match', ''))]
    response = HttpResponse(status=304) if not pending and (etag in tags or '*' in tags) else JsonResponse(payload, status=202 if pending else 200)
    response['ETag'] = etag
    response['Cache-Control'] = 'no-store' if pending else 'public, max-age=60'
    if pending:
        response['Retry-After'] = '15'
    return response


def start_news_snapshot_worker():
    """监控进程维护，公开页面不会启动构建或查询数据库。"""
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        def run():
            while True:
                close_old_connections()
                try:
                    refresh_news_snapshot(poll=True)
                except Exception:
                    logger.exception('新闻轻量索引维护失败')
                finally:
                    close_old_connections()
                time.sleep(15)
        _thread = threading.Thread(target=run, name='news-snapshot', daemon=True)
        _thread.start()
