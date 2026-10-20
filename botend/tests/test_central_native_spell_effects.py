"""PTR 12.1.5.70077 sampled DB2 effects through real central ORM compilation."""
import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import TestCase

from botend.models import WowSpellSnapshot, WowTalentNodeMetadata, WowTalentVersion
from botend.services.wow_spell_display import SpellDisplayReader, sync_spell_display


class CentralNativeSpellEffectTests(TestCase):
    def setUp(self):
        self.sample = json.loads((Path(__file__).parent / 'fixtures' /
                                  'ptr_spell_effect_sample.json').read_text())
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.source = Path(directory.name)
        self.version, _ = WowTalentVersion.objects.update_or_create(key='ptr', defaults={
            'branch': 'ptr', 'is_active': True, 'status': 'active',
            'current_build': self.sample['build'], 'source_dir': str(self.source)})
        for sid, raw in self.sample['spells'].items():
            WowTalentNodeMetadata.objects.create(
                talent_version=self.version, class_name='Shaman', spec_name='Restoration',
                tree_type='spec', node_id=int(sid), spell_id=int(sid),
                name_zh='机制测试', description_zh=raw['Description_lang'], source='db2_backfill')

    def write_effects(self, filename, rows):
        with (self.source / filename).open('w', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def assert_compiled_mechanics(self):
        report = sync_spell_display(branch='ptr', apply=True, spell_ids=[633, 1064])
        self.assertEqual(report['scanned'], 2)
        reader = SpellDisplayReader('ptr')
        lay = reader.get(633)['description_zh']
        chain = reader.get(1064)['description_zh']
        self.assertIn('最大生命值的100%', lay)
        self.assertIn('20码范围内', chain)
        self.assertIn('每跳一次就会降低30%', chain)
        self.assertNotIn('$', lay + chain)
        self.assertEqual(WowSpellSnapshot.objects.filter(locale='zhCN').count(), 2)
        for sid, raw in self.sample['spells'].items():
            self.assertEqual(WowTalentNodeMetadata.objects.get(spell_id=int(sid)).description_zh,
                             raw['Description_lang'])

    def test_native_only_preserves_sampled_chinese_mechanics(self):
        self.write_effects('SpellEffect.csv', self.sample['effects'])
        self.assert_compiled_mechanics()

    def test_legacy_index_remains_supported(self):
        rows = [{k: v for k, v in row.items() if k != 'DifficultyID'}
                for row in self.sample['effects']]
        self.write_effects('spell_effect_index.csv', rows)
        self.assert_compiled_mechanics()

    def test_native_wins_over_stale_index_and_ignores_difficulty_rows(self):
        stale = [dict(row, EffectBasePointsF='777') for row in self.sample['effects']]
        difficult = [dict(row, DifficultyID='16', EffectBasePointsF='999')
                     for row in self.sample['effects']]
        self.write_effects('spell_effect_index.csv', stale)
        # Later duplicate keys must not overwrite the default-difficulty facts.
        self.write_effects('SpellEffect.csv', self.sample['effects'] + difficult)
        self.assert_compiled_mechanics()
