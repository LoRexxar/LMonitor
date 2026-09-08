"""共享名称写入、原表复用、结构隔离与版本回归。"""
from django.test import TestCase
from django.contrib.auth import get_user_model
from botend.models import WowTalentVersion, WowTalentNodeMetadata, WowSpellSnapshot, WowItemSnapshot
from botend.services.wow_localization import write_name, effective_names, export_names
from botend.services.class_guide_content import resolve_references
from botend.services.class_guide_service import build_guide_glossary
from botend.services.wow_news_glossary_service import WowNewsGlossary


class SharedNameTests(TestCase):
    def setUp(self):
        self.version = WowTalentVersion.objects.create(key='retail-12.1', major_version='12.1', branch='retail',
            is_active=True, is_default_player_tree=True)

    def term(self, kind='talent', identity=900, **kwargs):
        return dict(game_version='12.1', kind=kind, object_id=identity, name_en='Arcane Blast',
            name_zh=kwargs.get('name_zh', '奥术冲击'), icon='spell_arcane_blast', evidence='官方名称')

    def test_export_does_not_treat_legacy_english_placeholder_as_chinese(self):
        write_name(self.term())
        WowTalentNodeMetadata.all_objects.create(talent_version=self.version, localization_only=True,
            name_kind='spell', reference_id=123, name='Placeholder', name_zh='Placeholder')
        rows = export_names({'12.1'})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['name_zh'], '奥术冲击')

    def test_import_reuses_native_talent_and_preserves_shared_chinese(self):
        node = WowTalentNodeMetadata.objects.create(talent_version=self.version, class_name='mage', spec_name='arcane',
            node_id=7, talent_id=8, spell_id=30451, name='Arcane Blast', name_zh='共享中文')
        record, created = write_name(self.term())
        self.assertFalse(created)
        self.assertEqual(record['pk'], node.pk)
        self.assertEqual(record['name_zh'], '共享中文')
        self.assertEqual(WowTalentNodeMetadata.all_objects.count(), 1)
        node.refresh_from_db()
        self.assertEqual(node.reference_aliases, [900])
        blocks = [{'id':'p', 'type':'html', 'html':'[[talent:900]]'}]
        self.assertEqual(resolve_references(blocks, '12.1', 'mage', 'arcane')['[[talent:900]]']['name'], '共享中文')
        node.name_zh = '统一更正'; node.save(update_fields=['name_zh'])
        self.assertEqual(resolve_references(blocks, '12.1', 'mage', 'arcane')['[[talent:900]]']['name'], '统一更正')

    def test_name_only_rows_do_not_become_tree_nodes_or_simulation_items(self):
        write_name(self.term())
        write_name(self.term('spell', 30451))
        write_name(self.term('item', 123))
        self.assertEqual(WowTalentNodeMetadata.objects.count(), 0)
        self.assertEqual(WowTalentNodeMetadata.all_objects.count(), 3)
        self.assertEqual(WowSpellSnapshot.objects.count(), 0)
        self.assertEqual(WowItemSnapshot.objects.count(), 0)
        self.assertEqual(len(effective_names('12.1')), 3)
        self.assertFalse(effective_names('12.2'))

    def test_shared_name_is_the_single_editable_source_for_spell_names(self):
        write_name(self.term('spell', 30451))
        native = WowSpellSnapshot.objects.create(spell_id=30451, locale='enUS', snapshot_build='12.1.0.123',
            name='Arcane Blast', name_zh='当前中文')
        records = [r for r in effective_names('12.1') if r['kind'] == 'spell']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['model'], 'WowTalentNodeMetadata')
        self.assertEqual(records[0]['name_zh'], '奥术冲击')
        edited, _ = write_name(self.term('spell', 30451, name_zh='全站修正'), overwrite=True)
        self.assertEqual(edited['pk'], records[0]['pk'])
        self.assertEqual(WowTalentNodeMetadata.all_objects.get(pk=edited['pk']).name_zh, '全站修正')
        self.assertEqual(WowNewsGlossary.from_shared_localization('Arcane Blast')._terms.get('Arcane Blast'), '全站修正')

    def test_untranslated_snapshot_does_not_replace_verified_chinese(self):
        WowSpellSnapshot.objects.create(spell_id=30451, locale='enUS', snapshot_build='12.1.0.123',
            name='Arcane Blast', name_zh='Arcane Blast')
        row, _ = write_name(self.term('spell', 30451))
        self.assertEqual(row['name_zh'], '奥术冲击')

    def test_item_names_use_the_same_typed_table(self):
        item = WowItemSnapshot.objects.create(item_id=123, name='Arcane Blast')
        record, created = write_name(self.term('item', 123))
        self.assertTrue(created)
        self.assertEqual(record['model'], 'WowTalentNodeMetadata')
        self.assertEqual(WowTalentNodeMetadata.all_objects.get(pk=record['pk']).name_kind, 'item')
        self.assertEqual(WowItemSnapshot.objects.count(), 1)
        repeated, created = write_name(self.term('item', 123))
        self.assertFalse(created)
        self.assertEqual(repeated['pk'], record['pk'])

    def test_talent_provider_reuses_name_supplement_for_real_node(self):
        from botend.wow.talents.metadata import TalentMetadataProvider
        write_name(self.term())
        node = WowTalentNodeMetadata.objects.create(talent_version=self.version, class_name='Mage', spec_name='Arcane',
            source='db2', talent_id=900, node_id=800, spell_id=30451, name='Arcane Blast', name_zh='')
        rows = TalentMetadataProvider(talent_version=self.version).get_full_tree_nodes('Mage', 'Arcane')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['name'], '奥术冲击')
        self.assertEqual(rows[0]['node_id'], node.node_id)

    def test_shared_page_writes_native_row_and_requires_global_permission(self):
        user = get_user_model().objects.create_superuser('名称管理员', password='测试密码')
        self.client.force_login(user)
        native = WowTalentNodeMetadata.objects.create(talent_version=self.version, talent_id=900, node_id=800,
            name='Arcane Blast', name_zh='奥术冲击')
        response = self.client.post('/api/dashboard/wow-localization/', self.term(name_zh='统一中文'), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        native.refresh_from_db(); self.assertEqual(native.name_zh, '统一中文')
        self.assertEqual(WowTalentNodeMetadata.all_objects.count(), 1)
        self.assertEqual(self.client.get('/api/dashboard/class-guides/terms/').status_code, 404)
        user.is_superuser = False; user.save(update_fields=['is_superuser'])
        self.assertEqual(self.client.get('/api/dashboard/wow-localization/').status_code, 403)

    def test_macro_names_do_not_pollute_ordinary_translation(self):
        write_name({**self.term('macro'), 'name_en':'Corruption', 'name_zh':'腐蚀术'})
        glossary = build_guide_glossary([{'type':'html', 'html':'Corruption'}], '12.1')
        self.assertNotIn('Corruption', glossary._terms)
