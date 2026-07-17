import importlib
from unittest.mock import Mock

from django.apps import apps
from django.test import TestCase

from botend.models import (
    MonitorTask,
    PortalEvent,
    PortalToolLink,
    PortalVideo,
    SystemAlert,
    VideoMonitorTarget,
    WowArticle,
)


class NgaDomainMigrationTests(TestCase):
    def test_replaces_old_domain_and_merges_colliding_articles(self):
        old_url = 'https://nga.178.com/read.php?tid=123'
        new_url = 'https://bbs.nga.cn/read.php?tid=123'
        old = WowArticle.objects.create(
            title='前瞻文章', url=old_url, source='nga', category='nga',
            author='nga前瞻区', content=f'原文链接：{old_url}', reply_count=21,
        )
        current = WowArticle.objects.create(
            title='前瞻文章', url=new_url, source='nga', category='hot', reply_count=20,
        )
        tool = PortalToolLink.objects.create(
            name='NGA', url='https://nga.178.com/thread.php?fid=7', url_hash='legacy-tool-hash',
        )
        task = MonitorTask.objects.create(name='nga-domain-migration-test', flag=old_url)
        alert = SystemAlert.objects.create(
            category='TEST', subject='nga-domain', level=1, title='test', content=old_url,
        )
        event = PortalEvent.objects.create(
            title='NGA event', url=old_url, url_hash='legacy-event-hash', source='nga', tag='nga',
        )
        target = VideoMonitorTarget.objects.create(
            name='NGA', tag='nga', platform='nga', target_url=old_url,
            target_url_hash='legacy-target-hash',
        )
        video = PortalVideo.objects.create(
            title='NGA video', url=old_url, url_hash='legacy-video-hash', target=target,
        )

        migration = importlib.import_module('botend.migrations.0112_replace_nga_domain')
        migration.replace_nga_domain(apps, Mock())

        self.assertFalse(WowArticle.objects.filter(pk=old.pk).exists())
        current.refresh_from_db()
        self.assertEqual(current.url, new_url)
        self.assertEqual(current.category, 'nga')
        self.assertEqual(current.author, 'nga前瞻区')
        self.assertEqual(current.reply_count, 21)
        self.assertEqual(current.content, f'原文链接：{new_url}')
        tool.refresh_from_db()
        self.assertEqual(tool.url, 'https://bbs.nga.cn/thread.php?fid=7')
        task.refresh_from_db()
        alert.refresh_from_db()
        self.assertNotIn('https://nga.178.com/', task.flag)
        self.assertNotIn('https://nga.178.com/', alert.content)
        event.refresh_from_db()
        target.refresh_from_db()
        video.refresh_from_db()
        self.assertEqual(event.url, new_url)
        self.assertEqual(target.target_url, new_url)
        self.assertEqual(video.url, new_url)
        for obj, field in (
            (event, 'url_hash'), (target, 'target_url_hash'), (video, 'url_hash'),
        ):
            self.assertEqual(getattr(obj, field), migration._url_hash(new_url))
