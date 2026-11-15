import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

from bs4 import BeautifulSoup
from django.test import SimpleTestCase
from botend.services.class_guide_content import REF_RE, walk_blocks
from botend.services.class_guide_markdown import compile_markdown
from botend.services.class_guide_icy_format import restore_icy_format
from botend.services.class_guide_presentation import prepare_guide_presentation


class FrozenIcyLayoutTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bundle = Path(__file__).resolve().parents[1] / 'data/class_guides/icy-full-initial-20261022.zip'

    def project(self, slug, edit=''):
        with ZipFile(self.bundle) as archive:
            guide = json.loads(archive.read(slug + '-12.1.5.json'))
        guide['content_markdown'] += edit
        obj = SimpleNamespace(**guide)
        blocks = compile_markdown(guide['content_markdown'])
        for block in walk_blocks(blocks):
            if block.get('html'):
                block['html'] = REF_RE.sub(lambda m: '<span class="guide-ref" data-reference-kind="{}" data-reference-id="{}" data-tooltip-body="preserve">[[{}:{}]]</span>'.format(m[1], m[2], m[1], m[2]), block['html'])
        restore_icy_format(blocks, obj)
        before = {b['id']: b.get('html','') for b in walk_blocks(blocks)}
        result = prepare_guide_presentation(blocks, obj)
        return {b['id']: BeautifulSoup(b.get('html',''), 'html.parser') for b in walk_blocks(result)}, before

    def test_frozen_holy_sentence_and_balance_places_are_complete_paragraphs(self):
        result, _ = self.project('icy-holy-paladin-pve-healing-guide')
        soup = result['md-240']
        self.assertTrue(any('[[item:268222]]和[[item:271878]]由于' in p.get_text() for p in soup.find_all('p')))
        self.assertEqual(soup.select_one('[data-reference-id="268222"]')['data-tooltip-body'], 'preserve')
        result, _ = self.project('icy-balance-druid-pve-dps-guide')
        self.assertTrue(any('来自乌拉特克和盘绕祭坛的强化' in p.get_text() for p in result['md-248'].find_all('p')))

    def test_frozen_fire_sentence_is_not_split_around_spells(self):
        result, _ = self.project('icy-fire-mage-pve-dps-guide')
        self.assertTrue(any(p.get_text()=='在[[spell:190319]]结束时，你将获得[[spell:383860]].' for p in result['md-205'].find_all('p')))

    def test_timed_brewmaster_actions_keep_conditions_times_and_reference_order(self):
        result, before = self.project('icy-brewmaster-monk-pve-tank-guide')
        soup = result['md-229'];sequence=soup.select_one('ol.guide-opener-sequence')
        self.assertIsNotNone(sequence)
        first=sequence.find('li',recursive=False)
        self.assertIn('Charred Passions（未启用）',first.get_text())
        self.assertIn('[[spell:115181]]',first.get_text())
        self.assertIn('3s',first.get_text())
        self.assertEqual([x['data-reference-id'] for x in BeautifulSoup(before['md-229'],'html.parser').select('.guide-ref')], [x['data-reference-id'] for x in soup.select('.guide-ref')])
        self.assertGreater(len(soup.select('ol:not(.guide-opener-sequence) li')), 0)

    def test_mutually_exclusive_survival_openers_have_independent_scopes_and_numbering(self):
        result, before = self.project('icy-survival-hunter-pve-dps-guide')
        soup=result['md-213']
        branches=soup.select('.guide-opener-branch')
        self.assertEqual(len(branches),4)
        expected=['猎群领袖（启用）；Twin Fangs（未启用）','猎群领袖（启用）；Twin Fangs（启用）','哨兵（启用）；Twin Fangs（未启用）','哨兵（启用）；Twin Fangs（启用）']
        for branch, scope in zip(branches,expected):
            self.assertIn(scope,branch.select_one(':scope>p').get_text())
            self.assertEqual(sum(p.get_text()=='拉怪' for p in branch.find_all('p')),1)
            self.assertEqual(len(branch.select(':scope>ol')),1)
            self.assertNotIn('start',branch.ol.attrs)
        original=BeautifulSoup(before['md-213'],'html.parser')
        self.assertEqual([dict(n.attrs) for n in original.select('.guide-ref')],[dict(n.attrs) for n in soup.select('.guide-ref')])
        self.assertEqual([s.get_text() for s in original.select('strong')],[s.get_text() for s in soup.select('strong')])

    def test_manual_edit_does_not_receive_frozen_editorial_joins(self):
        result, _ = self.project('icy-fire-mage-pve-dps-guide', '\n人工校订。\n')
        self.assertTrue(any(p.get_text()=='在' for p in result['md-205'].find_all('p')))
