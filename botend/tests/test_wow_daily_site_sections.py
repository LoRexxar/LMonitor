"""Daily content integration uses real central rows; no HTTP or AI required."""
import json
import tempfile
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings

from botend.models import PortalEvent, WowTodaySnapshot, WowTodayCardSetting, WowHotfixReport
from botend.wow_daily_report.generator import generate_wow_daily_report
from botend.wow_daily_report.site_sections import collect_site_sections


class DailySiteSectionsTests(TestCase):
    def setUp(self):
        self.day = date(2026, 7, 1)
        self.noon = datetime(2026, 7, 1, 12, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.snapshot = WowTodaySnapshot.objects.create(snapshot_date=self.day-timedelta(days=1),
            fetched_at=self.noon, region='na', game_version='retail', sections_json=[{
                'key': 'dungeons-raids', 'name': '地下城与团队副本', 'modules': [
                    {'key': 'delves', 'name': '丰裕地下堡', 'items': [{'name': '真实中央地下堡'}]},
                    {'key': 'hidden', 'name': '隐藏卡片', 'items': [{'name': '不得泄露'}]}]}])
        WowTodayCardSetting.objects.create(section_key='dungeons-raids', card_key='hidden', is_visible=False)
        for number, source in enumerate(('db2_holidays', 'seed')):
            PortalEvent.objects.create(title='日历活动' if number==0 else '演示活动不要收录',
                url=f'https://example.com/event/{number}', source=source,
                start_at=self.noon-timedelta(days=1), end_at=self.noon+timedelta(days=1))

    def test_real_generation_adds_daily_context_without_losing_date_or_visibility(self):
        with tempfile.TemporaryDirectory() as root, override_settings(BASE_DIR=root):
            result = generate_wow_daily_report(report_date=self.day, use_llm=False)
            html = open(result['full_path'], encoding='utf-8').read()
            self.assertIn('真实中央地下堡', html)
            self.assertIn('日历活动', html)
            self.assertIn('北美正式服', html)
            self.assertIn('2026-06-30', html)
            self.assertIn('不是国服同日轮换', html)
            self.assertNotIn('不得泄露', html)
            self.assertNotIn('演示活动不要收录', html)
            self.assertEqual(result['ext']['sections']['today']['count'], 1)
            self.assertEqual(result['ext']['sections']['events']['count'], 1)
            self.assertEqual(result['ext']['html_renderer'], 'daily_editorial_v2')

    def test_daily_report_omits_selected_rotation_cards_without_changing_central_data(self):
        from botend.wow_daily_report.site_sections import collect_today_section
        excluded = {
            'season-caps': '赛季上限', 'mn-notable-world-quests': '值得关注的世界任务',
            'empowered-abundance': '强化丰裕事件', 'mn-heroic-world-tier': '英雄世界层级',
            'mn-ritual-site': '当前仪式场地', 'mn-void-assault': '虚空侵袭',
            'mn-cursed-surges': '诅咒涌动', 'mn-aqir-invasion': '亚基入侵',
            'epiceliteworldmn': '午夜之境世界首领', 'darkmoon-faire': '暗月马戏团',
        }
        modules = [{'key': key, 'name': name, 'items': [{'name': name+'内容'}]}
                   for key, name in excluded.items()]
        kept = [('mythicaffix', '大秘境词缀'), ('mn-bountiful-delves', '丰裕地下堡'), ('holiday', '节日')]
        modules += [{'key': key, 'name': name, 'items': [{'name': name+'内容'}]} for key, name in kept]
        modules[-1]['items'].extend([{'name': '暗月马戏团'}, {'name': 'Darkmoon Faire'}])
        WowTodaySnapshot.objects.filter(pk=self.snapshot.pk).update(sections_json=[{
            'key': 'dungeons-raids', 'name': '轮换', 'modules': modules}])
        WowTodayCardSetting.objects.create(section_key='dungeons-raids', card_key='season-caps', display_name='改过显示名的赛季上限')
        before = list(WowTodaySnapshot.objects.values())
        settings_before = list(WowTodayCardSetting.objects.values())
        section = collect_today_section(self.day)
        self.assertEqual([item['reference']['card_key'] for item in section['items']], [key for key, _ in kept])
        with tempfile.TemporaryDirectory() as root, override_settings(BASE_DIR=root):
            result = generate_wow_daily_report(report_date=self.day, use_llm=False)
            html = open(result['full_path'], encoding='utf-8').read()
            self.assertEqual(result['ext']['sections']['today']['count'], 3)
            for name in excluded.values():
                self.assertNotIn(name, html)
            self.assertNotIn('Darkmoon Faire', html)
            for _, name in kept:
                self.assertIn(name, html)
        self.assertEqual(list(WowTodaySnapshot.objects.values()), before)
        self.assertEqual(list(WowTodayCardSetting.objects.values()), settings_before)

    def test_cutoff_html_does_not_display_internal_snapshot_notes(self):
        from botend.wow_daily_report.generator import _render_cutoff_items
        html = _render_cutoff_items([{'region_label': '国服', 'cutoff_0_1': 3990,
            'source_updated_at': '2026-07-01 10:00',
            'snapshot_note': '沿用本日已发布快照；原记录未保存来源时区'}])
        self.assertIn('3990', html)
        self.assertIn('2026-07-01 10:00', html)
        self.assertNotIn('沿用本日已发布快照', html)
        self.assertNotIn('原记录未保存来源时区', html)

    def test_event_links_respect_the_existing_public_calendar_domain_gate(self):
        from botend.wow_daily_report.site_sections import collect_event_section
        from botend.wow_daily_report.generator import _render_site_items
        row = PortalEvent.objects.get(source='db2_holidays')
        for url in ('https://wago.tools/db2/Holidays?build=123', 'javascript:alert(1)', '//evil.test/x'):
            PortalEvent.objects.filter(pk=row.pk).update(url=url)
            section = collect_event_section(self.day)
            self.assertEqual(section['items'][0]['url'], '')
            self.assertIn('日历活动', _render_site_items(section['items']))
            self.assertNotIn('<a ', _render_site_items(section['items']))
        for url in ('https://www.wowhead.com/event=1', '/#section-events'):
            PortalEvent.objects.filter(pk=row.pk).update(url=url)
            self.assertEqual(collect_event_section(self.day)['items'][0]['url'], url)

    def test_collectors_read_only_exact_day_no_old_or_future_snapshot(self):
        before = list(WowTodaySnapshot.objects.values())
        self.assertEqual([s['key'] for s in collect_site_sections(self.day)], ['today', 'events'])
        self.assertEqual(list(WowTodaySnapshot.objects.values()), before)
        WowTodaySnapshot.objects.filter(pk=self.snapshot.pk).update(fetched_at=self.noon+timedelta(days=1))
        self.assertNotIn('today', [s['key'] for s in collect_site_sections(self.day)])
        PortalEvent.objects.filter(source='db2_holidays').update(end_at=self.noon.replace(hour=0))
        self.assertNotIn('events', [s['key'] for s in collect_site_sections(self.day)])

    def test_updates_require_detected_day_completed_event_and_real_public_file(self):
        from pathlib import Path
        from botend.models import WowWagoHotfixEvent
        row = WowHotfixReport.objects.create(branch='wowt', region_id=1, to_push=123,
            summary_title='PTR 来源更新', collection_complete=True, content_html_path='portal/reports/test.html',
            entry_count=2, table_count=1)
        event = WowWagoHotfixEvent.objects.create(branch='wowt', region_id=1, report=row,
            to_push=123, detected_at=self.noon-timedelta(days=1), status='report_generated')
        with tempfile.TemporaryDirectory() as root, override_settings(BASE_DIR=root):
            target = Path(root)/'static/portal/reports/test.html'
            target.parent.mkdir(parents=True)
            target.write_text('<!doctype html><html><body>测试已发布报告</body></html>')
            # Neither report creation nor polling updates imply today's discovery.
            WowHotfixReport.objects.filter(pk=row.pk).update(created_at=self.noon, updated_at=self.noon)
            self.assertNotIn('updates', [s['key'] for s in collect_site_sections(self.day)])
            WowWagoHotfixEvent.objects.filter(pk=event.pk).update(detected_at=self.noon)
            section = next(s for s in collect_site_sections(self.day) if s['key']=='updates')
            self.assertIn('PTR', section['items'][0]['source'])
            self.assertEqual(section['items'][0]['url'], f'/portal/wow-hotfix-report/{row.pk}/')
            WowHotfixReport.objects.filter(pk=row.pk).update(collection_complete=False)
            self.assertNotIn('updates', [s['key'] for s in collect_site_sections(self.day)])
            WowHotfixReport.objects.filter(pk=row.pk).update(collection_complete=True)
            WowWagoHotfixEvent.objects.filter(pk=event.pk).update(status='failed')
            self.assertNotIn('updates', [s['key'] for s in collect_site_sections(self.day)])
            WowWagoHotfixEvent.objects.filter(pk=event.pk).update(status='report_generated')
            target.unlink()
            self.assertNotIn('updates', [s['key'] for s in collect_site_sections(self.day)])

    def test_cutoff_keeps_local_source_time_and_rejects_future_snapshot(self):
        from botend.models import PortalMplusSeasonCutoff
        from botend.wow_daily_report.generator import collect_cutoff_section
        row = PortalMplusSeasonCutoff.objects.create(season='test-daily', region='cn',
            cutoff_0_1=4000, source_updated_at='2026-07-01T04:00:00Z')
        items = collect_cutoff_section(self.day)['items']
        self.assertEqual(items[0]['source_updated_at'], '2026-07-01 12:00')
        PortalMplusSeasonCutoff.objects.filter(pk=row.pk).update(source_updated_at='2026-07-02T04:00:00Z')
        self.assertEqual(collect_cutoff_section(self.day)['items'], [])
        from botend.wow_daily_report.generator import _fmt_source_time
        raw = 'Mon Nov 02 2026 16:10:31 GMT+0000 (Coordinated Universal Time)'
        self.assertEqual(_fmt_source_time(raw), '2026-11-03 00:10')
        PortalMplusSeasonCutoff.objects.filter(pk=row.pk).update(source_updated_at='Thu Jul 02 2026 04:00:00 GMT+0000 (Coordinated Universal Time)')
        self.assertEqual(collect_cutoff_section(self.day)['items'], [])
        snapshot = {'cutoffs': {'by_region': {'cn': {'season': 'test-daily',
            'cutoff_0_1': 3990, 'cutoff_1': 3500, 'source_updated_at': '2026-07-01 10:00'}}}}
        retained = collect_cutoff_section(self.day, published_ext=snapshot)['items']
        self.assertEqual(retained[0]['cutoff_0_1'], 3990)
        self.assertEqual(retained[0]['source_updated_at'], '2026-07-01 10:00')
        self.assertIn('原记录未保存来源时区', retained[0]['snapshot_note'])
