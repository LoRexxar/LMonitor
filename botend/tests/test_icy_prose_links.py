from copy import deepcopy
from types import SimpleNamespace
from bs4 import BeautifulSoup
from django.test import SimpleTestCase
from botend.services.class_guide_presentation import prepare_guide_presentation


class IcyProseLinkTests(SimpleTestCase):
    def present(self, markup):
        guide = SimpleNamespace(source_url='https://www.icy-veins.com/wow/test')
        return BeautifulSoup(prepare_guide_presentation([
            {'id': 'prose', 'type': 'html', 'html': markup, 'children': []}], guide)[0]['html'], 'html.parser')

    def test_split_sentence_preserves_location_link_text_without_external_navigation(self):
        markup = ('<p>你需要获取来自</p>'
                  '<p><a href="https://www.icy-veins.com/wow/ulatek-raid-guide">乌拉特克</a></p>'
                  '<p>和</p><p><a href="https://www.icy-veins.com/wow/coiled-altar-raid-guide">盘绕祭坛</a></p>'
                  '<p>的强化物品等级装备。</p>')
        soup = self.present(markup)
        self.assertEqual(soup.get_text('', strip=True), '你需要获取来自乌拉特克和盘绕祭坛的强化物品等级装备。')
        self.assertFalse(soup.select('a'))

    def test_promotional_group_does_not_consume_later_standalone_prose_link(self):
        markup = ('<p><img src="https://static.icy-veins.com/images/common/raiderio-logo.svg"></p>'
                  '<p>大秘境资源</p><p><a href="https://raider.io/test">顶级天赋配置</a></p>'
                  '<h4>装备来源</h4><p>该物品来自</p>'
                  '<p><a href="https://www.icy-veins.com/wow/ulatek-raid-guide">乌拉特克</a></p>'
                  '<p>。</p>')
        soup = self.present(markup)
        self.assertNotIn('顶级天赋配置', soup.get_text())
        self.assertIn('乌拉特克', soup.get_text())

    def test_identical_adjacent_conditions_collapse_without_combining_different_scopes(self):
        markup = ('<p><strong>适用条件：天赋甲（启用）。</strong></p>'
                  '<p><strong>适用条件：天赋甲（启用）。</strong></p>'
                  '<p><span class="guide-ref">技能甲</span></p>'
                  '<p><strong>适用条件：天赋甲（未启用）。</strong></p>'
                  '<p><span class="guide-ref">技能乙</span></p>')
        soup=self.present(markup)
        self.assertEqual([s.get_text() for s in soup.select('strong')], ['适用条件：天赋甲（启用）。', '适用条件：天赋甲（未启用）。'])
        self.assertEqual([s.get_text() for s in soup.select('.guide-ref')], ['技能甲', '技能乙'])

    def test_parenthetical_reference_continuation_is_inline_but_rotation_is_not(self):
        soup = self.present('<p>唯一可获得的胸甲是</p><p><span class="guide-ref" data-reference-id="1">装备甲</span></p><p>（来自首领）。</p>')
        self.assertEqual(len(soup.find_all('p')), 1)
        self.assertEqual(soup.p.get_text(), '唯一可获得的胸甲是装备甲（来自首领）。')
        rotation = self.present('<p>起手顺序：</p><p><span class="guide-ref">技能甲</span></p><p><span class="guide-ref">技能乙</span></p>')
        self.assertEqual(len(rotation.find_all('p')), 3)
