"""完整攻略排版归整必须保留内容、步骤、代码、引用与链接。"""
from copy import deepcopy
from types import SimpleNamespace

from bs4 import BeautifulSoup
from django.test import SimpleTestCase

from botend.services.class_guide_presentation import prepare_guide_presentation, guide_reading_structure


def heading(key, title, level=2):
    return {'id': key, 'type': 'heading', 'title': title, 'data': {'level': level}, 'children': []}


def text(markup):
    return {'id': 'text', 'type': 'html', 'html': markup, 'children': []}


class GuidePresentationTests(SimpleTestCase):
    def present(self, blocks, icy=True):
        guide = SimpleNamespace(source_url='https://www.icy-veins.com/wow/test' if icy else 'https://maxroll.gg/wow/test')
        return prepare_guide_presentation(deepcopy(blocks), guide)

    def test_only_number_immediately_before_heading_is_absorbed(self):
        blocks = [text('<p>造成 2.3 倍伤害。</p><p>2.3.1.</p>'), heading('section', '天赋', 3)]
        result = self.present(blocks)
        self.assertIn('造成 2.3 倍伤害。', result[0]['html'])
        self.assertNotIn('<p>2.3.1.</p>', result[0]['html'])
        self.assertEqual(result[1]['data']['source_number'], '2.3.1.')
        self.assertIn('<p>2.3.1.</p>', blocks[0]['html'])
        self.assertIn('<p>2.3.1.</p>', self.present(blocks, icy=False)[0]['html'])

    def test_real_ordered_steps_are_kept_and_empty_heading_marker_is_removed(self):
        markup = '<ol start="2"><li>施放技能</li><li>等待两秒</li></ol><ol start="4"><li></li></ol>'
        result = self.present([text(markup), heading('next', '下一节')])
        soup = BeautifulSoup(result[0]['html'], 'html.parser')
        self.assertEqual([li.get_text() for li in soup.select('li')], ['施放技能', '等待两秒'])
        self.assertEqual(soup.ol['start'], '2')
        self.assertEqual(result[1]['data']['source_number'], '4.')

    def test_inline_reference_continuation_preserves_text_and_tooltip(self):
        markup = '<p>为团队提供增益（</p><p><span class="guide-ref" data-reference-id="1459" data-tooltip-body="保留提示">奥术智慧</span></p><p>），并提高伤害。</p>'
        result = self.present([text(markup)])[0]['html']
        soup = BeautifulSoup(result, 'html.parser')
        self.assertEqual(len(soup.find_all('p')), 1)
        self.assertEqual(soup.get_text(), '为团队提供增益（奥术智慧），并提高伤害。')
        self.assertEqual(soup.select_one('.guide-ref')['data-tooltip-body'], '保留提示')

    def test_standalone_rotation_references_are_never_joined(self):
        markup = '<p>起手顺序：</p><p><span class="guide-ref">技能甲</span></p><p><span class="guide-ref">技能乙</span></p><p>进入常规循环。</p>'
        result = self.present([text(markup)])[0]['html']
        self.assertEqual(len(BeautifulSoup(result, 'html.parser').find_all('p')), 4)

    def test_condition_table_and_icons_preserve_semantics(self):
        markup = '<ol><li><strong>适用条件：启用天赋。</strong>施放技能。</li></ol><table><thead><tr><th>部位</th><th>装备</th></tr></thead><tbody><tr><th>头部</th><td><a href="https://example.com/item">装备甲</a></td></tr></tbody></table><p><img src="https://static.icy-veins.com/images/wow/large_icons/spell.jpg"/>说明图标</p>'
        soup = BeautifulSoup(self.present([text(markup)])[0]['html'], 'html.parser')
        self.assertEqual(soup.select_one('.guide-condition').text, '适用条件：启用天赋。')
        self.assertEqual(len(soup.select('tr')), 2)
        self.assertEqual(soup.select_one('.guide-table-scroll')['tabindex'], '0')
        self.assertIsNone(soup.a)
        self.assertEqual(soup.td.text, '装备甲')
        self.assertEqual(soup.img['width'], '24')

    def test_macro_talent_codes_and_nested_heading_ids_are_preserved(self):
        blocks = [heading('main', '天赋'), {'id': 'macro', 'type': 'code', 'data': {'code': '/cast [@focus] 技能'}, 'children': []},
                  {'id': 'talent', 'type': 'talents', 'data': {'code': 'ABC+/=='}, 'children': []},
                  {'id': 'details', 'type': 'details', 'children': [heading('nested', '历史记录', 3)]}]
        self.assertEqual(self.present(blocks), blocks)
        chapters, toc = guide_reading_structure(blocks)
        self.assertEqual(len(chapters), 1)
        self.assertEqual(toc[0]['children'][0]['id'], 'nested')

    def test_source_note_and_chapter_navigation_keep_all_content(self):
        blocks = [heading('source', '来源与翻译说明'), text('<p>原作者与完整版权说明</p>'),
                  heading('overview', '专精概览'), heading('sub', '玩法', 3), heading('deep', '细节', 4),
                  heading('gear', '装备选择'), text('<p>所有装备。</p>')]
        chapters, toc = guide_reading_structure(blocks)
        self.assertTrue(chapters[0]['is_source'])
        self.assertEqual([b for chapter in chapters for b in chapter['blocks']], blocks)
        self.assertEqual([node['id'] for node in toc], ['source', 'overview', 'gear'])
        self.assertEqual(toc[1]['children'][0]['children'][0]['id'], 'deep')

    def test_source_promotions_and_standalone_navigation_are_removed(self):
        markup = '<p>正文介绍。</p><p><img src="https://static.icy-veins.com/images/common/raiderio-logo.svg"></p><p>奥术大秘境资源</p><p><a href="https://raider.io/specs/arcane-mage/talents">顶级天赋配置</a></p><p><a href="https://www.icy-veins.com/wow/test">另一篇攻略</a></p>'
        result = self.present([text(markup)])[0]['html']
        self.assertEqual(BeautifulSoup(result, 'html.parser').get_text(), '正文介绍。')
        self.assertIn('顶级天赋配置', self.present([text(markup)], icy=False)[0]['html'])

    def test_inline_source_links_keep_prose_and_game_references(self):
        markup = '<p>来自<a href="https://www.icy-veins.com/wow/boss">首领甲</a>的装备。</p><table><tr><td><a href="https://www.icy-veins.com/wow/professions">裁缝</a></td></tr></table><p><a href="https://www.icy-veins.com/wow/item"><span class="guide-ref" data-reference-id="123">装备甲</span></a></p><p><a href="https://www.curseforge.com/wow/addons/test">插件下载</a></p>'
        soup = BeautifulSoup(self.present([text(markup)])[0]['html'], 'html.parser')
        self.assertEqual(soup.p.get_text(), '来自首领甲的装备。')
        self.assertEqual(soup.td.get_text(), '裁缝')
        table_paragraph = '<table><tr><td><p><a href="https://www.icy-veins.com/wow/boss">首领甲</a></p></td></tr></table>'
        table_soup = BeautifulSoup(self.present([text(table_paragraph)])[0]['html'], 'html.parser')
        self.assertEqual(table_soup.td.get_text(), '首领甲')
        self.assertEqual(soup.select_one('.guide-ref')['data-reference-id'], '123')
        self.assertEqual(len(soup.select('a')), 0)
        self.assertIn('插件下载', soup.get_text())

    def test_source_attribution_links_are_retained(self):
        markup = '<p><a href="https://www.icy-veins.com/wow/test">章节原文</a></p>'
        result = self.present([heading('source', '来源与翻译说明'), text(markup), heading('body', '专精概览'), text(markup)])
        self.assertIn('<a ', result[1]['html'])
        self.assertNotIn('<a ', result[3]['html'])
