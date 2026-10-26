"""来源组件必须保留关系语义，旧稿修复不能覆盖人工修改。"""
import hashlib
from types import SimpleNamespace
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.test import SimpleTestCase

from botend.services.class_guide_content import walk_blocks
from botend.services.class_guide_icy import convert_page
from botend.services.class_guide_icy_format import restore_icy_format, stat_widget_data, localize_stat_label, priority_html, legacy_inline_groups
from botend.services.class_guide_markdown import blocks_to_markdown, compile_markdown
from botend.services.class_guide_presentation import prepare_guide_presentation


class IcyFormatTests(SimpleTestCase):
    def test_cached_windows_newlines_match_original_paragraph_boundaries(self):
        soup = BeautifulSoup('<div class="guide-page-content"><p>第一句\r\r\n<strong>后半句</strong>。</p></div>', 'html.parser')
        self.assertEqual(legacy_inline_groups(soup), [['第一句', '后半句 。']])

    def test_source_paragraph_blank_lines_and_numbered_heading_keep_structure(self):
        source = '<div class="guide-page-content"><div class="heading_container heading_number_2"><span>1.</span><h2>属性说明</h2></div><p>优先选择\n\n<strong>急速</strong>和精通，\n\n再考虑其他属性。</p><pre><code>/cast 技能甲\n/cast 技能乙</code></pre></div>'
        result = convert_page(source, 'https://www.icy-veins.com/wow/test')
        self.assertTrue(any(b['type'] == 'heading' and b['title'] == '属性说明' for b in result['blocks']))
        markup = ''.join(b.get('html', '') for b in result['blocks'])
        soup = BeautifulSoup(markup, 'html.parser')
        self.assertTrue(any('优先选择' in p.text and '再考虑其他属性' in p.text for p in soup.select('p')))
        self.assertEqual(next(b for b in result['blocks'] if b['type'] == 'code')['data']['code'], '/cast 技能甲\n/cast 技能乙')

    def test_inline_text_emphasis_and_references_stay_in_one_paragraph(self):
        source = '<div class="guide-page-content"><div>优先选择<strong>急速</strong>，再考虑<span data-iv-tooltip="wow/spell/123">技能甲</span>的收益。</div><h2><span class="heading_number_2">2.</span>后续说明</h2></div>'
        result = convert_page(source, 'https://www.icy-veins.com/wow/test')
        first = BeautifulSoup(result['blocks'][0]['html'], 'html.parser')
        self.assertEqual(len(first.select('p')), 1)
        self.assertEqual(first.get_text(), '优先选择急速，再考虑[[spell:123]]的收益。')
        self.assertNotIn('\n\n急速\n\n', result['content_markdown'])

    def widget(self):
        return '''<div class="stat-priority-widget"><div class="stat-name">Haste</div>
        <div class="stat-separator"><i class="ri-arrow-drop-right-line"></i></div><div class="stat-name">Versatility</div>
        <div class="stat-separator"><i class="ri-equal-line"></i></div><div class="stat-name">Mastery</div>
        <div class="stat-separator"><div class="greater-equal"><i class="ri-arrow-drop-right-line"></i></div></div><div class="stat-name">Critical Strike</div></div>'''

    def test_priority_conversion_and_markdown_roundtrip_keep_relations(self):
        result = convert_page('<div class="guide-page-content">' + self.widget() + '</div>', 'https://www.icy-veins.com/wow/test')
        blocks = compile_markdown(blocks_to_markdown(result['blocks']))
        block = next(b for b in blocks if b['type'] == 'priority')
        self.assertEqual(block['data']['decoded'], {'stat_labels': ['急速', '全能', '精通', '暴击'], 'relation': [1, 2, 3]})
        self.assertTrue(block['data']['converted'])

    def test_unknown_relation_is_rejected_instead_of_guessed(self):
        soup = BeautifulSoup(self.widget().replace('ri-equal-line', 'unknown-icon'), 'html.parser')
        with self.assertRaises(ValueError):
            stat_widget_data(soup)

    def test_thresholds_are_kept(self):
        self.assertEqual(localize_stat_label('Haste to 22%'), '急速（达到 22%）')
        self.assertEqual(localize_stat_label('Haste (to 700+)'), '急速（达到 700+ 等级）')
        self.assertEqual(localize_stat_label('Haste (until 1800 rating)'), '急速（达到 1800 等级）')
        markup = priority_html(['急速', '<精通>'], [2])
        self.assertIn('＝', markup)
        self.assertIn('&lt;精通&gt;', markup)

    def test_performance_panels_keep_labels_scores_and_hidden_body(self):
        source = '<div class="guide-page-content"><button id="raid">Raid</button><div class="performance-overview__panel" aria-labelledby="raid" data-rating="4.5/5" aria-hidden="true"><summary class="performance-header">稳定表现</summary><p>完整评价内容。</p></div></div>'
        result = convert_page(source, 'https://www.icy-veins.com/wow/test')
        rating = next(b for b in walk_blocks(result['blocks']) if b['type'] == 'rating')
        self.assertEqual(rating['title'], 'Raid · 4.5/5')
        self.assertIn('完整评价内容。', result['content_markdown'])

    def test_faq_badge_and_question_have_clear_boundary(self):
        source = '<div class="guide-page-content"><details class="faq-block__dropdown"><summary><span class="faq-block__badge">FAQ</span><span class="faq-block__question">是否有上限？</span></summary><p>完整答案。</p></details></div>'
        result = convert_page(source, 'https://www.icy-veins.com/wow/test')
        self.assertEqual(result['blocks'][0]['title'], 'FAQ：是否有上限？')
        self.assertIn('完整答案。', result['content_markdown'])

    def test_legacy_repair_requires_exact_content_and_paragraphs(self):
        guide = SimpleNamespace(slug='icy-test', content_markdown='完整原稿')
        repairs = {'icy-test': {'content_sha256': hashlib.sha256(guide.content_markdown.encode()).hexdigest(),
            'blocks': {'md-2': [{'kind': 'priority', 'paragraph': 1, 'text': ['急速', '精通'], 'relation': [3]}]}}}
        def blocks():
            return [{'id': 'md-2', 'type': 'html', 'html': '<p>介绍。</p><p>急速</p><p>精通</p><p>解释。</p>'}]
        with patch('botend.services.class_guide_icy_format._legacy_repairs', return_value=repairs):
            result = blocks();restore_icy_format(result, guide)
            soup = BeautifulSoup(result[0]['html'], 'html.parser')
            self.assertEqual([s.text for s in soup.select('.guide-stat-priority strong')], ['急速', '精通'])
            self.assertEqual([p.text for p in soup.select('p')], ['介绍。', '解释。'])
            guide.content_markdown = '人工修改';result = blocks();restore_icy_format(result, guide)
            self.assertEqual(result, blocks())

    def test_existing_table_headers_are_semantic_and_data_rows_are_not_promoted(self):
        blocks = [{'id': 'table', 'type': 'html', 'html': '<table><tr><th>属性</th><th>阈值</th></tr><tr><td>急速</td><td>1320</td></tr></table><table><tr><td>普通数据</td></tr></table>'}]
        result = prepare_guide_presentation(blocks, SimpleNamespace(source_url='https://www.icy-veins.com/wow/test'))
        soup = BeautifulSoup(result[0]['html'], 'html.parser')
        self.assertEqual(len(soup.select('thead')), 1)
        self.assertEqual(soup.select_one('thead th')['scope'], 'col')
        self.assertEqual(soup.select_one('td').text, '急速')

    def test_legacy_paragraph_merge_retains_inline_markup(self):
        guide = SimpleNamespace(slug='icy-test', content_markdown='旧稿')
        repairs = {'icy-test': {'content_sha256': hashlib.sha256(guide.content_markdown.encode()).hexdigest(),
            'blocks': {'md-2': [{'kind': 'paragraph', 'paragraph': 0, 'text': ['选择', '急速', '和精通。']}]}}}
        blocks = [{'id': 'md-2', 'type': 'html', 'html': '<p>选择</p><p><strong>急速</strong></p><p>和精通。</p>'}]
        with patch('botend.services.class_guide_icy_format._legacy_repairs', return_value=repairs):
            restore_icy_format(blocks, guide)
        soup = BeautifulSoup(blocks[0]['html'], 'html.parser')
        self.assertEqual(len(soup.select('p')), 1)
        self.assertEqual(soup.get_text(), '选择急速和精通。')
        self.assertEqual(soup.strong.text, '急速')
