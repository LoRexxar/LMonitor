"""Explicit Wednesday editions: rendering and public homepage API contracts."""
import copy
import json
from datetime import date, timedelta

from bs4 import BeautifulSoup
from django.test import SimpleTestCase, TestCase

from botend.models import WowDailyReport
from botend.wow_daily_report.presentation import render_daily_document


EDITION = {'kind': 'wednesday_weekly', 'publication_date': '2026-11-11',
           'period_start': '2026-11-04', 'period_end': '2026-11-10', 'timezone': 'Asia/Shanghai'}


def weekly_item():
    return {
        'region': 'cn', 'region_label': '国服', 'season_name': '午夜第2赛季',
        'data_date': '2026-11-10', 'population': 9999999,
        'fetched_at': '2026-11-11T02:15:25+08:00', 'upstream_at': '2026-11-11T00:08:31+08:00',
        'url': '/portal/rio-statistics/?tab=overview&region=cn',
        'heatmap_url': '/portal/rio-statistics/?tab=heatmaps&region=cn',
        'weekly': dict(period_start='2026-11-04', period_end='2026-11-10', baseline_date='2026-11-03',
                       previous_baseline_date='2026-10-27', start_population=100, end_population=107,
                       net_change=7, previous_net_change=10, change_vs_previous=-3, coverage_days=7,
                       days=[dict(date=(date(2026, 11, 4)+timedelta(days=i)).isoformat(),
                                  population=101+i, change=1) for i in range(7)])}


