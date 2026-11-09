"""SQLite lease logic + real loopback HTTP; not an InnoDB contention proof."""
from datetime import timedelta
from io import StringIO
import json
import socket
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from botend.tests import test_blizzard_forum as replay
from botend.models import (BlizzardForumCategory, BlizzardForumTopic, MonitorTask,
                           MonitorTaskLease, MonitorTaskLeaseLost, WowArticle)
from botend.plugin_sync import claim_monitor_task
from botend.controller.plugins.portal.PortalPostMonitor import PortalPostMonitor
from botend.services.blizzard_forum import (FORUM_URL, ForumMetadataCollector,
    persist_topic_metadata, validate_metadata_lease)


class BlizzardForumLeaseTests(TestCase):
    def setUp(self):
        connect = socket.socket.connect

        def loopback_only(sock, address):
            if address[0] not in ('127.0.0.1', '::1'):
                raise AssertionError('non-loopback network forbidden in lease regression')
            return connect(sock, address)

        network = patch.object(socket.socket, 'connect', loopback_only)
        network.start()
        self.addCleanup(network.stop)

    def article(self, post=1):
        return WowArticle.objects.create(title='Preserve', content='Original', title_cn='保留',
            source='blizzard_tracker', category='bluepost',
            url=FORUM_URL + f'/t/class-tuning/2404092/{post}')

    def claimed(self):
        task = MonitorTask.objects.create(name='PortalPostMonitor', target=FORUM_URL)
        return claim_monitor_task(task.pk, lease_owner='exact-owner')

    def guard(self, task):
        return lambda: validate_metadata_lease(task.pk, 'exact-owner')

    def test_loss_during_http_blocks_success_failure_and_new_topic_writes(self):
        task = self.claimed()
        a = self.article()
        with replay.replay_server() as (routes, received, Req):
            class LoseAfterHTTP(Req):
                def get(inner, *args, **kwargs):
                    response = super().get(*args, **kwargs)
                    MonitorTaskLease.objects.filter(task=task).update(owner='replacement')
                    return response
            req = LoseAfterHTTP(is_chrome=False)
            self.addCleanup(req.s.close)
            for status, text in ((200, replay.sample_html()), (403, 'private'), (200, '<html>bad</html>')):
                with self.subTest(status=status, text=text[:12]):
                    MonitorTaskLease.objects.filter(task=task).update(owner='exact-owner')
                    routes[urlsplit(a.url).path] = (status, text)
                    collector = ForumMetadataCollector(req, write_guard=self.guard(task))
                    with self.assertRaises(MonitorTaskLeaseLost):
                        collector.ensure(a, force=True)
                    self.assertEqual(BlizzardForumTopic.objects.count(), 0)
                    self.assertEqual(BlizzardForumCategory.objects.count(), 0)
                    a.refresh_from_db()
                    self.assertIsNone(a.blizzard_topic_id)
            self.assertEqual(len(received), 3)

    def test_expired_owner_and_fresh_ttl_attach_are_fenced(self):
        task = self.claimed()
        a, b = self.article(), self.article(2)
        persist_topic_metadata(a, replay.sample_html(), a.url)
        before = list(BlizzardForumTopic.objects.values())
        with replay.replay_server() as (routes, received, Req):
            req = Req(is_chrome=False)
            self.addCleanup(req.s.close)
            for updates in ({'owner': 'replacement'}, {'owner': 'exact-owner', 'expires_at': timezone.now() - timedelta(seconds=1)}):
                MonitorTaskLease.objects.filter(task=task).update(**updates)
                collector = ForumMetadataCollector(req, write_guard=self.guard(task))
                with self.assertRaises(MonitorTaskLeaseLost):
                    collector.ensure(b)
                b.refresh_from_db()
                self.assertIsNone(b.blizzard_topic_id)
                self.assertEqual(list(BlizzardForumTopic.objects.values()), before)
            self.assertEqual(received, [])

    def test_force_deduplicates_topic_but_body_cache_is_exact_url(self):
        a, b = self.article(), self.article(2)
        with replay.replay_server() as (routes, received, Req):
            routes[urlsplit(a.url).path] = (200, replay.body_html(replay.sample_html(), 'FIRST'))
            routes[urlsplit(b.url).path] = (200, replay.body_html(replay.sample_html(), 'SECOND'))
            req = Req(is_chrome=False)
            self.addCleanup(req.s.close)
            collector = ForumMetadataCollector(req)
            self.assertTrue(collector.ensure(a, force=True))
            self.assertTrue(collector.ensure(b, force=True))
            self.assertEqual(a.blizzard_topic_id, b.blizzard_topic_id)
            self.assertEqual(len(received), 1)
            self.assertIn('FIRST', collector.html_for_url(a.url))
            self.assertIn('SECOND', collector.html_for_url(b.url))
            self.assertEqual(received, [urlsplit(a.url).path, urlsplit(b.url).path])
            MonitorTask.objects.create(name='PortalPostMonitor', target=FORUM_URL)
            received.clear()
            with patch('utils.LReq.LReq', Req):
                out = StringIO()
                call_command('refresh_blizzard_forum_metadata', '--apply', stdout=out)
            self.assertEqual(json.loads(out.getvalue())['count'], 2)
            self.assertEqual(len(received), 1)

    def test_command_pairing_preflight_and_bound_owner(self):
        task = self.claimed()
        a = self.article()
        for args in (('--task-id', str(task.pk)), ('--lease-owner', 'exact-owner')):
            with self.subTest(args=args), self.assertRaises(CommandError):
                call_command('refresh_blizzard_forum_metadata', *args, stdout=StringIO())
        with replay.replay_server() as (routes, received, Req), patch('utils.LReq.LReq', Req):
            routes[urlsplit(a.url).path] = (200, replay.sample_html())
            with self.assertRaises((MonitorTaskLeaseLost, CommandError)):
                call_command('refresh_blizzard_forum_metadata', '--apply', '--task-id', str(task.pk),
                             '--lease-owner', 'wrong', stdout=StringIO())
            self.assertEqual(received, [])
            clock = MonitorTask.objects.get(pk=task.pk).last_scan_time
            out = StringIO()
            call_command('refresh_blizzard_forum_metadata', '--apply', '--task-id', str(task.pk),
                         '--lease-owner', 'exact-owner', stdout=out)
            self.assertEqual(json.loads(out.getvalue())['changed'], 1)
            self.assertEqual(MonitorTask.objects.get(pk=task.pk).last_scan_time, clock)

    def test_command_failure_is_nonzero_safe_json_and_cooldown_survives(self):
        self.claimed()
        a = self.article()
        with replay.replay_server() as (routes, received, Req), patch('utils.LReq.LReq', Req):
            routes[urlsplit(a.url).path] = (403, 'private secret')
            out = StringIO()
            with self.assertRaises(CommandError):
                call_command('refresh_blizzard_forum_metadata', '--apply', stdout=out)
            report = json.loads(out.getvalue())
            self.assertEqual(report['failed'], 1)
            self.assertNotIn('secret', out.getvalue())
            retry = BlizzardForumTopic.objects.get(external_id=2404092).metadata_retry_at
            out = StringIO()
            call_command('refresh_blizzard_forum_metadata', '--apply', stdout=out)
            report = json.loads(out.getvalue())
            self.assertEqual(report['results'][0]['error'], 'metadata_cooldown')
            self.assertEqual(report['skipped'], 1)
            self.assertEqual(len(received), 1)
            self.assertEqual(BlizzardForumTopic.objects.get(external_id=2404092).metadata_retry_at, retry)

    def test_monitor_owner_propagates_and_loss_stops_scan_other_sources(self):
        task = self.claimed()
        a = self.article()
        with replay.replay_server() as (routes, received, Req):
            rss = '/en/wow/g/blizzard-tracker/posts.rss'
            routes[rss] = (200, f'<rss><channel><item><title>Preserve</title><link>{a.url}</link></item></channel></rss>')
            routes[urlsplit(a.url).path] = (200, replay.sample_html())
            req = Req(is_chrome=False)
            self.addCleanup(req.s.close)
            monitor = PortalPostMonitor(req, task)
            clock = MonitorTask.objects.get(pk=task.pk).last_scan_time
            self.assertTrue(monitor.ensure_article_channel(a))
            original_get = req.get

            def lose_on_rss(*args, **kwargs):
                response = original_get(*args, **kwargs)
                if urlsplit(args[0]).path == rss:
                    MonitorTaskLease.objects.filter(task=task).update(owner='replacement')
                return response

            req.get = lose_on_rss
            with patch.object(monitor, 'update_exwind_latest') as other, \
                    patch.object(monitor, 'update_blizzard_cn_news'), patch.object(monitor, 'update_nga_hot'):
                with self.assertRaises(MonitorTaskLeaseLost):
                    monitor.scan(FORUM_URL)
                other.assert_not_called()
            self.assertEqual(received, [urlsplit(a.url).path, rss])
            self.assertEqual(MonitorTask.objects.get(pk=task.pk).last_scan_time, clock)
            offline = PortalPostMonitor(req, MagicMock())
            self.assertIsNone(offline._forum_metadata_collector().write_guard)

    def test_same_scope_other_live_lease_rejects_exact_owner(self):
        task = self.claimed()
        other = MonitorTask.objects.create(name='PortalPostMonitor', target=FORUM_URL)
        MonitorTaskLease.objects.create(task=other, owner='other', expires_at=timezone.now()+timedelta(minutes=5))
        with transaction.atomic(), self.assertRaises(MonitorTaskLeaseLost):
            validate_metadata_lease(task.pk, 'exact-owner')


    def test_fresh_category_ttl_does_not_allow_body_write_after_detail_http_loses_owner(self):
        task = self.claimed()
        a = self.article()
        persist_topic_metadata(a, replay.sample_html(), a.url)
        before = WowArticle.objects.values().get(pk=a.pk)
        with replay.replay_server() as (routes, received, Req):
            rss = '/en/wow/g/blizzard-tracker/posts.rss'
            routes[rss] = (200, f'<rss><channel><item><title>Preserve</title><link>{a.url}</link></item></channel></rss>')
            routes[urlsplit(a.url).path] = (200, replay.body_html(replay.sample_html()))

            class LoseDuringBody(Req):
                def get(inner, *args, **kwargs):
                    response = super().get(*args, **kwargs)
                    if urlsplit(args[0]).path == urlsplit(a.url).path:
                        MonitorTaskLease.objects.filter(task=task).update(owner='replacement')
                    return response

            req = LoseDuringBody(is_chrome=False)
            self.addCleanup(req.s.close)
            monitor = PortalPostMonitor(req, task)
            with patch.object(monitor, 'update_exwind_latest') as other, \
                    patch.object(monitor, 'update_blizzard_cn_news') as china, patch.object(monitor, 'update_nga_hot') as nga:
                with self.assertRaises(MonitorTaskLeaseLost):
                    monitor.scan(FORUM_URL)
                other.assert_not_called();china.assert_not_called();nga.assert_not_called()
            self.assertEqual(WowArticle.objects.values().get(pk=a.pk), before)
            self.assertEqual(received, [rss, urlsplit(a.url).path])
