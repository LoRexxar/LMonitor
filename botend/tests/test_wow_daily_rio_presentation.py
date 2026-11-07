"""RIO daily UI contracts, separate from central source/archive tests."""
import json
from datetime import date

from bs4 import BeautifulSoup
from django.test import SimpleTestCase, TestCase

from botend.models import WowDailyReport
from botend.wow_daily_report import generator
from botend.wow_daily_report.presentation import render_daily_document


class DailyRioPresentationTests(SimpleTestCase):
    def item(self, **values):
        return dict(region='cn', region_label='国服', season='season-mn-2', season_name='午夜第2赛季',
                    data_date='2026-11-06', population=1288707, previous_date='2026-11-05',
                    day_delta=2200, week_date='2026-10-30', week_delta=30643,
                    fetched_at='2026-11-07T02:15:25+08:00', upstream_at='2026-11-07T00:08:31+08:00',
                    url='/portal/rio-statistics/?tab=overview&region=cn',
                    heatmap_url='/portal/rio-statistics/?tab=heatmaps&region=cn', stale=False, **values)

    def test_region_card_keeps_counts_comparison_dates_and_context_links(self):
        dom = BeautifulSoup(generator._render_rio_items([self.item()]), 'html.parser')
        self.assertEqual(dom.select_one('[data-region]')['data-region'], 'cn')
        self.assertIn('1,288,707', dom.select_one('.rio-report-population').get_text())
        self.assertEqual([n.get_text(strip=True) for n in dom.select('.rio-report-metrics dd')], ['+2,200', '+30,643'])
        self.assertIn('2026-11-05', dom.get_text())
        self.assertIn('2026-10-30', dom.get_text())
        self.assertEqual(len(dom.select('a')), 2)
        self.assertNotIn('0.1%', dom.get_text())

    def test_absent_comparison_is_not_zero_and_zero_is_not_missing(self):
        item = self.item(); item.update(day_delta=None, week_delta=0)
        dom = BeautifulSoup(generator._render_rio_items([item]), 'html.parser')
        self.assertEqual([n.get_text(strip=True) for n in dom.select('.rio-report-metrics dd')], ['—', '0'])
        self.assertIn('无可比数据', dom.get_text())

    def test_source_metadata_and_unsafe_values_are_escaped(self):
        item = self.item(); item.update(region_label='<script>x</script>', season_name='<b>test</b>',
                                        url='javascript:alert(1)', heatmap_url='//evil.test')
        dom = BeautifulSoup(generator._render_rio_items([item]), 'html.parser')
        self.assertFalse(dom.select('script, b, a'))
        self.assertIn('<script>x</script>', dom.get_text())
        self.assertIn('2026-11-07 02:15', dom.get_text())
        item['stale'] = True
        self.assertIn('保留快照', BeautifulSoup(generator._render_rio_items([item]), 'html.parser').get_text())

    def test_new_section_has_independent_region_count_and_no_ai_summary(self):
        dom = BeautifulSoup(render_daily_document(date(2026, 11, 6), [{'key': 'rio',
            'title': '大秘境参与观察', 'items': [self.item()], 'summary': '不得生成的AI结论'}],
            {'rio': generator._render_rio_items}, generated_at='2026-11-07'), 'html.parser')
        self.assertEqual(dom.select_one('.overview-list li a').get_text(), '数据观察')
        self.assertIn('1 个地区', dom.select_one('.section-count').get_text())
        self.assertNotIn('不得生成的AI结论', dom.get_text())
        self.assertEqual(dom.select_one('style#daily-rio-style').name, 'style')

    def test_empty_section_and_directory_entry_are_both_omitted(self):
        dom = BeautifulSoup(render_daily_document(date(2026, 11, 6), [{'key': 'rio', 'items': []}],
            {'rio': lambda _: self.fail('empty renderer called')}, generated_at='2026-11-07'), 'html.parser')
        self.assertFalse(dom.select('[data-section="rio"]'))


class DailyRioApiTests(TestCase):
    def test_latest_home_projection_exposes_new_section_without_mixing_counts(self):
        WowDailyReport.objects.create(report_date=date(2026, 11, 6),
            md_path='portal/reports/wow_daily_report_2026-11-06.html',
            ext_json=json.dumps({'sections': {'news': {'count': 15}, 'rio': {
                'title': '大秘境参与观察', 'count': 2, 'references': []}}}))
        sections = self.client.get('/portal/api/daily-report/latest/').json()['data']['sections']
        projected = {s['key']: s for s in sections}
        self.assertEqual(projected['rio']['label'], '数据观察')
        self.assertEqual(projected['rio']['count'], 2)
        self.assertEqual(projected['news']['count'], 15)


