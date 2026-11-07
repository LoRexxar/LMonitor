"""Real source-metadata extracts, real migrated ORM, and loopback HTTP replay."""
from contextlib import contextmanager
from datetime import timedelta
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

from bs4 import BeautifulSoup
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
import requests
from requests.adapters import HTTPAdapter

from botend.controller.plugins.portal.PortalPostMonitor import PortalPostMonitor
from botend.models import BlizzardForumCategory, BlizzardForumTopic, MonitorTask, WowArticle
from botend.services.article_translation_service import ArticleTranslationService
from botend.services.blizzard_forum import (
    FORUM_URL, ForumMetadataCollector, TopicIdentityMismatch, article_channel,
    channel_payload, parse_topic_metadata, persist_topic_metadata, topic_id_from_url,
)

ROOT = Path(__file__).parent / 'fixtures/blizzard_forum'
SAMPLES = json.loads((ROOT / 'manifest.json').read_text())
BY_ID = {s['topic_id']: s for s in SAMPLES}


def sample_html(topic_id=2404092):
    return (ROOT / BY_ID[topic_id]['file']).read_text()


def alter_html(text, mutate):
    soup = BeautifulSoup(text, 'html.parser')
    data = json.loads(soup.select_one('#data-preloaded').string)
    key = next(k for k in data if k.startswith('topic_'))
    topic, site = json.loads(data[key]), json.loads(data['site'])
    mutate(topic, site, soup, data)
    data = {k: v for k, v in data.items() if not k.startswith('topic_')}
    data[f"topic_{topic['id']}"] = json.dumps(topic)
    data['site'] = json.dumps(site)
    soup.select_one('#data-preloaded').string = json.dumps(data)
    return str(soup)


def cloned_html(topic_id, source_id=2404092):
    return alter_html(sample_html(source_id), lambda t, *_: t.update(id=topic_id))


def body_html(text, body='Controlled loopback article body. ' * 20):
    # Deliberately synthetic body; only metadata comes from official source extracts.
    return text.replace('</body>', '<div class="topic-body crawler-post"><div class="post">'
                        + '<p>' + escape(body) + '</p></div></div></body>')


def synthetic_metadata(categories, topic_id=900001):
    payload = {f'topic_{topic_id}': json.dumps({'id': topic_id, 'category_id': categories[-1]['id']}),
               'site': json.dumps({'categories': categories})}
    crumbs, path = [], FORUM_URL + '/c'
    for pos, c in enumerate(categories, 1):
        path += '/' + c['slug']
        url = path + '/' + str(c['id'])
        crumbs.append(f'<span itemprop="itemListElement"><a itemprop="item" href="{url}">'
                      f'<span itemprop="name">{escape(c["name"])}</span></a>'
                      f'<meta itemprop="position" content="{pos}"></span>')
    return ('<script id="data-preloaded">' + json.dumps(payload) + '</script>'
            + '<div class="topic-category" itemtype="http://schema.org/BreadcrumbList">'
            + ''.join(crumbs) + '</div>')


class UnavailableEngine:
    def available(self):
        return False


