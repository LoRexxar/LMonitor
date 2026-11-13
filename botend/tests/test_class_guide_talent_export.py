"""Real Maxroll Export regression: planner embeds are not game loadouts."""
import json

from django.test import SimpleTestCase, TestCase
from django.template.loader import render_to_string
from bs4 import BeautifulSoup

from botend.services.class_guide_markdown import compile_markdown, blocks_to_markdown
from botend.services.class_guide_maxroll import convert

# Captured from the real Maxroll Arcane Mage M+ page and its Export button.
# Evidence: maxroll-official-ui-export-verified.json; no local codec oracle.
SOURCE = 'C6DAmxqme2NN7ABQRxXf6C8R0NzwYZmZmFMzQzMmBAAwAAmZmmlllZmZmZZZmJ2AAYBmZG2sMjZWmxYmZmZYhZMjZAAGAAgZmFMzAzMAYYmZ'
EXPORTED = 'C4DAAAAAAAAAAAAAAAAAAAAAAMzwYZmZmFMzQzMmBAAwAAmZmmlllZmZmZZZmJ2AAYBmZG2sMjZWmxYmZmZYhZMjZAAGAAgZmFMzAzMAYYmZA'


class MaxrollArticleTalentExportTests(SimpleTestCase):
    def test_existing_markdown_and_new_embeds_use_official_game_export(self):
        body = ':::talents 团本\n```json\n' + json.dumps({'source_code': SOURCE, 'code': SOURCE}) + '\n```\n原说明。\n:::'
        data = compile_markdown(body)[0]['data']
        self.assertEqual(data['code'], EXPORTED)
        self.assertEqual(data['source_code'], SOURCE)
        self.assertTrue(data['converted'])
        self.assertEqual(compile_markdown(blocks_to_markdown(compile_markdown(body)))[0]['data']['code'], EXPORTED)
        blocks, audit = convert({'gutenbergBlock': [{'blockName': 'core/embed', 'attributes': {'url': 'https://maxroll.gg/wow/embed-tools/talents=' + SOURCE}, 'innerBlocks': [], 'innerHTML': ''}]})
        self.assertEqual(blocks[0]['data']['code'], EXPORTED)
        self.assertEqual(blocks[0]['data']['source_code'], SOURCE)
        native = ':::talents\n```json\n' + json.dumps({'code': EXPORTED}) + '\n```\n:::'
        self.assertEqual(compile_markdown(native)[0]['data']['code'], EXPORTED)


class ArticleTalentCopyBoundaryTests(TestCase):
    def test_render_copies_native_and_rejects_unknown_tree_and_wrong_spec(self):
        from botend.guide_models import ClassGuide
        from botend.services.class_guide_render import render_blocks
        guide = ClassGuide(title='奥术', class_name='Mage', spec_name='Arcane', spec_id=62,
                           game_version='12.1.5', source_url='https://maxroll.gg/wow/class-guides/arcane-mage-mythic-plus-guide')
        unknown = SOURCE[:4] + 'A' + SOURCE[5:]
        for code, expected_spec, success in [(SOURCE, 62, True), (EXPORTED, 62, True),
                                             (unknown, 62, False), (SOURCE, 63, False),
                                             ('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 62, False)]:
            with self.subTest(code=code[:12], spec=expected_spec):
                guide.spec_id = expected_spec
                original = {'id': 'talent', 'type': 'talents', 'title': '原方案',
                            'data': {'code': code, 'converted': True}, 'children': []}
                blocks = render_blocks([original], {}, guide)
                html = render_to_string('shared/class_guide_blocks.html', {'blocks': blocks})
                soup = BeautifulSoup(html, 'html.parser')
                self.assertEqual(original['data']['code'], code)
                self.assertEqual(bool(soup.select('button[data-copy]')), success)
                self.assertEqual(bool(soup.select('a[href*="/portal/talents/"]')), success)
                if success:
                    self.assertEqual(soup.select_one('.build-code').get_text(), EXPORTED)
                    self.assertIn('code=C4DA', soup.select_one('a')['href'])
                else:
                    self.assertFalse(soup.select('.build-code'))
                    self.assertIn('暂无法转换', soup.get_text())
                    self.assertNotIn(code, html)