class DailyRioIncrementalTests(SimpleTestCase):
    def document(self):
        body = '<article><p>原文 &amp; <b>AI 摘要不变</b></p><img src="https://example.test/a.jpg"></article>'
        return render_daily_document(date(2026, 11, 6), [{'key': 'news', 'items': [{'title': '新闻'}]}],
            {'news': lambda _: body}, generated_at='2026-11-07 06:41:26')

    def section(self):
        return {'key': 'rio', 'title': '大秘境参与观察', 'items': [DailyRioPresentationTests().item()]}

    def test_incremental_merge_preserves_all_unrelated_bytes_and_is_idempotent(self):
        from botend.wow_daily_report.rio_refresh import merge_rio_section, without_rio
        original = self.document()
        updated = merge_rio_section(original, date(2026, 11, 6), self.section())
        self.assertEqual(without_rio(updated), without_rio(original))
        self.assertIn('原文 &amp; <b>AI 摘要不变</b>', updated)
        self.assertEqual(merge_rio_section(updated, date(2026, 11, 6), self.section()), updated)
        dom = BeautifulSoup(updated, 'html.parser')
        self.assertEqual([n['data-section'] for n in dom.select('.daily-section')], ['rio', 'news'])
        self.assertEqual(len(dom.select('style#daily-rio-style')), 1)
        self.assertEqual(dom.select_one('.overview-list a')['href'], '#section-rio')

    def test_merge_rejects_unrecognised_or_duplicate_shell(self):
        from botend.wow_daily_report.rio_refresh import merge_rio_section
        for value in [self.document().replace('id="edition-content"', 'id="other"'),
                      self.document() + '<!--daily-rio:section-->unclosed',
                      self.document() + '<section data-section="rio"></section>']:
            with self.subTest(value=value[-60:]), self.assertRaises(ValueError):
                merge_rio_section(value, date(2026, 11, 6), self.section())

    def test_empty_collection_does_not_remove_published_content(self):
        from botend.wow_daily_report.rio_refresh import merge_rio_section
        original = self.document()
        self.assertEqual(merge_rio_section(original, date(2026, 11, 6), {'key': 'rio', 'items': []}), original)


    def test_cross_node_component_markers_never_delete_news_or_navigation(self):
        from botend.wow_daily_report.rio_refresh import merge_rio_section
        original = self.document()
        valid = merge_rio_section(original, date(2026, 11, 6), self.section())
        section_close = '<!--/daily-rio:section-->'
        nav_close = '<!--/daily-rio:nav-->'
        style_close = '<!--/daily-rio:style-->'
        mutations = [
            valid.replace(section_close, '', 1).replace('\n</div>\n<footer class="edition-footer">', section_close+'\n</div>\n<footer class="edition-footer">', 1),
            valid.replace(nav_close, '', 1).replace('</ul></nav></aside>', nav_close+'</ul></nav></aside>', 1),
            valid.replace(style_close, '', 1).replace('</head>', '</head>'+style_close, 1),
            valid.replace('</section>'+section_close, section_close+'</section>', 1),
        ]
        for malformed in mutations:
            self.assertNotEqual(malformed, valid)
            with self.subTest(tail=malformed[-120:]), self.assertRaises(ValueError):
                merge_rio_section(malformed, date(2026, 11, 6), self.section())
            self.assertIn('原文 &amp; <b>AI 摘要不变</b>', malformed)


    def test_self_closing_nonvoid_and_duplicate_attributes_are_rejected(self):
        from botend.wow_daily_report.rio_refresh import merge_rio_section
        valid = merge_rio_section(self.document(), date(2026, 11, 6), self.section())
        for extra in ['<div/>', '<img src="one" src="two"/>']:
            malformed = valid.replace('</section><!--/daily-rio:section-->', extra+'</section><!--/daily-rio:section-->', 1)
            self.assertNotEqual(malformed, valid)
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                merge_rio_section(malformed, date(2026, 11, 6), self.section())
