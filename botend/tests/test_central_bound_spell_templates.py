"""Central spell display compilation: native effects and bound source contracts."""
import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from django.test import TestCase
from botend.models import WowSpellSnapshot, WowTalentNodeMetadata, WowTalentVersion
from botend.services.wow_spell_display import SpellDisplayReader, sync_spell_display
import tarfile
from unittest.mock import patch


class CentralSpellCSVFixtureMixin:
    def write_csv(self, name, rows):
        with (self.source / name).open('w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


class CentralNativeSpellEffectTests(CentralSpellCSVFixtureMixin, TestCase):
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
        self.write_csv('SpellEffect.csv', self.sample['effects'])
        self.assert_compiled_mechanics()

    def test_legacy_index_remains_supported(self):
        rows = [{k: v for k, v in row.items() if k != 'DifficultyID'}
                for row in self.sample['effects']]
        self.write_csv('spell_effect_index.csv', rows)
        self.assert_compiled_mechanics()

    def test_native_wins_over_stale_index_and_ignores_difficulty_rows(self):
        stale = [dict(row, EffectBasePointsF='777') for row in self.sample['effects']]
        difficult = [dict(row, DifficultyID='16', EffectBasePointsF='999')
                     for row in self.sample['effects']]
        self.write_csv('spell_effect_index.csv', stale)
        # Later duplicate keys must not overwrite the default-difficulty facts.
        self.write_csv('SpellEffect.csv', self.sample['effects'] + difficult)
        self.assert_compiled_mechanics()


class CentralBoundSpellTemplateTests(CentralSpellCSVFixtureMixin, TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.source = Path(directory.name)
        self.version, _ = WowTalentVersion.objects.update_or_create(key='ptr', defaults={
            'branch': 'ptr', 'is_active': True, 'status': 'active',
            'current_build': '12.1.5.70077', 'source_dir': str(self.source)})
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('offline regression'))
        network.start()
        self.addCleanup(network.stop)


    def node(self, sid, entry, **extra):
        values = dict(talent_version=self.version, class_name='Shaman', spec_name='Restoration',
                      tree_type='spec', node_id=entry, spell_id=sid, display_spell_id=sid,
                      name_zh='机制测试', source='db2_backfill')
        values.update(extra)
        return WowTalentNodeMetadata.objects.create(**values)

    def display(self, node):
        return SpellDisplayReader(self.version.branch).get(node.display_spell_id,
            class_name=node.class_name, spec_name=node.spec_name, node_id=node.node_id)

    def install_retail_archive(self):
        # This is the tracked real retail source, not a fabricated PTR English fixture.
        self.version.key = self.version.branch = 'retail'
        self.version.current_build = '12.1.0.69283'
        WowTalentVersion.objects.filter(key='retail').exclude(pk=self.version.pk).delete()
        self.version.save()
        asset = Path(__file__).resolve().parents[1] / 'data/retail_talent_db2_12.1.0.69283.tar.gz'
        with tarfile.open(asset) as archive:
            for name in archive.getnames():
                self.assertEqual(Path(name).name, name)
                (self.source / name).write_bytes(archive.extractfile(name).read())

    def test_ptr_sample_recovers_numbers_with_empty_raw_snapshots_and_damaged_nodes(self):
        sample = json.loads((Path(__file__).parent / 'fixtures/ptr_spell_effect_sample.json').read_text())
        self.write_csv('Spell_zhCN.csv', list(sample['spells'].values()))
        self.write_csv('SpellEffect.csv', sample['effects'])
        damaged = {
            633: '治疗一个友方目标，数值相当于你的最大生命值的%。',
            1064: '为友方目标恢复生命值，然后跳跃到最多码范围内受伤最重的名盟友进行治疗。治疗效果每跳一次就会会有所降低。',
        }
        nodes = [self.node(sid, entry, description=text, description_zh=text)
                 for (sid, text), entry in zip(damaged.items(), (102583, 127861))]
        for sid in damaged:
            WowSpellSnapshot.objects.create(branch='wowxptr', locale='zhCN', spell_id=sid)
            WowSpellSnapshot.objects.create(branch='wowt', locale='enUS', spell_id=sid)
        before = list(WowSpellSnapshot.objects.order_by('pk').values())
        report = sync_spell_display(branch='ptr', apply=True, spell_ids=damaged)
        self.assertEqual((report['created'], report['updated']), (0, 2))
        for node, fragments in zip(nodes, (('最大生命值的100%',), ('20码范围内', '每跳一次就会降低30%'))):
            text = self.display(node)['description_zh']
            for fragment in fragments:
                self.assertIn(fragment, text)
            # A context-free default has no aura selection. The sampled PTR fixture
            # deliberately lacks the 236502 dependency of Chain Heal's active-aura arm.
            self.assertIn(fragments[-1], SpellDisplayReader('ptr').get(node.spell_id)['description_zh'])
            node.refresh_from_db()
            self.assertEqual(node.description, damaged[node.spell_id])
            self.assertEqual(node.description_zh, damaged[node.spell_id])
        after = list(WowSpellSnapshot.objects.order_by('pk').values())
        for old, new in zip(before, after):
            old.pop('display_data')
            new.pop('display_data')
            self.assertEqual(old, new)
        second = sync_spell_display(branch='ptr', apply=True, spell_ids=damaged)
        self.assertEqual((second['created'], second['updated'], second['unchanged']), (0, 0, 2))

    def test_bound_bilingual_spell_replaces_damaged_context_even_with_good_default(self):
        self.install_retail_archive()
        node = self.node(633, 102583, class_name='Paladin', spec_name='Holy',
                         description='Heals for % maximum health.', description_zh='治疗最大生命值的%。')
        with (self.source / 'Spell_zhCN.csv').open(newline='') as handle:
            raw = next(row['Description_lang'] for row in csv.DictReader(handle) if row['ID'] == '633')
        snapshot = WowSpellSnapshot.objects.create(branch='wow', locale='zhCN', spell_id=633, description=raw)
        sync_spell_display(branch='retail', apply=True, spell_ids=[633])
        display = self.display(node)
        self.assertIn('最大生命值的100%', display['description_zh'])
        self.assertIn('100% your maximum health', display['description'])
        self.assertNotIn('提高目标', display['description_zh'])  # inactive aura is not tree membership
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.description, raw)
        node.refresh_from_db()
        self.assertEqual(node.description_zh, '治疗最大生命值的%。')
        self.assertEqual(sync_spell_display(branch='retail', apply=True, spell_ids=[633])['updated'], 0)

    def test_real_trait_override_and_manual_node_survive_spell_source_priority(self):
        self.install_retail_archive()
        with (self.source / 'TraitNodeEntry.csv').open(newline='') as handle:
            entry = next(int(row['ID']) for row in csv.DictReader(handle) if row['TraitDefinitionID'] == '108281')
        node = self.node(102401, entry, class_name='Druid', spec_name='Balance',
                         description='Old damaged override', description_zh='旧损坏说明')
        self.node(24858, 999001, class_name='Druid', spec_name='Balance')
        manual = self.node(102401, 999002, class_name='Druid', spec_name='Balance', source='manual',
                           description='Reviewed custom mechanics.', description_zh='人工校订机制。')
        sync_spell_display(branch='retail', apply=True, spell_ids=[102401])
        display = self.display(node)
        for fragment in ('枭兽形态', '旅行形态', '20码', '150%，持续5'):
            self.assertIn(fragment, display['description_zh'])
        self.assertIn('Moonkin Form', display['description'])
        self.assertEqual(self.display(manual)['description_zh'], '人工校订机制。')
        self.assertEqual(self.display(manual)['description'], 'Reviewed custom mechanics.')
        self.assertEqual(sync_spell_display(branch='retail', apply=True, spell_ids=[102401])['updated'], 0)

    def test_bound_template_keeps_specialization_conditionals(self):
        self.install_retail_archive()
        with (self.source / 'TraitDefinition_zhCN.csv').open(newline='') as handle:
            definition = next(row['ID'] for row in csv.DictReader(handle) if row['SpellID'] == '1022')
        with (self.source / 'TraitNodeEntry.csv').open(newline='') as handle:
            entry = next(int(row['ID']) for row in csv.DictReader(handle) if row['TraitDefinitionID'] == definition)
        holy = self.node(1022, entry, class_name='Paladin', spec_name='Holy', description_zh='旧渲染说明')
        protection = self.node(1022, entry, class_name='Paladin', spec_name='Protection', description_zh='旧渲染说明')
        sync_spell_display(branch='retail', apply=True, spell_ids=[1022])
        self.assertNotIn('与破咒祝福共享冷却时间', self.display(holy)['description_zh'])
        self.assertIn('与破咒祝福共享冷却时间', self.display(protection)['description_zh'])
        self.assertEqual(sync_spell_display(branch='retail', apply=True, spell_ids=[1022])['updated'], 0)

    def test_no_bound_source_keeps_node_without_cross_branch_or_global_dump(self):
        self.version.source_dir = ''
        self.version.save(update_fields=['source_dir'])
        node = self.node(633, 102583, description_zh='保留本分支历史说明')
        WowSpellSnapshot.objects.create(branch='wow', locale='zhCN', spell_id=633, description='其他分支说明')
        with patch('botend.wow.spell_text._dump_file', side_effect=AssertionError('no global source')):
            sync_spell_display(branch='ptr', apply=True, spell_ids=[633])
        self.assertEqual(self.display(node)['description_zh'], '保留本分支历史说明')
