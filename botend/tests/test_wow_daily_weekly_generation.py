"""Real monitor -> central snapshots -> generator -> HTML/ORM/API integration."""
import json
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from django.test import TestCase
from django.utils import timezone

from botend.models import MonitorTask, SeasonMeta, WowArticle, WowDailyReport, WowTalentVersion
from botend.services import rio_statistics as rio, rio_statistics_views as views
from botend.tests import test_wow_daily_rio_section as source_fixtures
from botend.wow_daily_report.generator import generate_wow_daily_report


class WednesdayWeeklyGenerationTests(TestCase):
    day = date(2026, 11, 10)
    fetched = '2026-11-10T23:15:00+00:00'
    upstream = '2026-11-10T20:08:00Z'
    metadata = source_fixtures.WowDailyRioSectionTests.metadata
    publish = source_fixtures.WowDailyRioSectionTests.publish

    def setUp(self):
        SeasonMeta.objects.update(is_active=False)
        WowTalentVersion.objects.update(is_active=False)
        self.season = SeasonMeta.objects.create(season_key='weekly-integration', season_name='中央赛季',
            rio_season='season-mn-2', game_build='12.1.5.70000', is_active=True,
            mplus_zone_id=1, raid_zone_id=2)
        WowTalentVersion.objects.create(key='retail-weekly', branch='retail', is_active=True, current_build='12.1.5.70000')
        temp = tempfile.TemporaryDirectory(prefix='weekly-integration-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / 'central'
        config = self.settings(RIO_STATISTICS_ROOT=self.root, BASE_DIR=self.base / 'web')
        config.enable(); self.addCleanup(config.disable)
        clock = patch.object(timezone, 'now', return_value=datetime.fromisoformat('2026-11-11T00:30:00+00:00'))
        clock.start(); self.addCleanup(clock.stop)
        no_network = patch('requests.sessions.Session.request', side_effect=AssertionError('report must not collect or call AI'))
        no_network.start(); self.addCleanup(no_network.stop)
        views.clear_cache(); self.addCleanup(views.clear_cache)
        start = self.day-timedelta(days=14)
        for region, factor in [('cn', 1), ('world', 3)]:
            self.publish(region, [(str(start+timedelta(days=i)), (100+i*i)*factor) for i in range(16)])

    def central_files(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def test_normal_wednesday_monitor_publishes_special_without_new_task_or_collection(self):
        from botend.controller.plugins.wow.WowDailyReportMonitor import WowDailyReportMonitor
        before = self.central_files()
        task = MonitorTask.objects.create(name='WowDailyReportMonitor', type=27, target='')
        with timezone.override(ZoneInfo('America/New_York')):
            self.assertTrue(WowDailyReportMonitor(None, task).scan(''))
        row = WowDailyReport.objects.get(report_date=self.day)
        ext = json.loads(row.ext_json)
        self.assertEqual(ext['edition']['kind'], 'wednesday_weekly')
        self.assertEqual(ext['edition']['publication_date'], '2026-11-11')
        self.assertEqual(ext['edition']['period_start'], '2026-11-04')
        self.assertEqual(ext['edition']['period_end'], '2026-11-10')
        self.assertEqual(ext['sections']['rio']['count'], 2)
        self.assertFalse(ext['sections']['rio']['summary_llm_ok'])
        self.assertEqual(ext['sections']['rio']['summary_error'], 'summary_disabled')
        self.assertTrue(ext['llm_enabled'])
        self.assertEqual(self.central_files(), before)
        refs = ext['sections']['rio']['references']
        self.assertEqual(len(refs), 2)
        self.assertFalse(any(k in ref for ref in refs for k in ['population', 'weekly', 'days', 'net_change']))
        html = (self.base / 'web' / 'static' / row.md_path).read_text()
        dom = BeautifulSoup(html, 'html.parser')
        self.assertEqual(dom.select_one('h1').get_text(), '魔兽世界周报')
        self.assertEqual(len(dom.select('article[data-region]')), 2)
        self.assertIn('2026-11-04', dom.get_text())
        self.assertIn('2026-11-10', dom.get_text())
        self.assertNotIn('325', dom.select_one('[data-region="cn"]').get_text())  # next Wednesday is excluded
        self.assertIn('+147', dom.select_one('[data-region="cn"]').get_text())
        task.refresh_from_db(); self.assertTrue(task.flag.startswith('2026-11-10@'))
        self.assertEqual(WowDailyReport.objects.count(), 1)
        api = self.client.get('/portal/api/daily-report/latest/').json()['data']
        self.assertEqual(api['report_date'], '2026-11-10')
        self.assertEqual(api['edition'], ext['edition'])
        self.assertIn('11月11日', api['title'])
        self.assertIn('周报', api['title'])

    def test_weekly_data_does_not_expand_news_to_the_whole_week(self):
        for day, title in [(date(2026, 11, 4), '周内旧新闻不混入单日栏目'), (self.day, '周二当日新闻保留')]:
            WowArticle.objects.create(title=title, url='https://example.test/'+day.isoformat(),
                content='<p>原始正文</p>', publish_time=datetime.combine(day, datetime.min.time(), tzinfo=ZoneInfo('Asia/Shanghai')),
                source='wowhead', category='news', is_active=True)
        before = list(WowArticle.objects.values())
        result = generate_wow_daily_report(report_date=self.day, use_llm=False)
        html = Path(result['full_path']).read_text()
        self.assertIn('周二当日新闻保留', html)
        self.assertNotIn('周内旧新闻不混入单日栏目', html)
        self.assertEqual(result['ext']['sections']['news']['count'], 1)
        self.assertEqual(list(WowArticle.objects.values()), before)
        self.assertEqual(len(result['ext']['sections']['rio']['references']), 2)

    def test_other_publication_days_keep_daily_layout_and_deltas(self):
        day = self.day+timedelta(days=1)
        result = generate_wow_daily_report(report_date=day, use_llm=False)
        dom = BeautifulSoup(Path(result['full_path']).read_text(), 'html.parser')
        self.assertEqual(result['ext']['edition']['kind'], 'daily')
        self.assertEqual(dom.select_one('h1').get_text(), '魔兽世界日报')
        self.assertIn('较前一日', dom.select_one('[data-region="cn"]').get_text())
        self.assertNotIn('本周净变化', dom.select_one('[data-region="cn"]').get_text())
        self.assertFalse(dom.select('article[data-region] table'))

    def test_late_regeneration_keeps_report_day_contract_and_frozen_references(self):
        first = generate_wow_daily_report(report_date=self.day, use_llm=False)
        refs = first['ext']['sections']['rio']['references']
        SeasonMeta.objects.filter(pk=self.season.pk).update(is_active=False)
        views.clear_cache()
        with patch.object(timezone, 'now', return_value=datetime.fromisoformat('2026-11-13T01:00:00+00:00')):
            second = generate_wow_daily_report(report_date=self.day, use_llm=False)
        self.assertEqual(second['ext']['edition'], first['ext']['edition'])
        self.assertEqual(second['ext']['sections']['rio']['references'], refs)
        dom = BeautifulSoup(Path(second['full_path']).read_text(), 'html.parser')
        self.assertEqual(dom.select_one('h1').get_text(), '魔兽世界周报')
        self.assertIn('+147', dom.select_one('[data-region="cn"]').get_text())
