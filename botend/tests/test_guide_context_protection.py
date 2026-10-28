"""Frozen Maxroll prose, adjacent entity cases, and real isolated cache replay."""
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from botend.guide_models import ClassGuideTranslation
from botend.services.class_guide_content import REF_RE
from botend.services.class_guide_glossary import GuideGlossary
from botend.services.class_guide_service import (
    build_guide_glossary, fingerprint, protect_guide_text, translate_blocks,
)
from botend.services.class_guide_translation import translate_fragment_nodes
from botend.services.wow_news_glossary_service import WowNewsGlossary


class OfflineService:
    def available(self):
        return False

    @property
    def engine(self):
        raise AssertionError('No model/provider access allowed')


class GuideContextProtectionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.rows = json.loads((Path(__file__).parent / 'fixtures/maxroll_context_protection.json').read_text())['nodes']

    def glossary(self):
        # Exercise the real default builder; these names come from frozen
        # protection evidence, not guessed spell IDs or live central edits.
        return GuideGlossary.prioritized(
            build_guide_glossary([], '12.1.5'),
            WowNewsGlossary.from_trusted_pairs([('Stagger', '醉拳'), ('Cast', '施放')]),
        )

    @staticmethod
    def node_key(version, protected):
        return hashlib.sha256(json.dumps(
            ['guide-node-v1', version, protected.text, protected.replacements],
            ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def test_real_nodes_preserve_refs_without_forcing_wrong_semantics(self):
        glossary = self.glossary()
        for row in self.rows:
            with self.subTest(source=row['source']):
                protected = glossary.protect(row['source'])
                restored = glossary.restore(protected.text, protected.replacements)
                self.assertEqual(REF_RE.findall(restored), REF_RE.findall(row['source']))
                self.assertNotIn('醉拳', restored)
                self.assertNotIn('范围伤害', restored)
                if 'Stagger' in row['source']:
                    self.assertEqual(restored, row['source'])
                else:
                    self.assertIn('范围 healing', restored)
                    self.assertIn('施放', restored)
                self.assertTrue(protected.is_intact(protected.text))
                self.assertTrue(glossary.needs_new_translation(row['source']))
                self.assertNotEqual(self.node_key(row['game_version'], protected), row['key'])

    def test_conservative_verb_context_and_real_skill_names(self):
        glossary = GuideGlossary.from_trusted_pairs([
            ('Stagger', '醉拳'), ('Heal', '治疗术'), ('Charge', '冲锋'),
            ('Power Word: Shield', '真言术：盾'), ('AoE', '范围'),
        ])
        ordinary = (
            'Stagger the dispel to avoid getting to 2 stacks.',
            'Stagger your dispels.', 'You should Stagger the dispels.',
            'Remember to Stagger your dispels.', 'You must not Stagger the wrong dispels.',
            'Heal the tank.', 'Charge your cooldowns carefully.',
            'stagger the dispels.',
        )
        for source in ordinary:
            with self.subTest(ordinary=source):
                self.assertEqual(glossary.protect(source).text, source)
                self.assertTrue(glossary.needs_new_translation(source))
        for source, expected in (
            ('Stagger', '醉拳'), ('Stagger reduces incoming damage.', '醉拳 reduces incoming damage.'),
            ('Purify your Stagger.', 'Purify your 醉拳.'), ('Use Heal on the tank.', 'Use 治疗术 on the tank.'),
            ('Cast Charge.', 'Cast 冲锋.'), ('Power Word: Shield', '真言术：盾'),
            ('Stagger, the defensive mechanic.', '醉拳, the defensive mechanic.'),
            ('Stagger the Unyielding', '醉拳 the Unyielding'),
            ('This makes Stagger the best defense.', 'This makes 醉拳 the best defense.'),
            ('Compare Stagger the mechanic with a cooldown.', 'Compare 醉拳 the mechanic with a cooldown.'),
        ):
            with self.subTest(skill=source):
                protected = glossary.protect(source)
                self.assertEqual(glossary.restore(protected.text, protected.replacements), expected)
                self.assertFalse(glossary.needs_new_translation(source))
        mixed = 'Stagger the dispels while managing Stagger.'
        protected = glossary.protect(mixed)
        self.assertEqual(glossary.restore(protected.text, protected.replacements),
                         'Stagger the dispels while managing 醉拳.')
        # This is a guide-only context rule, not a global change to news names.
        news = WowNewsGlossary.from_trusted_pairs([('Stagger', '醉拳')])
        self.assertIn('醉拳', news.restore(news.protect(ordinary[0]).text, news.protect(ordinary[0]).replacements))

    def test_generic_labels_and_change_prose_do_not_force_entity_names(self):
        glossary = GuideGlossary.from_trusted_pairs([
            ('General', '将军'), ('General Storm', '风暴将军'), ('Changed', '已更改'),
        ])
        for source in ('General', ' General ', 'Changed from the class tree to be '):
            with self.subTest(prose=source):
                self.assertEqual(glossary.protect(source).text, source)
                self.assertTrue(glossary.needs_new_translation(source))
        for source, expected in (
            ('General Storm', '风暴将军'), ('General attacks the tank.', '将军 attacks the tank.'),
            ('Changed', '已更改'), ('Use Changed now.', 'Use 已更改 now.'),
        ):
            with self.subTest(entity=source):
                protected = glossary.protect(source)
                self.assertEqual(glossary.restore(protected.text, protected.replacements), expected)
        html = '<ul><li>General<ul><li>Use [[spell:1294933]].</li></ul></li></ul>'
        protected = protect_guide_text(html, glossary)
        self.assertEqual(protected.text, html)
        # Real cache consumer: old same-source cache must not restore the wrong
        # entity interpretation; reviewed new-key cache remains reusable.
        for source, bad, good in (
            ('General', '将军', '通用'),
            ('Changed from the class tree to be ', '已更改 从职业树中改为 ', '从职业天赋树改为'),
        ):
            with self.subTest(cache=source):
                old_glossary = WowNewsGlossary.from_trusted_pairs([('General', '将军'), ('Changed', '已更改')])
                old = old_glossary.protect(source)
                old_row = ClassGuideTranslation.objects.create(key=self.node_key('12.1.5', old), source=source, translated=bad)
                self.assertIsNone(translate_fragment_nodes(source, '12.1.5', glossary, OfflineService(), cache_only=True))
                new = glossary.protect(source)
                ClassGuideTranslation.objects.create(key=self.node_key('12.1.5', new), source=source, translated=good)
                self.assertEqual(translate_fragment_nodes(source, '12.1.5', glossary, OfflineService(), cache_only=True), good)
                old_row.refresh_from_db()
                self.assertEqual(old_row.translated, bad)

    def test_aoe_default_is_effect_neutral_and_preserves_central_priority(self):
        glossary = self.glossary()
        for source in ('AoE healing', 'AoE damage', 'AoE control', 'aoe healing', 'AOE damage'):
            with self.subTest(source=source):
                protected = glossary.protect(source)
                self.assertEqual(glossary.restore(protected.text, protected.replacements),
                                 '范围' + source[3:])
        # Central facts are still authoritative; do not rewrite them in a subclass.
        with patch('botend.services.class_guide_service.effective_names', return_value=[
            {'kind': 'phrase', 'object_id': 0, 'name_en': 'AoE', 'name_zh': '中央审校译名'},
        ]):
            central = build_guide_glossary([], '12.1.5')
        protected = central.protect('AoE')
        self.assertEqual(central.restore(protected.text, protected.replacements), '中央审校译名')

    def test_old_node_and_historical_block_caches_do_not_reintroduce_bad_terms(self):
        glossary = self.glossary()
        bad_texts = (
            '2名玩家受到魔法持续伤害，到期触发 [[spell:1310599]]。醉拳驱散以避免2层。',
            '处于 [[spell:114052]] 时施放 [[spell:1064]] 进行范围伤害治疗。',
        )
        good_texts = (
            '2名玩家受到魔法持续伤害，到期触发 [[spell:1310599]]。错开驱散以避免叠至2层。',
            '处于 [[spell:114052]] 时施放 [[spell:1064]] 进行范围治疗。',
        )
        for row, bad_text, good_text in zip(self.rows, bad_texts, good_texts):
            with self.subTest(source=row['source']):
                ClassGuideTranslation.objects.all().delete()
                source, version = row['source'], row['game_version']
                old_node = ClassGuideTranslation.objects.create(key=row['key'], source=source, translated=bad_text)
                old_glossary = WowNewsGlossary.from_trusted_pairs([
                    ('Stagger', '醉拳'), ('AoE', '范围伤害'), ('Cast', '施放'),
                ])
                old = protect_guide_text(source, old_glossary)
                old_block_key = fingerprint(['guide-v1', version, old.text, old.replacements])
                old_block = ClassGuideTranslation.objects.create(key=old_block_key, source=source, translated=bad_text)
                self.assertIsNone(translate_fragment_nodes(source, version, glossary, OfflineService(), cache_only=True))
                blocks = [{'id': 'p', 'type': 'html', 'html': source, 'children': []}]
                with patch('botend.services.class_guide_service.build_guide_glossary', return_value=glossary):
                    out, failed = translate_blocks(blocks, version, service=OfflineService())
                    self.assertTrue(failed)
                    self.assertEqual(out, blocks)
                    # Explicit local cache fixture, not a claimed model response.
                    new = glossary.protect(source)
                    ClassGuideTranslation.objects.create(key=self.node_key(version, new), source=source, translated=good_text)
                    self.assertEqual(translate_fragment_nodes(source, version, glossary, OfflineService(), cache_only=True), good_text)
                    out, failed = translate_blocks(blocks, version, service=OfflineService())
                    self.assertFalse(failed)
                    self.assertEqual(out[0]['html'], good_text)
                    again, failed = translate_blocks(blocks, version, service=OfflineService())
                    self.assertFalse(failed)
                    self.assertEqual(again, out)
                for cached in (old_node, old_block):
                    cached.refresh_from_db()
                    self.assertEqual(cached.translated, bad_text)
