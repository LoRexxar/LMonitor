from django.test import TestCase

from botend.models import WowItemSnapshot, WowSpellSnapshot, WowTalentVersion
from botend.services.class_guide_content import resolve_references, render_references


class GuideCentralReferenceNamesTests(TestCase):
    def setUp(self):
        self.version, _ = WowTalentVersion.objects.update_or_create(
            key='retail', defaults={'branch': 'retail', 'is_active': True, 'major_version': '12.1'})

    def test_exact_central_names_do_not_require_duplicate_localization_rows(self):
        WowItemSnapshot.objects.create(item_id=281239, name="Corroded Hulk's Skullcap",
                                       name_zh='侵蚀巨怪罩帽', icon='inv_helmet_01')
        WowSpellSnapshot.objects.create(branch='wow', locale='zhCN', spell_id=1282078,
                                       name='Plague Froth', name_zh='瘟疫泡沫')
        # Same-number records in other namespaces must not certify a talent.
        text = '[[item:281239]] [[spell:1282078]] [[talent:1282078]]'
        refs = resolve_references([{'html': text}], '12.0.7')
        self.assertEqual(refs['[[item:281239]]']['name'], '侵蚀巨怪罩帽')
        self.assertEqual(refs['[[spell:1282078]]']['name'], '瘟疫泡沫')
        self.assertTrue(refs['[[item:281239]]']['resolved'])
        self.assertTrue(refs['[[spell:1282078]]']['resolved'])
        self.assertFalse(refs['[[talent:1282078]]']['resolved'])
        rendered = render_references('[[item:281239]] [[spell:1282078]]', refs)
        self.assertNotIn('is-unresolved', rendered)

    def test_missing_entry_does_not_borrow_another_referenced_entry_by_name(self):
        from botend.models import WowTalentNodeMetadata
        WowTalentNodeMetadata.all_objects.create(talent_version=self.version,
            node_id=50101, name='Same Name', name_zh='同名天赋')
        refs = resolve_references([{'html': '[[talent:50101]] [[talent:50102]]'}],
            '12.1', source_refs={'[[talent:50102]]': {'source_name': 'Same Name'}})
        self.assertFalse(refs['[[talent:50102]]']['resolved'])

    def test_central_names_are_fallback_after_usable_manual_and_native_names(self):
        from botend.models import WowTalentNodeMetadata
        for sid, mode in ((901001, 'manual'), (901002, 'native'), (901003, 'invalid')):
            with self.subTest(mode=mode):
                WowSpellSnapshot.objects.create(branch='wow', locale='zhCN', spell_id=sid,
                    name='Central Name', name_zh='中央名称')
                if mode == 'native':
                    WowTalentNodeMetadata.all_objects.create(talent_version=self.version,
                        node_id=sid + 100, spell_id=sid, display_spell_id=sid,
                        name='Native Name', name_zh='已核定原生名称')
                else:
                    WowTalentNodeMetadata.all_objects.create(talent_version=self.version,
                        localization_only=True, name_kind='spell', reference_id=sid,
                        name='Manual Name', name_zh='已核定人工名称' if mode == 'manual' else 'English Only')
                token = '[[spell:%s]]' % sid
                ref = resolve_references([{'html': token}], '12.1',
                    source_refs={token: {'source_name': 'Central Name'}})[token]
                self.assertTrue(ref['resolved'])
                self.assertEqual(ref['name'], {'manual': '已核定人工名称',
                    'native': '已核定原生名称', 'invalid': '中央名称'}[mode])

    def test_locale_identity_and_branch_priority_do_not_take_foreign_chinese(self):
        WowSpellSnapshot.objects.create(branch='wow', locale='enUS', spell_id=1282078,
                                       name='Plague Froth', name_zh='错误的英文行中文')
        WowSpellSnapshot.objects.create(branch='wow', locale='zhCN', spell_id=1282078,
                                       name='瘟疫泡沫', name_zh='')
        WowSpellSnapshot.objects.create(branch='wowt', locale='zhCN', spell_id=1282078,
                                       name='PTR Name', name_zh='测试服不同名')
        WowSpellSnapshot.objects.create(branch='wow', locale='enUS', spell_id=999010,
                                       name='Placeholder', name_zh='未核定中文')
        refs = resolve_references([{'html': '[[spell:1282078]] [[spell:999010]]'}], '12.1')
        self.assertEqual(refs['[[spell:1282078]]']['name'], '瘟疫泡沫')
        self.assertEqual(refs['[[spell:1282078]]']['name_en'], 'Plague Froth')
        self.assertFalse(refs['[[spell:999010]]']['resolved'])


