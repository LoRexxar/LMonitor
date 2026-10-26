"""职业攻略私有文件快照：后台编译，前台只读，整批原子发布。"""
from functools import lru_cache
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlencode
import uuid

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import close_old_connections, transaction
from django.db.models.signals import m2m_changed, post_delete, post_save
from django.template.loader import render_to_string
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.html import strip_tags

from botend.constants.wow import CLASS_CN, CLASS_COLOR, SPEC_ROLE, specialization_catalog
from botend.guide_models import ClassGuide, ClassGuideTag
from botend.services.class_guide_authors import author_profile_for
from botend.services.class_guide_content import resolve_references, safe_url, walk_blocks
from botend.services.class_guide_render import render_blocks
from botend.services.class_guide_tags import guide_disclaimers
from botend.services.class_guide_tools import gear_tool_url
from botend.services.simc_benchmark_result_snapshot import _load, _lock, _write

logger = logging.getLogger(__name__)
SCHEMA = 3
_thread = None
_thread_lock = threading.Lock()


def snapshot_root():
    return Path(getattr(settings, 'CLASS_GUIDE_SNAPSHOT_ROOT', Path(settings.BASE_DIR) / 'var/class-guides'))


def _digest(value):
    return hashlib.sha256(json.dumps(value, cls=DjangoJSONEncoder, ensure_ascii=False,
                                    sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def request_guide_refresh():
    """只写提交后的刷新标记；回滚不会发布未提交内容。"""
    def enqueue():
        try:
            _write(snapshot_root() / 'revision.json', {'revision': uuid.uuid4().hex})
        except Exception:
            logger.exception('攻略快照刷新通知失败，等待周期补偿')
    transaction.on_commit(enqueue)


def register_guide_signals():
    def changed(sender, **kwargs):
        request_guide_refresh()
    def tags_changed(sender, action, **kwargs):
        if action in ('post_add', 'post_remove', 'post_clear'):
            request_guide_refresh()
    for model in (ClassGuide, ClassGuideTag):
        post_save.connect(changed, sender=model, weak=False, dispatch_uid=f'guide-snapshot-save-{model.__name__}')
        post_delete.connect(changed, sender=model, weak=False, dispatch_uid=f'guide-snapshot-delete-{model.__name__}')
    m2m_changed.connect(tags_changed, sender=ClassGuide.tags.through, weak=False, dispatch_uid='guide-snapshot-tags')


def article_updated(guide):
    if guide.source_url or guide.source_modified:
        try:
            return parse_date(guide.source_modified[:10])
        except ValueError:
            return None
    return guide.updated_at


def _summary(guide, specs):
    updated = article_updated(guide)
    return {
        'guide': {key: getattr(guide, key) for key in ('id', 'title', 'spec_id', 'class_name', 'spec_name', 'source_url')},
        'title': guide.title, 'spec': specs[guide.spec_id],
        'author_name': author_profile_for(guide)['name'],
        'tags': [{'name': tag.name} for tag in guide.tags.all()],
        'cover': safe_url(guide.source_payload.get('featuredImage', '')),
        'updated': updated.isoformat() if updated else None,
        'role': SPEC_ROLE[(guide.class_name, guide.spec_name)],
    }


def _article(guide, row):
    """正文仅编译一次；原文审核属于管理端，不参与公开内容发布。"""
    blocks = guide.blocks
    refs = resolve_references(blocks, guide.game_version, guide.class_name, guide.spec_name,
                              (guide.check_data or {}).get('source_refs'))
    blocks = render_blocks(blocks, refs, guide)
    from botend.services.class_guide_presentation import guide_reading_structure
    chapters, toc_tree = guide_reading_structure(blocks)
    gear_url = next((b['data']['tool_url'] for b in walk_blocks(blocks)
                     if b['type'] == 'gear' and b.get('data', {}).get('tool_url')), None)
    query = urlencode({'class': guide.class_name, 'spec': guide.spec_name, 'version': guide.game_version})
    return {
        'schema': SCHEMA, 'title': guide.title, 'guide': {**row['guide'], 'tags': {'all': row['tags']}},
        'spec': row['spec'], 'author_profile': author_profile_for(guide),
        'disclaimers': guide_disclaimers(guide), 'updated': row['updated'], 'cover': row['cover'],
        'article_html': render_to_string('portal/class_guide_body.html', {'chapters': chapters}),
        'toc_tree': toc_tree,
        'toc': [{'id': b['id'], 'title': strip_tags(b['title']), 'level': b['data']['level']}
                for b in walk_blocks(blocks) if b['type'] == 'heading'],
        'read_minutes': max(1, len(re.sub(r'\s', '', strip_tags(guide.content_markdown))) // 500),
        'talent_url': '/portal/talents/?' + query,
        'gear_url': gear_url or gear_tool_url({}, guide, {}),
    }


def _save_content(body):
    filename = _digest(body) + '.json'
    path = snapshot_root() / filename
    if _load(path) != body:
        _write(path, body)
    return filename


def _save_html(body):
    """发布原始 UTF-8 字节；不把 HTML 包进 JSON，也不在请求端解码。"""
    filename = hashlib.sha256(body).hexdigest() + '.html'
    path = snapshot_root() / filename
    if not path.exists() or path.read_bytes() != body:
        pending = path.with_suffix('.pending')
        with pending.open('wb') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
        if os.name != 'nt':
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    return filename


def _save_article(article):
    article = dict(article)
    article['body_file'] = _save_html(article.pop('article_html').encode('utf-8'))
    return _save_content(article)


def _publish_page(template, context, header):
    # 随机标记仅存在于构建过程，最终文件记录字节偏移，避免与正文内容混淆。
    header_slot, management_slot = uuid.uuid4().hex, uuid.uuid4().hex
    markup = render_to_string(template, {**context, 'snapshot_header_slot': header_slot,
                                         'snapshot_management_slot': management_slot}).encode('utf-8')
    before, remaining = markup.split(header_slot.encode())
    middle, after = remaining.split(management_slot.encode())
    body = before + header + middle + after
    return {'html': _save_html(body), 'header_start': len(before), 'header_end': len(before) + len(header),
            'management_offset': len(before) + len(header) + len(middle)}


def refresh_guide_snapshot(*, poll=False):
    """变更后增量编译，五分钟补偿无信号写入，每小时刷新引用资料。"""
    root = snapshot_root()
    with _lock(root / 'publish.lock', blocking=False) as acquired:
        if not acquired:
            return False
        now = time.time()
        revision = _load(root / 'revision.json', {})
        checked = _load(root / 'checked.json', {})
        previous = _load(root / 'index.json', {})
        if poll:
            if now - _load(root / 'attempt.json', {}).get('at', 0) < 15:
                return False
            if (previous.get('schema') == SCHEMA and revision == checked.get('revision')
                    and now - checked.get('at', 0) < 300):
                return False
        _write(root / 'attempt.json', {'at': now})
        full = not poll or previous.get('schema') != SCHEMA or now - checked.get('full_at', 0) >= 3600
        try:
            specs = {s['spec_id']: s for s in specialization_catalog()}
            rows, articles = [], {}
            query = (ClassGuide.objects.filter(archived=False).defer('source_markdown')
                     .prefetch_related('tags').order_by('class_name', 'spec_name', 'title', 'id'))
            for guide in query.iterator(chunk_size=100):
                row = _summary(guide, specs)
                rows.append(row)
                fingerprint = _digest({'row': row, 'body': guide.content_markdown, 'version': guide.game_version,
                                       'refs': (guide.check_data or {}).get('source_refs'),
                                       'author': author_profile_for(guide), 'disclaimers': guide_disclaimers(guide)})
                old = previous.get('articles', {}).get(str(guide.pk), {})
                filename = old.get('file')
                if not full and old.get('source') == fingerprint:
                    try:
                        _html_content(_content(filename)['body_file'])
                    except (OSError, ValueError, KeyError, TypeError):
                        filename = None
                else:
                    filename = None
                if filename is None:
                    filename = _save_article(_article(guide, row))
                articles[str(guide.pk)] = {'source': fingerprint, 'file': filename}
            catalog = {'schema': SCHEMA, 'rows': rows, 'specializations': list(specs.values()),
                       'classes': [{'key': key, 'label': label, 'color': CLASS_COLOR[key],
                                    'icon': f'https://render.worldofwarcraft.com/us/icons/56/classicon_{key.lower()}.jpg',
                                    'count': sum(row['guide']['class_name'] == key for row in rows)}
                                   for key, label in CLASS_CN.items()],
                       'tags': sorted({tag['name'] for row in rows for tag in row['tags']}),
                       'roles': [['tank', '坦克'], ['healer', '治疗'], ['dps', '输出']], 'title': '职业攻略'}
            index = {'schema': SCHEMA, 'catalog': _save_content(catalog), 'articles': articles}
            # 整页在后台渲染，公开请求只读一个 HTML 文件及轻量发布索引。
            header = render_to_string('portal/_header.html').encode('utf-8')
            index['catalog_page'] = _publish_page('portal/class_guides.html', _catalog_context(catalog), header)
            for entry in articles.values():
                context = _article_context(_content(entry['file']), catalog)
                entry.update(_publish_page('portal/class_guide_article.html', context, header))
            # 构建中收到编辑通知时整批放弃，避免较旧内容覆盖更新。
            if revision != _load(root / 'revision.json', {}):
                return False
            if index != previous:
                _write(root / 'index.json', index)
            _write(root / 'checked.json', {'revision': revision, 'at': now,
                                         'full_at': now if full else checked.get('full_at', 0)})
            return True
        except Exception:
            logger.exception('攻略快照构建失败，保留上次发布内容')
            raise


@lru_cache(maxsize=64)
def _cached_content(path, identity):
    body = _load(Path(path))
    if not isinstance(body, dict) or body.get('schema') != SCHEMA or Path(path).name != _digest(body) + '.json':
        raise ValueError('攻略快照校验失败')
    return body


def _content(filename):
    if not isinstance(filename, str) or not re.fullmatch(r'[a-f0-9]{64}\.json', filename):
        raise ValueError('攻略快照文件名无效')
    path = snapshot_root() / filename
    stat = path.stat()
    return _cached_content(str(path), (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino))


def _date(value):
    return (parse_datetime(value) or parse_date(value)) if value else None


@lru_cache(maxsize=64)
def _cached_html(path, identity):
    body = Path(path).read_bytes()
    if Path(path).name != hashlib.sha256(body).hexdigest() + '.html':
        raise ValueError('攻略 HTML 校验失败')
    return body


def _html_content(filename):
    if not isinstance(filename, str) or not re.fullmatch(r'[a-f0-9]{64}\.html', filename):
        raise ValueError('攻略 HTML 文件名无效')
    path = snapshot_root() / filename
    stat = path.stat()
    return _cached_html(str(path), (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino))


def _catalog_context(catalog):
    return {**catalog, 'rows': [{**row, 'updated': _date(row['updated'])} for row in catalog['rows']]}


def _article_context(article, catalog):
    return {**article, 'guide': SimpleNamespace(**article['guide']), 'updated': _date(article['updated']),
            'article_html': _html_content(article['body_file']).decode('utf-8'),
            'related': sorted((row for row in _catalog_context(catalog)['rows']
                               if row['guide']['spec_id'] == article['guide']['spec_id']), key=lambda row: row['title'])}


def read_guide_page(guide_id=None):
    """冷访问也只读 HTML 字节；不读文章 JSON、不渲染模板、不访问数据源。"""
    index = _load(snapshot_root() / 'index.json')
    if not isinstance(index, dict) or index.get('schema') != SCHEMA:
        raise ValueError('攻略内容正在准备，请稍后重试')
    entry = index['catalog_page'] if guide_id is None else index['articles'].get(str(guide_id))
    if entry is None:
        return None
    body = _html_content(entry['html'])
    start, end, offset = entry['header_start'], entry['header_end'], entry['management_offset']
    if not all(type(value) is int for value in (start, end, offset)) or not 0 <= start <= end <= offset <= len(body):
        raise ValueError('攻略页面片段边界无效')
    return body, (start, end, offset)


def read_guide_snapshot(guide_id=None):
    """构建核验用的结构化读取；公开页面使用 read_guide_page 直读 HTML。"""
    index = _load(snapshot_root() / 'index.json')
    if not isinstance(index, dict) or index.get('schema') != SCHEMA:
        raise ValueError('攻略内容正在准备，请稍后重试')
    catalog = _content(index.get('catalog'))
    if guide_id is None:
        return _catalog_context(catalog)
    entry = index['articles'].get(str(guide_id))
    if entry is None:
        return None
    article = _content(entry['file'])
    return _article_context(article, catalog)


def start_guide_snapshot_worker(*, lifecycle=None):
    """仅由监控进程启动，与其他公共数据快照保持一致。"""
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return _thread
        from botend.backend_lifecycle import worker_cycles
        def run():
            for _ in worker_cycles(lifecycle, interval=15, label='class-guide-snapshot'):
                close_old_connections()
                try:
                    refresh_guide_snapshot(poll=True)
                except Exception:
                    logger.exception('攻略快照维护失败')
                finally:
                    close_old_connections()
        _thread = threading.Thread(target=run, name='class-guide-snapshot', daemon=True)
        _thread.start()
        return _thread