@contextmanager
def replay_server():
    routes, received = {}, []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.path)
            status, content = routes.get(self.path, (404, 'not found'))
            data = content.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class SourceToLoopbackAdapter(HTTPAdapter):
        def send(self, request, **kwargs):
            original = request.url
            parsed = urlsplit(original)
            assert parsed.scheme == 'https' and parsed.netloc == 'us.forums.blizzard.com'
            request.url = f'http://127.0.0.1:{server.server_port}' + parsed.path
            try:
                response = super().send(request, **kwargs)
                response.url = original
                return response
            finally:
                request.url = original

    from utils.LReq import LReq

    class LoopbackLReq(LReq):
        bindings = []

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.s.trust_env = False
            self.s.mount('https://us.forums.blizzard.com/', SourceToLoopbackAdapter())

        def set_current_task(self, task):
            super().set_current_task(task)
            self.bindings.append(task.pk if task else None)

    try:
        yield routes, received, LoopbackLReq
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class BlizzardForumTests(TestCase):
    def article(self, topic_id=2404092, url=None, **fields):
        defaults = dict(title='Unrelated title without product clues', source='blizzard_tracker', category='bluepost',
                        url=url or BY_ID[topic_id]['url'], title_cn='保留译文', content='Existing original body. ' * 20,
                        content_cn='原有正文译文', content_blocks='[]', content_blocks_cn='[]',
                        description='preserved description')
        defaults.update(fields)
        return WowArticle.objects.create(**defaults)

    def protected(self, article):
        return {f: getattr(article, f) for f in ('title', 'title_cn', 'description', 'content', 'content_cn',
                                               'content_blocks', 'content_blocks_cn', 'publish_time')}

    def test_authoritative_real_samples(self):
        for sample in SAMPLES:
            with self.subTest(topic=sample['topic_id']):
                raw = (ROOT / sample['file']).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), sample['fixture_sha256'])
                parsed = parse_topic_metadata(raw.decode(), sample['url'])
                self.assertEqual(parsed['topic_id'], sample['topic_id'])
                self.assertEqual(parsed['categories'][-1]['external_id'], sample['category_id'])
                self.assertEqual(parsed['categories'][-1]['game_version'], sample['game_version'])
                a = self.article(sample['topic_id'])
                before = self.protected(a)
                persist_topic_metadata(a, raw.decode(), sample['url'])
                a.refresh_from_db()
                self.assertEqual(self.protected(a), before)
                self.assertEqual(article_channel(a)['game_version'], sample['game_version'])
        self.assertNotIn('Forever', BY_ID[2404092]['title'])
        self.assertEqual(BY_ID[2402801]['title'], BY_ID[2402802]['title'])
        self.assertNotEqual(BY_ID[2402801]['game_version'], BY_ID[2402802]['game_version'])
        self.assertEqual(BlizzardForumTopic.objects.count(), 8)
        self.assertEqual(set(BlizzardForumCategory.objects.values_list('external_id', flat=True)),
                         {23, 170, 171, 346, 347, 349, 350, 351, 352})

    def test_url_identity_and_strict_positive_ids(self):
        for url in (FORUM_URL + '/t/2404092/1', FORUM_URL + '/t/2404092', BY_ID[2404092]['url']):
            self.assertEqual(topic_id_from_url(url), 2404092)
        bad = ['http://us.forums.blizzard.com/en/wow/t/x/1',
               'https://evil.example/en/wow/t/x/1', FORUM_URL + '/t/x/01', FORUM_URL + '/t/x/1/0',
               FORUM_URL + '/t/x/-1', FORUM_URL + '/t/x/1?post=2', FORUM_URL + '/t/x/1#2',
               FORUM_URL + '/t/x/1/2/3', FORUM_URL.replace('/en/wow', '/en/other') + '/t/x/1',
               'https://user@us.forums.blizzard.com/en/wow/t/x/1', FORUM_URL + '/t/x/1\n',
               'https://us.forums.blizzard.com:443/en/wow/t/x/1']
        for url in bad:
            with self.subTest(url=url), self.assertRaises(ValueError):
                topic_id_from_url(url)
        for invalid in (True, '2404092', 0, -1):
            html = alter_html(sample_html(), lambda t, *_: t.update(id=invalid))
            with self.subTest(id=invalid), self.assertRaises(ValueError):
                parse_topic_metadata(html, BY_ID[2404092]['url'])

    def test_unknown_beta_classic_and_test_ancestor_precedence(self):
        unknowns = [
            [{'id': 197, 'name': 'WoW Classic', 'slug': 'wow-classic', 'parent_category_id': None}],
            [{'id': 43, 'name': 'Recruitment', 'slug': 'recruitment', 'parent_category_id': None},
             {'id': 279, 'name': 'WoW Group Finder Forums BETA – Find Other Players!',
              'slug': 'group-finder', 'parent_category_id': 43}],
        ]
        for cats in unknowns:
            with self.subTest(category=cats[-1]['id']):
                p = parse_topic_metadata(synthetic_metadata(cats), FORUM_URL + '/t/x/900001')
                self.assertEqual(p['categories'][-1]['game_version'], '')
        cats = [{'id': 346, 'name': 'Forever', 'slug': 'forever', 'parent_category_id': None},
                {'id': 23, 'name': 'In Development', 'slug': 'in-development', 'parent_category_id': 346},
                {'id': 349, 'name': 'Forever Beta', 'slug': 'forever-beta', 'parent_category_id': 23}]
        p = parse_topic_metadata(synthetic_metadata(cats), FORUM_URL + '/t/x/900001')
        self.assertEqual(p['categories'][-1]['game_version'], 'test')

    def test_invalid_conflicting_metadata_keeps_all_previous_facts(self):
        a = self.article()
        persist_topic_metadata(a, sample_html(), a.url)
        before = self.protected(a)
        topic_before = list(BlizzardForumTopic.objects.values())
        categories_before = list(BlizzardForumCategory.objects.values())
        cases = [
            '<html>challenge</html>',
            sample_html(2360696),
            alter_html(sample_html(), lambda t, *_: t.update(category_id=99999)),
            alter_html(sample_html(), lambda t, site, *_: site['categories'][-1].update(parent_category_id=None)),
            alter_html(sample_html(), lambda t, site, *_: site['categories'][-1].update(parent_category_id=347)),
            alter_html(sample_html(), lambda t, site, soup, *_: soup.select_one('[itemprop="name"]').clear()),
            alter_html(sample_html(), lambda t, site, soup, *_: soup.select_one('a[itemprop="item"]').attrs.update(href='https://evil.example/c/346')),
        ]
        for html in cases:
            with self.subTest(case=html[:60]), self.assertRaises(ValueError):
                persist_topic_metadata(a, html, a.url)
        a.refresh_from_db()
        self.assertEqual(self.protected(a), before)
        self.assertEqual(list(BlizzardForumTopic.objects.values()), topic_before)
        self.assertEqual(list(BlizzardForumCategory.objects.values()), categories_before)
        other = self.article(2360696)
        with self.assertRaises(TopicIdentityMismatch):
            persist_topic_metadata(other, sample_html(), a.url)
        other.source = 'wowhead'
        with self.assertRaises(TopicIdentityMismatch):
            persist_topic_metadata(other, sample_html(2360696), other.url)

    def test_suggested_topics_and_body_links_cannot_supply_classification(self):
        text = alter_html(sample_html(), lambda t, *_: t.update(
            suggested_topics=[{'id': 999, 'category_id': 349}]))
        text += '<a href="/en/wow/c/in-development/wow-forever-beta-discussion/349">Beta</a>'
        self.assertEqual(parse_topic_metadata(text, BY_ID[2404092]['url'])['categories'][-1]['game_version'], 'forever')

    def test_central_constraints_idempotence_stale_response_and_join_budget(self):
        a = self.article()
        now = timezone_now = __import__('django.utils.timezone', fromlist=['now']).now()
        persist_topic_metadata(a, sample_html(), a.url, observed_at=now)
        first_topic = list(BlizzardForumTopic.objects.values())
        first_categories = list(BlizzardForumCategory.objects.values())
        with CaptureQueriesContext(connection) as queries:
            persist_topic_metadata(a, sample_html(), a.url, observed_at=now)
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries))
        self.assertEqual(list(BlizzardForumTopic.objects.values()), first_topic)
        self.assertEqual(list(BlizzardForumCategory.objects.values()), first_categories)
        with self.assertRaises(IntegrityError), transaction.atomic():
            BlizzardForumTopic.objects.create(forum_url=FORUM_URL, external_id=2404092)
        with self.assertRaises(IntegrityError), transaction.atomic():
            BlizzardForumCategory.objects.create(forum_url=FORUM_URL, external_id=347, name='x', slug='x', url='x')
        retail = cloned_html(2404092, source_id=2402801)
        persist_topic_metadata(a, retail, a.url, observed_at=now - timedelta(seconds=1))
        self.assertEqual(article_channel(a)['game_version'], 'forever')
        persist_topic_metadata(a, retail, a.url, observed_at=now + timedelta(seconds=1))
        self.assertEqual(article_channel(a)['game_version'], 'retail')
        for n in range(2, 8):
            other = self.article(url=FORUM_URL + f'/t/x/2404092/{n}')
            persist_topic_metadata(other, retail, other.url, observed_at=now)
        with self.assertNumQueries(1):
            payloads = [article_channel(row) for row in WowArticle.objects.select_related('blizzard_topic__category')]
        self.assertEqual(len(payloads), 7)
        self.assertTrue(all(p['game_version_label'] == BlizzardForumCategory.GameVersion.RETAIL.label for p in payloads))
        self.assertEqual(channel_payload('nonsense')['game_version_label'], '')
        self.assertIsNone(channel_payload('')['forum_category'])
        unrelated = self.article(2360696, source='wowhead', blizzard_topic=a.blizzard_topic)
        self.assertEqual(article_channel(unrelated), channel_payload(''))

    def test_http_response_reuse_and_topic_sharing_without_post_body_sharing(self):
        with replay_server() as (routes, received, Req):
            url1 = FORUM_URL + '/t/class-tuning/2404092/1'
            url2 = FORUM_URL + '/t/class-tuning/2404092/2'
            routes[urlsplit(url1).path] = (200, body_html(sample_html(), 'First post unique body. ' * 20))
            routes[urlsplit(url2).path] = (200, body_html(sample_html(), 'Second post different body. ' * 20))
            task = MonitorTask.objects.create(name='PortalPostMonitor', target=FORUM_URL)
            req = Req(is_chrome=False)
            req.set_current_task(task)
            self.addCleanup(req.s.close)
            monitor = PortalPostMonitor(req, task)
            monitor.translation_service = ArticleTranslationService(engine=UnavailableEngine())
            a, b = self.article(url=url1, content=''), self.article(url=url2, content='')
            self.assertTrue(monitor.ensure_article_channel(a))
            self.assertTrue(monitor.ensure_article_channel(b))
            self.assertEqual(received, [urlsplit(url1).path])
            monitor._ensure_article_filled_and_translated(a, 'blizzard_tracker')
            monitor._ensure_article_filled_and_translated(b, 'blizzard_tracker')
            a.refresh_from_db(); b.refresh_from_db()
            self.assertIn('First post unique body', a.content)
            self.assertNotIn('Second post different body', a.content)
            self.assertIn('Second post different body', b.content)
            self.assertEqual(a.blizzard_topic_id, b.blizzard_topic_id)
            self.assertEqual(received, [urlsplit(url1).path, urlsplit(url2).path])
            self.assertEqual(BlizzardForumTopic.objects.count(), 1)

    def test_rss_metadata_independent_of_three_translation_budget_and_twenty_limit(self):
        with replay_server() as (routes, received, Req):
            items = []
            for i in range(21):
                tid = 900000 + i
                url = FORUM_URL + f'/t/unrelated-title/{tid}/1'
                html = cloned_html(tid)
                routes[urlsplit(url).path] = (200, body_html(html))
                items.append(f'<item><title>Title {i}</title><link>{url}</link></item>')
                self.article(url=url, content='' if i < 3 else 'Original full existing body ' * 20)
            rss_path = '/en/wow/g/blizzard-tracker/posts.rss'
            routes[rss_path] = (200, '<rss><channel>' + ''.join(items) + '</channel></rss>')
            req = Req(is_chrome=False)
            self.addCleanup(req.s.close)
            monitor = PortalPostMonitor(req, None)
            monitor.translation_service = ArticleTranslationService(engine=UnavailableEngine())
            monitor.update_blueposts()
            self.assertEqual(monitor._translate_budget, 0)
            self.assertEqual(WowArticle.objects.filter(blizzard_topic__isnull=False).count(), 20)
            self.assertEqual(BlizzardForumTopic.objects.count(), 20)
            self.assertEqual(len(received), 21)  # RSS + 20 exact detail URLs, no extra catalogs or JSON.
            self.assertEqual(received[0], rss_path)
            self.assertNotIn('/en/wow/t/unrelated-title/900020/1', received)
            for i in range(3):
                self.assertIn('Controlled loopback article body', WowArticle.objects.get(url=FORUM_URL + f'/t/unrelated-title/{900000+i}/1').content)
            self.assertTrue(WowArticle.objects.get(url=FORUM_URL + '/t/unrelated-title/900019/1').content.startswith('Original full'))

    def test_metadata_failure_body_survives_shared_backoff_and_expiry(self):
        from django.utils import timezone
        with replay_server() as (routes, received, Req):
            a = self.article(content='')
            path = urlsplit(a.url).path
            routes[path] = (200, body_html('<html><body>metadata absent</body></html>'))
            req = Req(is_chrome=False)
            self.addCleanup(req.s.close)
            monitor = PortalPostMonitor(req, None)
            monitor.translation_service = ArticleTranslationService(engine=UnavailableEngine())
            self.assertFalse(monitor.ensure_article_channel(a))
            monitor._ensure_article_filled_and_translated(a, 'blizzard_tracker')
            a.refresh_from_db()
            self.assertIn('Controlled loopback article body', a.content)
            self.assertIsNone(a.blizzard_topic_id)
            retry = BlizzardForumTopic.objects.get(external_id=2404092).metadata_retry_at
            self.assertGreater(retry, timezone.now())
            second = PortalPostMonitor(req, None)
            self.assertFalse(second.ensure_article_channel(a))
            self.assertEqual(BlizzardForumTopic.objects.get(external_id=2404092).metadata_retry_at, retry)
            self.assertEqual(received, [path])
            BlizzardForumTopic.objects.filter(external_id=2404092).update(metadata_retry_at=timezone.now() - timedelta(seconds=1))
            routes[path] = (200, body_html(sample_html()))
            third = PortalPostMonitor(req, None)
            self.assertTrue(third.ensure_article_channel(a))
            a.refresh_from_db()
            self.assertIsNone(a.blizzard_topic.metadata_retry_at)
            self.assertEqual(article_channel(a)['game_version'], 'forever')
            self.assertEqual(received, [path, path])

    def test_http_403_and_wrong_topic_never_reused_as_body(self):
        for status, text in [(403, 'Forbidden'), (200, body_html(sample_html(2360696)))]:
            with self.subTest(status=status), replay_server() as (routes, received, Req):
                a = self.article(url=BY_ID[2404092]['url'] + ('/' if status == 403 else ''), content='')
                # Preserve existing last-good category across both failure kinds.
                persist_topic_metadata(a, sample_html(), a.url)
                before = self.protected(a)
                routes[urlsplit(a.url).path] = (status, text)
                req = Req(is_chrome=False)
                self.addCleanup(req.s.close)
                monitor = PortalPostMonitor(req, None)
                self.assertFalse(monitor.ensure_article_channel(a, force=True))
                self.assertEqual(monitor._fetch_blizzard_tracker_blocks(a.url), [])
                a.refresh_from_db()
                self.assertEqual(self.protected(a), before)
                self.assertEqual(article_channel(a)['game_version'], 'forever')
                self.assertFalse(PortalPostMonitor(req, None).ensure_article_channel(a, force=True))
                self.assertEqual(received, [urlsplit(a.url).path])
                BlizzardForumTopic.objects.all().delete()

    def test_management_dry_run_is_zero_write_and_apply_is_exact_metadata_only(self):
        from botend.management.commands.refresh_blizzard_forum_metadata import Command
        task = MonitorTask.objects.create(name='PortalPostMonitor', target=FORUM_URL)
        a, b, c = self.article(), self.article(2360696), self.article(2402801, source='wowhead')
        before = {row.pk: self.protected(row) for row in (a, b, c)}
        with CaptureQueriesContext(connection) as queries, patch.object(Command, 'build_collector', side_effect=AssertionError('dry-run must not fetch')):
            output = StringIO()
            call_command('refresh_blizzard_forum_metadata', '--id', str(a.pk), '--id', str(b.pk), '--limit', '1', stdout=output)
        report = json.loads(output.getvalue())
        self.assertEqual((report['count'], report['changed'], report['failed']), (1, 0, 0))
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries))
        self.assertEqual(BlizzardForumTopic.objects.count(), 0)
        with replay_server() as (routes, received, Req), patch('utils.LReq.LReq', Req):
            for row in (a, b):
                routes[urlsplit(row.url).path] = (200, sample_html(topic_id_from_url(row.url)))
            output = StringIO()
            call_command('refresh_blizzard_forum_metadata', '--apply', '--id', str(a.pk), '--id', str(b.pk), '--id', str(c.pk), '--limit', '23', stdout=output)
            report = json.loads(output.getvalue())
            self.assertEqual((report['count'], report['changed'], report['failed']), (2, 2, 0))
            self.assertEqual(set(received), {urlsplit(a.url).path, urlsplit(b.url).path})
            self.assertEqual(Req.bindings, [task.pk, None])
        for row in (a, b, c):
            row.refresh_from_db()
            self.assertEqual(self.protected(row), before[row.pk])
        self.assertEqual(WowArticle.objects.count(), 3)
        self.assertIsNone(c.blizzard_topic_id)
        self.assertEqual(article_channel(a)['game_version'], 'forever')
        self.assertEqual(article_channel(b)['game_version'], 'test')
        for bad in ('0', '-1', 'x'):
            with self.subTest(limit=bad), self.assertRaises(Exception):
                call_command('refresh_blizzard_forum_metadata', '--limit', bad)

    def test_management_safe_failure_and_no_facts_erasure(self):
        a = self.article()
        persist_topic_metadata(a, sample_html(), a.url)
        before = self.protected(a)
        MonitorTask.objects.create(name='PortalPostMonitor', target=FORUM_URL)
        with replay_server() as (routes, received, Req), patch('utils.LReq.LReq', Req):
            routes[urlsplit(a.url).path] = (403, 'secret credentials do not leak')
            output = StringIO()
            with self.assertRaises(CommandError):
                call_command('refresh_blizzard_forum_metadata', '--apply', '--id', str(a.pk), stdout=output)
            report = json.loads(output.getvalue())
            self.assertEqual(report['failed'], 1)
            self.assertEqual(report['results'][0]['error'], 'ValueError')
            self.assertNotIn('secret', output.getvalue())
            self.assertEqual(len(received), 1)
        a.refresh_from_db()
        self.assertEqual(self.protected(a), before)
        self.assertEqual(article_channel(a)['game_version'], 'forever')