class WednesdayPresentationTests(SimpleTestCase):
    def document(self, edition=None, day=date(2026, 11, 10)):
        return render_daily_document(day, [], {}, generated_at='2026-11-11 06:30', edition=edition)

    def test_legacy_tuesday_and_daily_edition_keep_daily_identity(self):
        for day in (date(2026, 11, 10), date(2026, 11, 6)):
            dom = BeautifulSoup(self.document(day=day), 'html.parser')
            self.assertEqual(dom.h1.get_text(), '魔兽世界日报')
            self.assertEqual(dom.select_one('.edition-kicker').get_text(), 'WOW DAILY · 每日读本')
            self.assertEqual(dom.select_one('.edition-date').get_text(), day.isoformat())
            self.assertFalse(dom.select('.edition-period'))
        daily = {'kind': 'daily', 'publication_date': '2026-11-07', 'period_start': '2026-11-06',
                 'period_end': '2026-11-06', 'timezone': 'Asia/Shanghai'}
        self.assertEqual(self.document(daily, date(2026, 11, 6)), self.document(day=date(2026, 11, 6)))

    def test_explicit_weekly_header_is_publication_dated_and_explains_mixed_windows(self):
        dom = BeautifulSoup(self.document(EDITION), 'html.parser')
        self.assertEqual(dom.h1.get_text(), '魔兽世界周报')
        self.assertEqual(dom.select_one('.edition-kicker').get_text(), 'WOW WEEKLY · 周三特刊')
        self.assertEqual(dom.select_one('.edition-date').get_text(), '2026-11-11')
        self.assertEqual(dom.select_one('.edition-date-label').get_text(), '发布日期')
        self.assertIn('2026-11-04—2026-11-10', dom.select_one('.edition-period').get_text())
        self.assertIn('周三—周二，北京时间', dom.get_text())
        self.assertIn('新闻、活动等仍按日报内容日 2026-11-10 收录', dom.get_text())
        self.assertIn('本期暂无收录内容', dom.get_text())

    def test_invalid_explicit_edition_is_rejected_not_silently_inferred(self):
        for key, value in [('kind', 'daily'), ('period_start', '<script>x</script>'),
                           ('publication_date', '2026-11-12'), ('timezone', 'UTC')]:
            edition = dict(EDITION, **{key: value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.document(edition)
        for edition in ({}, 'wednesday_weekly', dict(EDITION, extra='x')):
            with self.subTest(edition=edition), self.assertRaises(ValueError):
                self.document(edition)

    def render_items(self, items):
        from botend.wow_daily_report.rio_weekly_presentation import render_weekly_rio_items
        return render_weekly_rio_items(items)

    def test_weekly_card_shows_exact_values_seven_dates_sources_and_two_links(self):
        dom = BeautifulSoup(self.render_items([weekly_item()]), 'html.parser')
        self.assertEqual(dom.select_one('.rio-weekly-population strong').get_text(), '107')
        self.assertEqual([n.get_text() for n in dom.select('.rio-weekly-metrics dd')], ['+7', '+10', '-3'])
        self.assertEqual([n.get_text() for n in dom.select('tbody th')],
                         [f'11月{day}日' for day in range(4, 11)])
        self.assertEqual(len(dom.select('tbody tr')), 7)
        self.assertIn('周内有效日期 7/7', dom.get_text())
        self.assertIn('周初基线（2026-11-03，上周二）100', dom.get_text())
        self.assertIn('2026-11-11 02:15', dom.get_text())
        self.assertIn('2026-11-11 00:08', dom.get_text())
        self.assertEqual([a['href'] for a in dom.select('a')], [weekly_item()['url'], weekly_item()['heatmap_url']])
        self.assertNotIn('9,999,999', dom.get_text())
        self.assertNotIn('%', dom.get_text())
        self.assertNotIn('新增玩家', dom.get_text())

    def test_missing_tuesday_does_not_use_monday_or_shift_fixed_period(self):
        item = weekly_item()
        item.update(data_date='2026-11-09', population=106, stale=True)
        item['weekly'].update(end_population=None, net_change=None, change_vs_previous=None, coverage_days=6)
        item['weekly']['days'][-1].update(population=None, change=None)
        dom = BeautifulSoup(self.render_items([item]), 'html.parser')
        self.assertEqual(dom.select_one('.rio-weekly-population strong').get_text(), '—')
        self.assertIn('2026-11-04—2026-11-10', dom.get_text())
        self.assertIn('数据日期 2026-11-09', dom.get_text())
        self.assertIn('周内有效日期 6/7', dom.get_text())
        self.assertIn('缺失', dom.get_text())
        self.assertIn('不补零', dom.get_text())
        self.assertIn('保留快照', dom.get_text())
        self.assertEqual([n.get_text() for n in dom.select('tbody tr')[-1].select('td')], ['—', '—'])

    def test_zero_negative_escape_and_no_ai_summary_or_combined_region_total(self):
        item = weekly_item()
        item.update(region_label='<script>x</script>', season_name='<b>season</b>',
                    url='javascript:alert(1)', heatmap_url='//evil.test')
        item['weekly'].update(end_population=0, net_change=0, previous_net_change=-5, change_vs_previous=5)
        other = copy.deepcopy(item); other.update(region='world', region_label='全球')
        dom = BeautifulSoup(self.render_items([item, other]), 'html.parser')
        self.assertEqual(len(dom.select('article')), 2)
        self.assertFalse(dom.select('script, b, a, section'))
        self.assertIn('<script>x</script>', dom.get_text())
        self.assertEqual([n.get_text() for n in dom.select('.rio-weekly-metrics dd')][:3], ['0', '-5', '+5'])
        self.assertEqual(dom.select_one('.rio-weekly-population strong').get_text(), '0')
        document = render_daily_document(date(2026, 11, 10), [{'key': 'rio', 'title': '大秘境一周观察',
            'items': [item], 'summary': '不能展示的AI结论'}], {'rio': self.render_items},
            generated_at='2026-11-11', edition=EDITION)
        self.assertNotIn('不能展示的AI结论', document)
        self.assertEqual(BeautifulSoup(document, 'html.parser').select_one('.daily-nav a').get_text(), '数据周报')
        from botend.wow_daily_report.rio_refresh import without_rio
        self.assertNotIn('rio-weekly-card', without_rio(document))


class WednesdayHomepageApiTests(TestCase):
    def test_only_valid_persisted_edition_changes_title_and_rio_badge(self):
        row = WowDailyReport.objects.create(report_date=date(2026, 11, 10),
            md_path='portal/reports/wow_daily_report_2026-11-10.html')
        for edition in (None, EDITION, dict(EDITION, publication_date='2026-11-12'), {}):
            ext = {'sections': {'rio': {'count': 2}, 'news': {'count': 3}}}
            if edition is not None:
                ext['edition'] = edition
            row.ext_json = json.dumps(ext); row.save(update_fields=['ext_json'])
            payload = self.client.get('/portal/api/daily-report/latest/').json()['data']
            weekly = edition == EDITION
            self.assertEqual(payload['title'], '11月11日魔兽世界周报' if weekly else '11月10日魔兽世界日报')
            self.assertEqual(payload.get('edition'), EDITION if weekly else None)
            self.assertEqual(payload['report_date'], '2026-11-10')
            self.assertEqual(payload['current_url'], '/portal/daily-report/')
            self.assertIn('wow_daily_report_2026-11-10.html?v=', payload['url'])
            self.assertEqual(payload['sections'][0]['label'], '数据周报' if weekly else '数据观察')
            self.assertEqual(payload['sections'][1]['count'], 3)
