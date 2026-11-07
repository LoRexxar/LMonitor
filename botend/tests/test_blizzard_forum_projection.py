"""Central forum facts feed the one public projection without title rewriting."""
import json
import tempfile
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from botend.models import BlizzardForumCategory, BlizzardForumTopic, WowArticle
from botend.services import news_snapshot as ns
from botend.wow_daily_report.generator import collect_news_section, render_daily_html

FORUM='https://us.forums.blizzard.com/en/wow'


class BlizzardForumProjectionTests(TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='bluepost-projection-');self.addCleanup(temp.cleanup)
        config=self.settings(NEWS_SNAPSHOT_ROOT=Path(temp.name)/'news')
        config.enable();self.addCleanup(config.disable)

    def article(self, version='forever', source='blizzard_tracker', number=1):
        category=BlizzardForumCategory.objects.create(forum_url=FORUM, external_id=346+number,
            name='真实板块 '+str(number),slug='board-'+str(number),url=FORUM+'/c/board-'+str(number)+'/'+str(346+number),game_version=version)
        topic=BlizzardForumTopic.objects.create(forum_url=FORUM, external_id=2400000+number,category=category)
        return WowArticle.objects.create(title='Original title '+str(number),title_cn='原中文标题 '+str(number),
            source=source,category='bluepost',url=FORUM+'/t/thread/'+str(2400000+number)+'/1',
            content='<p>原正文不可改动</p>',content_cn='原译文不可改动',
            blizzard_topic=topic,publish_time=timezone.now())

    def test_live_detail_and_readonly_snapshot_share_labels_without_mutating_titles(self):
        for number,(version,label) in enumerate([('retail','正式服'),('forever','无限服'),('test','测试服')],1):
            article=self.article(version,number=number)
            before=WowArticle.objects.values().get(pk=article.pk)
            detail=self.client.get(f'/portal/api/article/{article.pk}/').json()['data']
            self.assertEqual((detail['game_version'],detail['game_version_label']),(version,label))
            self.assertEqual(detail['forum_category']['id'],346+number)
            self.assertEqual(detail['title'],article.title)
            self.assertEqual(detail['title_cn'],article.title_cn)
            self.assertEqual(WowArticle.objects.values().get(pk=article.pk),before)
        self.assertTrue(ns.refresh_news_snapshot())
        with self.assertNumQueries(0):
            payload=self.client.get('/portal/api/blueposts/').json()
        rows={row['id']:row for row in payload['data']}
        for number,(version,label) in enumerate([('retail','正式服'),('forever','无限服'),('test','测试服')],1):
            row=next(r for r in rows.values() if r['title']=='Original title '+str(number))
            self.assertEqual((row['game_version'],row['game_version_label']),(version,label))

    def test_unknown_and_other_sources_are_not_labeled(self):
        unknown=self.article('',number=1)
        foreign=self.article('forever',source='wowhead',number=2)
        for article in [unknown,foreign]:
            detail=self.client.get(f'/portal/api/article/{article.pk}/').json()['data']
            self.assertFalse(detail.get('game_version_label'))
        self.assertTrue(ns.refresh_news_snapshot())
        for row in self.client.get('/portal/api/news/').json()['data']:
            self.assertFalse(row.get('game_version_label'))

    def test_snapshot_publisher_joins_metadata_once_and_never_reads_full_body(self):
        for number in range(1,7):self.article(number=number)
        with CaptureQueriesContext(connection) as captured:rows=list(ns.lightweight_rows())
        self.assertEqual(len(captured),1)
        self.assertEqual(len(rows),6)
        self.assertTrue(all(row.get('blizzard_topic__category__game_version')=='forever' for row in rows))
        self.assertNotIn('content',rows[0])
        self.assertNotIn('content_blocks',rows[0])

    def test_daily_news_reuses_the_same_label_and_preserves_source_fact(self):
        article=self.article('forever')
        before=WowArticle.objects.values().get(pk=article.pk)
        section=collect_news_section(timezone.localdate())
        item=next(i for i in section['items'] if i['url']==article.url)
        self.assertEqual(item['game_version_label'],'无限服')
        dom=BeautifulSoup(render_daily_html(timezone.localdate(),[section]),'html.parser')
        card=dom.select_one('.news-card')
        self.assertIn('[无限服]',card.select_one('.meta').get_text())
        self.assertEqual(card.select_one('.news-title').get_text(),article.title_cn)
        self.assertEqual(WowArticle.objects.values().get(pk=article.pk),before)
