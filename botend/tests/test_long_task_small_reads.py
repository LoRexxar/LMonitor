"""Real ORM/HTTP regressions for small consumers of long-task data."""
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from zoneinfo import ZoneInfo

from django.db import connection
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from botend.models import SeasonMeta, SpecDungeonRanking, SpecRaidRanking, WowArticle
from botend.portal.updates import build_site_updates
from botend.controller.plugins.portal.PortalArticleTranslateMonitor import PortalArticleTranslateMonitor
from botend.services.article_translation_service import ArticleTranslationService, FallbackTranslationEngine


class SiteUpdateBoundedReadsTests(TransactionTestCase):
    def test_rank_timestamp_queries_are_indexed_top_one_and_keep_scope(self):
        now = timezone.now()
        old = now - timedelta(days=2)
        season = SeasonMeta.objects.create(season_key='bounded-updates', season_name='test',
            mplus_zone_id=1, raid_zone_id=1, mplus_encounters=[{'id': 1}, {'id': 2}])
        for dungeon, at in [(1, old), (2, old + timedelta(hours=1)), (99, now), (1, now + timedelta(days=1))]:
            SpecDungeonRanking.objects.create(season_id=season.pk, dungeon_id=dungeon,
                dps=100, last_updated=at)
        SpecRaidRanking.objects.create(season_id=season.pk, boss_id=1, dps=100, last_updated=old)
        SpecRaidRanking.objects.create(season_id=season.pk + 1, boss_id=1, dps=100, last_updated=now)
        with CaptureQueriesContext(connection) as captured:
            items = {r['key']: r for r in build_site_updates(now)['items']}
        self.assertEqual(items['mplus']['updated_at'], (old + timedelta(hours=1)).astimezone(
            ZoneInfo('Asia/Shanghai')).isoformat())
        self.assertEqual(items['raid']['status'], 'older')
        queries = [q['sql'] for q in captured if 'wow_spec_dungeon_ranking' in q['sql'] or 'wow_spec_raid_ranking' in q['sql']]
        self.assertEqual(len(queries), 3)
        for sql in queries:
            self.assertNotIn('MAX(', sql.upper())
            self.assertIn('LIMIT 1', sql.upper())
            self.assertNotIn('gear_json', sql)
        for model, name in [(SpecDungeonRanking, 'idx_dungeon_freshness'), (SpecRaidRanking, 'idx_raid_freshness')]:
            with connection.cursor() as cursor:
                indexes = connection.introspection.get_constraints(cursor, model._meta.db_table)
            self.assertIn(name, indexes)


class ArticleFetchSingleAttemptTests(TransactionTestCase):
    def test_rejected_body_fetched_once_and_backoff_survives_new_instance(self):
        requests_seen = []
        class Handler(BaseHTTPRequestHandler):
            body = b'<html><nav>Unrelated navigation</nav></html>'
            def do_GET(self):
                requests_seen.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(self.body)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.shutdown)
        article = WowArticle.objects.create(title='Actual unrelated long article title',
            source='blizzard_tracker', category='news', content='',
            url=f'http://127.0.0.1:{server.server_port}/article')
        def monitor():
            obj = PortalArticleTranslateMonitor(None, None)
            obj.translation_service = ArticleTranslationService(engine=FallbackTranslationEngine(engines=[]))
            return obj
        self.assertTrue(monitor().scan(''))
        self.assertEqual(requests_seen, ['/article'])
        article.refresh_from_db()
        self.assertFalse(article.content)
        self.assertGreater(article.content_fetch_retry_at, timezone.now())
        self.assertTrue(monitor().scan(''))
        self.assertEqual(requests_seen, ['/article'])
        WowArticle.objects.filter(pk=article.pk).update(content_fetch_retry_at=timezone.now() - timedelta(seconds=1))
        self.assertTrue(monitor().scan(''))
        self.assertEqual(requests_seen, ['/article', '/article'])
        Handler.body = ('<article><h1>Actual unrelated long article title</h1><p>'
            + 'Actual unrelated long article title. Verified game news body. ' * 30
            + '</p></article>').encode()
        WowArticle.objects.filter(pk=article.pk).update(content_fetch_retry_at=timezone.now() - timedelta(seconds=1))
        self.assertTrue(monitor().scan(''))
        self.assertEqual(requests_seen, ['/article', '/article', '/article'])
        article.refresh_from_db()
        self.assertIn('Verified game news body', article.content)
        self.assertTrue(article.content_blocks)
        self.assertIsNone(article.content_fetch_retry_at)
        self.assertTrue(monitor().scan(''))
        self.assertEqual(requests_seen, ['/article', '/article', '/article'])