class HeroAnchorImportTests(TestCase):
    def test_db2_import_and_repair_never_use_same_number_spell_for_selector(self):
        import csv
        import tempfile
        from pathlib import Path
        from io import StringIO
        from django.core.management import call_command
        from botend.models import WowTalentNodeMetadata
        from botend.services.wow_localization import names_for
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fixtures = {
                'TraitNode.csv': (["ID", "TraitTreeID", "TraitSubTreeID", "Type", "PosX", "PosY", "Flags"], [[99800, 658, 0, 3, 9300, 600, 9]]),
                'TraitNodeXTraitNodeEntry.csv': (["ID", "TraitNodeEntryID", "TraitNodeID"], [[1, 123340, 99800]]),
                'TraitNodeEntry.csv': (["ID", "TraitDefinitionID", "MaxRanks", "NodeEntryType", "TraitSubTreeID"], [[123340, 0, 1, 2, 39]]),
                'TraitDefinition.csv': (["ID", "SpellID"], []),
                'TraitEdge.csv': (["LeftTraitNodeID", "RightTraitNodeID"], []),
                'TraitSubTree.csv': (["ID", "Name_lang", "Description_lang", "TraitTreeID"], [[39, "日怒", "日怒法师投身于奥术烈焰。", 658]]),
                'SpellName_enUS.csv': (["ID", "Name_lang"], [[123340, "Player Farm - Summon Smothered Juicycrunch Carrot"]]),
                'SpellName_zhCN.csv': (["ID", "Name_lang"], [[123340, "Player Farm - Summon Smothered Juicycrunch Carrot"]]),
                'Spell_zhCN.csv': (["ID", "Description_lang"], [[123340, "错误的同号技能描述"]]),
            }
            for filename, (headers, data) in fixtures.items():
                with (root / filename).open('w', newline='') as f:
                    writer = csv.writer(f); writer.writerow(headers); writer.writerows(data)
            version, _ = WowTalentVersion.objects.update_or_create(key='retail', defaults={
                'branch': 'retail', 'major_version': '12.1', 'is_active': True,
                'status': 'staging', 'source_dir': folder})
            call_command('backfill_db2_talent_nodes', dump_dir=folder, version_key=version.key,
                         class_name='Mage', allow_inactive=True, stdout=StringIO())
            call_command('repair_talent_metadata_from_db2', dump_dir=folder, version_key=version.key,
                         skip_snapshot=True, stdout=StringIO())
            nodes = list(WowTalentNodeMetadata.all_objects.filter(talent_version=version, node_id=123340))
            self.assertTrue(nodes)
            for node in nodes:
                self.assertEqual(node.name, 'Sunfury')
                self.assertEqual(node.name_zh, '日怒')
                self.assertNotIn('错误的同号', node.description_zh)
                self.assertIsNone(node.display_spell_id)
                self.assertEqual(node.spell_id, 123340)  # legacy storage identity stays unchanged
            refs = resolve_references([{'html': '[[talent:123340]]'}], '12.1')
            self.assertTrue(refs['[[talent:123340]]']['resolved'])
            self.assertEqual(refs['[[talent:123340]]']['name'], '日怒')
            self.assertIn('英雄天赋专精', refs['[[talent:123340]]']['tooltip_text'])
