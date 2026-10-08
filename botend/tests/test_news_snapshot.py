"""验证新闻统一发布、原列表契约、轻量读取与更新边界。"""
from datetime import timedelta
from io import StringIO
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from bs4 import BeautifulSoup

from django.conf import settings
from django.core.management import call_command
from django.db import connection, transaction
from django.db.models import Q
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from botend.models import WowArticle
from botend.portal.api import (PortalBluepostsAPIView, PortalExwindLatestAPIView,
                              PortalNgaHotAPIView, PortalNewsIndexAPIView, PortalWowheadLatestAPIView)
from botend.services import news_snapshot as ns


class NewsSnapshotTests(TestCase):
    def setUp(self):
        parent = Path(settings.BASE_DIR) / '.cache/news-tests'
        parent.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=parent))
        self.override = self.settings(NEWS_SNAPSHOT_ROOT=self.root)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.factory = RequestFactory()
        self.now = timezone.now()
        for index in range(36):
            source = ['wowhead', 'nga', 'exwind', 'blizzard_cn'][index % 4]
            WowArticle.objects.create(
                title=f'新闻 Alpha {index}', title_cn=f'中文 测试 {index}', author='作者 LMonitor' if index % 3 else 'LMonitor',
                source=source, category='hot' if source == 'nga' else 'bluepost' if index % 5 == 0 else 'news',
                publish_time=self.now - timedelta(days=index % 12, seconds=index), reply_count=index,
                nga_board_id='310' if source == 'nga' else '',
                url=f'https://example.invalid/news/{index}', content='<p>主楼 [b]内容[/b]</p>' + '大正文' * 1000,
                content_cn='已翻译' if index % 2 else '', content_blocks='大结构正文' * 1000,
            )
        WowArticle.objects.create(title='停用新闻', source='wowhead', is_active=False)
        self.empty = WowArticle.objects.create(title='', source='exwind')
        self.null_time = WowArticle.objects.create(title='没有时间', publish_time=None, source='wowhead', category='news')

    def response(self, view, query=None, headers=None):
        return view.as_view()(self.factory.get('/', query or {}, headers=headers))

    @staticmethod
    def payload(response):
        return json.loads(response.content)

    def public_row(self, row):
        url = (row.url or '').strip()
        if url in ('-', '#'):
            url = ''
        if url.startswith('/static/portal/reports/'):
            url = '/portal/reports/' + url[len('/static/portal/reports/'):]
        author = (row.author or '').strip()
        return {'id': row.id, 'title': row.title or '', 'title_cn': row.title_cn or '', 'url': url,
                'article_url': f'/portal/article/{row.id}/', 'author': '' if author == 'LMonitor' else author,
                'source': row.source or '', 'category': row.category or '',
                'publish_time': timezone.localtime(row.publish_time).strftime('%Y-%m-%d %H:%M:%S') if row.publish_time else '',
                'reply_count': row.reply_count or 0, 'has_content': bool(row.content), 'has_translation': bool(row.content_cn)}

    def test_home_fields_match_database_and_all_public_reads_use_zero_sql(self):
        ns.refresh_news_snapshot()
        since = timezone.now() - timedelta(days=7)
        base = WowArticle.objects.filter(is_active=True)
        examples = [
            (PortalBluepostsAPIView, {}, base.filter(category='bluepost', publish_time__gte=since)),
            (PortalWowheadLatestAPIView, {}, base.filter(source='wowhead', category='news', publish_time__gte=since)),
            (PortalExwindLatestAPIView, {}, base.filter(source__in=['exwind', 'blizzard_cn'], publish_time__gte=since)),
            (PortalExwindLatestAPIView, {'source': 'nga_preview'}, base.filter(source='nga').filter(
                Q(nga_board_id='310') | Q(nga_board_id='', category='nga', author='nga前瞻区'))),
        ]
        for view, params, query in examples:
            expected = [self.public_row(row) for row in query.order_by('-publish_time', '-id')[:60]]
            for _ in range(2):
                with self.assertNumQueries(0), patch.object(ns, '_write') as write:
                    response = self.response(view, params)
                    actual = self.payload(response)
                write.assert_not_called()
                self.assertEqual(actual['data'], expected)
                self.assertEqual(actual['snapshot']['state'], 'ready')
        with self.assertNumQueries(0):
            payload = self.payload(self.response(PortalNgaHotAPIView))
        self.assertTrue(payload['data'])
        self.assertTrue(all(row['source'] == 'nga' and row['reply_count'] > 20 for row in payload['data']))
        first = WowArticle.objects.get(pk=payload['data'][0]['id'])
        self.assertEqual(payload['data'][0]['content_preview'],
                         BeautifulSoup(first.content[:600], 'html.parser').get_text(' ', strip=True)[:200])

    def test_history_filters_sources_keywords_and_pagination_match_database(self):
        ns.refresh_news_snapshot()
        base = WowArticle.objects.filter(is_active=True).exclude(title__isnull=True).exclude(title='')
        for params in ({}, {'exclude_source': 'nga', 'page': '2', 'page_size': '10'},
                       {'source': 'nga', 'exclude_source': 'nga'}, {'q': 'alpha 2'},
                       {'q': 'LMonitor'}, {'q': '中文 测试 1'}, {'q': 'missing'},
                       {'page': '999999'}, {'page': 'bad', 'page_size': 'bad'}, {'q': 'bluepost'},
                       {'q': '测试', 'page': '2', 'page_size': '10'}, {'q': '测试', 'page': '999', 'page_size': '10'}):
            query = base
            if params.get('source'):
                query = query.filter(source=params['source'])
            elif params.get('exclude_source'):
                query = query.exclude(source=params['exclude_source'])
            if params.get('q'):
                q = params['q']
                query = query.filter(Q(title__icontains=q) | Q(title_cn__icontains=q) | Q(author__icontains=q)
                                     | Q(source__icontains=q) | Q(category__icontains=q))
            expected = list(query.order_by('-publish_time', '-id'))
            with self.assertNumQueries(0):
                actual = self.payload(self.response(PortalNewsIndexAPIView, params))
            meta = actual['meta']
            offset = (meta['page'] - 1) * meta['page_size']
            self.assertEqual(meta['total'], len(expected))
            self.assertEqual(actual['data'], [self.public_row(row) for row in expected[offset:offset + meta['page_size']]])
            self.assertEqual(sum(row['count'] for row in actual['sources']), base.count())

    def test_publisher_reads_limited_fields_and_no_body_is_published(self):
        with CaptureQueriesContext(connection) as queries:
            ns.refresh_news_snapshot()
        self.assertEqual(len(queries), 1)
        sql = queries[0]['sql']
        self.assertNotIn('"content_blocks"', sql)
        self.assertNotIn('"description"', sql)
        self.assertIn('SUBSTR(', sql)
        index = ns._index()
        data = ns._content(index['shards'][0]['file'])
        self.assertNotIn('大结构正文', json.dumps(data, ensure_ascii=False))
        self.assertNotIn('大正文', json.dumps(data, ensure_ascii=False))
        self.assertNotIn('content', data['items'][0])

    def test_archive_shards_skip_unrelated_history_and_source_pages(self):
        WowArticle.objects.bulk_create([WowArticle(title=f'档案 {i}', source='archive', publish_time=self.now - timedelta(days=50))
                                       for i in range(1200)])
        ns.refresh_news_snapshot()
        index = ns._index()
        self.assertEqual(sum(shard['count'] for shard in index['shards']), 1237)
        self.assertTrue(all(shard['count'] <= 500 for shard in index['shards']))
        with patch.object(ns, '_content', wraps=ns._content) as read, self.assertNumQueries(0):
            payload = ns.read_news_page({'page': '37', 'page_size': '30'})
        self.assertEqual(read.call_count, 1)
        self.assertEqual(len(payload['data']), 30)
        with patch.object(ns, '_content', wraps=ns._content) as read:
            payload = ns.read_news_page({'source': 'wowhead'})
        # 跨月和无时间的新闻均需保留，但不应读取只有其他来源的历史片。
        self.assertEqual({call.args[0] for call in read.call_args_list},
                         {shard['file'] for shard in index['shards'] if shard['sources'].get('wowhead')})
        self.assertTrue(all(row['source'] == 'wowhead' for row in payload['data']))

    def test_empty_database_can_publish_a_valid_empty_list(self):
        WowArticle.objects.all().delete()
        ns.refresh_news_snapshot()
        with self.assertNumQueries(0):
            payload = ns.read_news_page({})
        self.assertEqual(payload['snapshot']['state'], 'ready')
        self.assertEqual(payload['data'], [])
        self.assertEqual(payload['meta']['total'], 0)

    def test_new_article_reuses_unchanged_historical_months(self):
        ns.refresh_news_snapshot()
        current_month = timezone.localtime(self.now).strftime('%Y-%m')
        previous = {shard['month']: shard['file'] for shard in ns._index()['shards'] if shard['month'] != current_month}
        self.assertTrue(previous)
        WowArticle.objects.create(title='新增本月新闻', publish_time=self.now)
        ns.refresh_news_snapshot()
        for shard in ns._index()['shards']:
            if shard['month'] != current_month:
                self.assertEqual(shard['file'], previous[shard['month']])

    def test_cold_and_corrupt_reads_do_not_query_enqueue_or_silently_drop_rows(self):
        with self.assertNumQueries(0), patch.object(ns, '_write') as write:
            response = self.response(PortalNewsIndexAPIView)
            home = self.response(PortalBluepostsAPIView)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(home.status_code, 202)
        self.assertEqual(response['Cache-Control'], 'no-store')
        write.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])
        ns.refresh_news_snapshot()
        index = ns._index()
        path = self.root / index['shards'][0]['file']
        path.write_text('{"schema":1,"items":[]}', encoding='utf-8')
        with self.assertNumQueries(0), patch.object(ns, '_write') as write:
            response = self.response(PortalNewsIndexAPIView)
        self.assertEqual(response.status_code, 503)
        write.assert_not_called()
        ns.refresh_news_snapshot()
        self.assertEqual(self.response(PortalNewsIndexAPIView).status_code, 200)
        damaged = ns._index()
        damaged['sources'] = {}
        ns._write(self.root / 'index.json', damaged)
        self.assertEqual(self.response(PortalNewsIndexAPIView).status_code, 503)
        ns.refresh_news_snapshot()
        self.assertEqual(self.response(PortalNewsIndexAPIView).status_code, 200)

    def test_content_reuse_etag_and_time_window_expiry(self):
        ns.refresh_news_snapshot()
        original = ns._index()
        ns.refresh_news_snapshot()
        self.assertEqual(ns._index(), original)
        response = self.response(PortalWowheadLatestAPIView)
        with self.assertNumQueries(0):
            cached = self.response(PortalWowheadLatestAPIView, headers={'If-None-Match': 'W/' + response['ETag']})
        self.assertEqual(cached.status_code, 304)
        with patch.object(ns.timezone, 'now', return_value=self.now + timedelta(days=20)):
            expired = self.response(PortalWowheadLatestAPIView, headers={'If-None-Match': response['ETag']})
        self.assertEqual(expired.status_code, 200)
        self.assertEqual(self.payload(expired)['data'], [])

    def test_failed_publish_preserves_previous_generation_and_recovers(self):
        ns.refresh_news_snapshot()
        previous = ns._index()
        previous_ids = [row['id'] for row in ns.read_news_page({})['data']]
        WowArticle.objects.create(title='新版本', source='wowhead')
        with patch.object(ns, '_write', side_effect=lambda path, body: (_ for _ in ()).throw(OSError('模拟发布中断'))
                               if path.name == 'index.json' else self.real_write(path, body)):
            with self.assertRaises(OSError):
                ns.refresh_news_snapshot()
        self.assertEqual(ns._index(), previous)
        self.assertEqual([row['id'] for row in ns.read_news_page({})['data']], previous_ids)
        self.assertEqual(ns.read_news_page({})['snapshot']['state'], 'stale')
        ns.refresh_news_snapshot()
        self.assertEqual(ns.read_news_page({})['snapshot']['state'], 'ready')
        self.assertNotEqual(ns._index()['generation'], previous['generation'])

    real_write = staticmethod(ns._write)

    def test_commit_notifications_cover_translation_activity_and_deletion(self):
        article = WowArticle.objects.first()
        for change in ('title_cn', 'is_active', 'delete'):
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                if change == 'delete':
                    article.delete()
                else:
                    setattr(article, change, '标题已翻译' if change == 'title_cn' else False)
                    article.save(update_fields=[change])
            self.assertEqual(len(callbacks), 1)
            self.assertTrue(ns._load(self.root / 'revision.json')['revision'])
        before = ns._load(self.root / 'revision.json')
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaises(ValueError), transaction.atomic():
                WowArticle.objects.create(title='回滚')
                raise ValueError('回滚')
        self.assertEqual(callbacks, [])
        self.assertEqual(ns._load(self.root / 'revision.json'), before)

    def test_poll_idle_zero_sql_and_bulk_update_compensation(self):
        ns.refresh_news_snapshot()
        with self.assertNumQueries(0):
            self.assertFalse(ns.refresh_news_snapshot(poll=True))
        article = WowArticle.objects.filter(source='wowhead').first()
        WowArticle.objects.filter(pk=article.pk).update(title_cn='批量翻译')
        before = ns._index()['generation']
        with patch.object(ns.time, 'time', return_value=ns.time.time() + 301):
            self.assertTrue(ns.refresh_news_snapshot(poll=True))
        self.assertNotEqual(ns._index()['generation'], before)

    def test_updates_during_build_abandon_publish_and_keep_pending(self):
        ns.refresh_news_snapshot()
        previous = ns._index()
        actual_rows = list(ns.lightweight_rows())
        def changed():
            yield actual_rows[0]
            ns._write(self.root / 'revision.json', {'revision': '新来源通知'})
            yield from actual_rows[1:]
        with patch.object(ns, 'lightweight_rows', side_effect=changed):
            self.assertFalse(ns.refresh_news_snapshot())
        self.assertEqual(ns._index(), previous)
        self.assertNotEqual(ns._load(self.root / 'checked.json')['revision'], ns._load(self.root / 'revision.json'))
        self.assertTrue(ns.refresh_news_snapshot())

    def test_publish_lock_prevents_duplicate_workers(self):
        with ns._lock(self.root / 'publish.lock'):
            with self.assertNumQueries(0):
                self.assertFalse(ns.refresh_news_snapshot())
        self.assertFalse((self.root / 'index.json').exists())

    def test_committed_event_refreshes_before_periodic_compensation(self):
        ns.refresh_news_snapshot()
        with self.captureOnCommitCallbacks(execute=True):
            article = WowArticle.objects.filter(source='wowhead').first()
            article.title_cn = '即时翻译通知'
            article.save(update_fields=['title_cn'])
        with patch.object(ns.time, 'time', return_value=ns.time.time() + 16):
            self.assertTrue(ns.refresh_news_snapshot(poll=True))
        self.assertEqual(ns.read_news_page({'q': '即时翻译通知'})['meta']['total'], 1)

    def test_nga_hot_fallback_and_legacy_preview_keep_existing_scope(self):
        WowArticle.objects.filter(source='nga').update(category='nga', nga_board_id='', author='nga前瞻区')
        ns.refresh_news_snapshot()
        preview = ns.read_home_news('nga_preview')['data']
        hot = ns.read_home_news('nga')['data']
        self.assertEqual(len(preview), 9)
        self.assertTrue(hot)
        self.assertTrue(all(row['category'] == 'nga' and row['reply_count'] > 20 for row in hot))

    def test_offline_command_uses_same_publisher(self):
        output = StringIO()
        call_command('refresh_news_snapshot', stdout=output)
        self.assertIn('发布完成', output.getvalue())
        self.assertEqual(ns.read_news_page({})['snapshot']['state'], 'ready')
