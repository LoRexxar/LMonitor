"""共享名称写入、原表复用、结构隔离与版本回归。"""
import re
from django.test import TestCase
from django.template.loader import render_to_string
from django.contrib.auth import get_user_model
from botend.models import WowTalentVersion, WowTalentNodeMetadata, WowSpellSnapshot, WowItemSnapshot
from botend.services.wow_localization import current_reference_version, names_for, write_name, effective_names, export_names
from botend.services.class_guide_content import resolve_references
from botend.services.class_guide_service import build_guide_glossary
from botend.services.wow_news_glossary_service import WowNewsGlossary
from botend.guide_models import ClassGuide


class SharedNameTests(TestCase):
    def setUp(self):
        self.version = WowTalentVersion.objects.create(key='retail-12.1', major_version='12.1', branch='retail',
            current_build='12.1.0.123', is_active=True, is_default_player_tree=True)

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
            node_id=900, talent_id=8, spell_id=30451, name='Arcane Blast', name_zh='共享中文')
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

    def test_explicit_talent_id_resolves_without_class_or_specialization_scope(self):
        WowTalentNodeMetadata.objects.create(
            talent_version=self.version, class_name='Warrior', spec_name='Arms',
            node_id=112123, talent_id=99852, spell_id=7384, display_spell_id=7384,
            name='Overpower', name_zh='压制', icon='ability_meleedamage',
            description_zh='对敌人造成伤害。',
        )
        blocks = [{'id': 'p', 'type': 'html', 'html': '<p>[[talent:112123]]</p>'}]

        reference = resolve_references(blocks, '12.1', 'mage', 'arcane')['[[talent:112123]]']

        self.assertTrue(reference['resolved'])
        self.assertEqual(reference['name'], '压制')
        self.assertEqual(reference['tooltip_text'], '对敌人造成伤害。')
        self.assertEqual(reference['tooltip_source'], 'talent_metadata')

    def test_source_text_reference_ignores_removed_guide_version(self):
        WowTalentNodeMetadata.all_objects.create(
            talent_version=self.version,
            localization_only=True,
            name_kind='talent',
            reference_id=102435,
            name='Focused Enmity',
            name_zh='专注敌意',
        )

        rows = names_for('12.0.7', source_text='旧攻略 [[talent:102435]]')

        self.assertEqual([(row['kind'], row['object_id']) for row in rows], [('talent', 102435)])

    def test_native_talent_record_exports_entry_id_not_trait_node_id(self):
        WowTalentNodeMetadata.objects.create(
            talent_version=self.version,
            class_name='Warrior',
            spec_name='Arms',
            node_id=112123,
            talent_id=99852,
            spell_id=7384,
            name='Overpower',
            name_zh='压制',
        )

        rows = effective_names('9.9', reference_ids={'talent': {112123}})

        self.assertEqual([(row['kind'], row['object_id']) for row in rows], [('talent', 112123)])

    def test_talent_reference_uses_entry_id_not_same_numbered_trait_node_id(self):
        WowTalentNodeMetadata.objects.create(
            talent_version=self.version, class_name='Mage', spec_name='Arcane',
            node_id=70001, talent_id=112123, spell_id=70002,
            name='Unrelated Trait Node', name_zh='同号但无关的天赋',
            description_zh='不得串入的效果。',
        )
        WowTalentNodeMetadata.objects.create(
            talent_version=self.version, class_name='Warrior', spec_name='Arms',
            node_id=112123, talent_id=99852, spell_id=7384,
            name='Overpower', name_zh='压制', description_zh='正确的压制效果。',
        )

        reference = resolve_references(
            [{'id': 'p', 'type': 'html', 'html': '<p>[[talent:112123]]</p>'}],
            '12.1', 'mage', 'arcane',
        )['[[talent:112123]]']

        self.assertEqual(reference['name'], '压制')
        self.assertEqual(reference['tooltip_text'], '正确的压制效果。')

    def test_explicit_spell_id_resolves_from_native_talent_without_class_or_specialization_scope(self):
        WowTalentNodeMetadata.objects.create(
            talent_version=self.version, class_name='Warrior', spec_name='Arms',
            node_id=112123, talent_id=99852, spell_id=7384, display_spell_id=1311653,
            name='Overpower', name_zh='压制', icon='ability_meleedamage',
        )
        for spell_id, description in ((7384, '压制技能描述。'), (1311653, '压制展示技能描述。')):
            WowSpellSnapshot.objects.create(
                branch='wow', locale='zhCN', spell_id=spell_id,
                name='压制', name_zh='压制', description=description,
                snapshot_build='12.1.0.999',
            )
        blocks = [{
            'id': 'p', 'type': 'html',
            'html': '<p>[[spell:7384]] [[spell:1311653]]</p>',
        }]

        references = resolve_references(blocks, '12.1', 'mage', 'arcane')

        for token, description in (
            ('[[spell:7384]]', '压制技能描述。'),
            ('[[spell:1311653]]', '压制展示技能描述。'),
        ):
            self.assertTrue(references[token]['resolved'])
            self.assertEqual(references[token]['name'], '压制')
            self.assertEqual(references[token]['tooltip_text'], description)
            self.assertEqual(references[token]['tooltip_source'], 'spell_snapshot')

    def test_reference_uses_global_name_and_current_spell_snapshot_without_guide_version_binding(self):
        write_name(self.term('spell', 30451))
        self.version.is_default_player_tree = False
        self.version.is_active = False
        self.version.save(update_fields=['is_default_player_tree', 'is_active'])
        WowTalentVersion.objects.create(
            key='retail-12.2', major_version='12.2', branch='retail',
            current_build='12.2.0.999', is_active=True, is_default_player_tree=True,
        )
        WowSpellSnapshot.objects.create(
            branch='wow', locale='zhCN', spell_id=30451,
            description='当前权威效果。', snapshot_build='12.2.0.999',
        )

        reference = resolve_references(
            [{'id': 'p', 'type': 'html', 'html': '<p>[[spell:30451]]</p>'}],
            '10.0',
        )['[[spell:30451]]']

        self.assertEqual(reference['name'], '奥术冲击')
        self.assertEqual(reference['tooltip_text'], '当前权威效果。')

    def test_current_reference_version_prefers_active_tree_over_inactive_default(self):
        WowTalentVersion.objects.exclude(pk=self.version.pk).delete()
        self.version.is_active = False
        self.version.save(update_fields=['is_active'])
        active = WowTalentVersion.objects.create(
            key='retail-12.2', major_version='12.2', branch='retail',
            current_build='12.2.0.999', is_active=True, is_default_player_tree=False,
        )

        self.assertEqual(current_reference_version(), active)

    def test_current_reference_version_prefers_retail_when_ptr_is_also_active(self):
        WowTalentVersion.objects.exclude(pk=self.version.pk).delete()
        self.version.branch = 'retail'
        self.version.is_active = True
        self.version.is_default_player_tree = True
        self.version.save(update_fields=['branch', 'is_active', 'is_default_player_tree'])
        WowTalentVersion.objects.create(
            key='ptr', major_version='12.1.5', branch='ptr',
            current_build='12.1.5.99999', is_active=True,
            is_default_player_tree=True,
        )

        self.assertEqual(current_reference_version(), self.version)

    def test_explicit_reference_searches_active_branches_with_retail_priority(self):
        ptr, _ = WowTalentVersion.objects.update_or_create(
            key='ptr', defaults=dict(
                major_version='12.1.5', branch='ptr',
                current_build='12.1.5.99999', is_active=True,
            ),
        )
        WowTalentNodeMetadata.objects.create(
            talent_version=ptr, class_name='Mage', spec_name='Arcane',
            node_id=880001, talent_id=880101, spell_id=880201,
            name='PTR Only Talent', name_zh='测试服独有天赋',
            description_zh='测试服效果。',
        )
        blocks = [{'id': 'p', 'type': 'html', 'html': '[[talent:880001]]'}]

        reference = resolve_references(blocks, '10.0')['[[talent:880001]]']
        self.assertEqual(reference['name'], '测试服独有天赋')

        WowTalentNodeMetadata.objects.create(
            talent_version=self.version, class_name='Mage', spec_name='Arcane',
            node_id=880001, talent_id=880103, spell_id=880203,
            name='Legacy Retail Talent', name_zh='旧正式服天赋',
            description_zh='旧正式服效果。',
        )
        stable_retail, _ = WowTalentVersion.objects.update_or_create(
            key='retail', defaults=dict(
                major_version='12.1.0', branch='retail',
                current_build='12.1.0.69283', is_active=True,
            ),
        )
        WowTalentNodeMetadata.objects.create(
            talent_version=stable_retail, class_name='Mage', spec_name='Arcane',
            node_id=880001, talent_id=880102, spell_id=880202,
            name='Retail Talent', name_zh='正式服天赋',
            description_zh='正式服效果。',
        )
        reference = resolve_references(blocks, '99.9')['[[talent:880001]]']
        self.assertEqual(reference['name'], '正式服天赋')
        self.assertEqual(reference['tooltip_text'], '正式服效果。')

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
        native = WowTalentNodeMetadata.objects.create(talent_version=self.version, talent_id=800, node_id=900,
            name='Arcane Blast', name_zh='奥术冲击')
        response = self.client.post('/api/dashboard/wow-localization/',
            {**self.term(name_zh='统一中文'), 'create': True}, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        native.refresh_from_db(); self.assertEqual(native.name_zh, '统一中文')
        self.assertEqual(WowTalentNodeMetadata.all_objects.count(), 1)
        self.assertEqual(self.client.get('/api/dashboard/class-guides/terms/').status_code, 404)
        user.is_superuser = False; user.save(update_fields=['is_superuser'])
        self.assertEqual(self.client.get('/api/dashboard/wow-localization/').status_code, 403)

    def test_create_uses_the_reference_kind_from_the_live_guide(self):
        user = get_user_model().objects.create_superuser('引用名称管理员', password='测试密码')
        self.client.force_login(user)
        native = WowTalentNodeMetadata.objects.create(
            talent_version=self.version, class_name='Warrior', spec_name='Arms', talent_id=99852,
            node_id=112123, spell_id=7384, name='Overpower', name_zh='', icon='ability_meleedamage')
        ClassGuide.objects.create(
            title='狂暴战攻略', slug='fury-warrior-raid-guide', class_name='warrior', spec_name='fury',
            spec_id=72, game_version='12.1', content_markdown='使用 [[talent:112123|Overpower]]。')

        response = self.client.post('/api/dashboard/wow-localization/', {
            **self.term('spell', 112123, name_zh='压制'), 'name_en': 'Overpower', 'icon': '', 'create': True,
        }, content_type='application/json')

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['kind'], 'talent')
        native.refresh_from_db()
        self.assertEqual(native.name_zh, '压制')
        self.assertEqual(native.reference_aliases, [112123])
        self.assertFalse(WowTalentNodeMetadata.all_objects.filter(
            localization_only=True, name_kind='spell', reference_id=112123).exists())
        resolved = resolve_references(
            [{'id': 'p', 'type': 'html', 'html': '[[talent:112123]]'}], '12.1', 'warrior', 'fury')
        self.assertEqual(resolved['[[talent:112123]]']['name'], '压制')
        self.assertTrue(resolved['[[talent:112123]]']['icon'].endswith('/ability_meleedamage.jpg'))

    def test_create_only_requires_id_and_chinese_and_generates_metadata(self):
        user = get_user_model().objects.create_superuser('自动识别管理员', password='测试密码')
        self.client.force_login(user)
        WowSpellSnapshot.objects.create(
            branch='wow', locale='enUS', spell_id=30451, name='Arcane Blast',
            icon='spell_nature_lightning', snapshot_build='12.1.0.123')
        ClassGuide.objects.create(
            title='奥法攻略', slug='arcane-mage-mythic-guide', class_name='mage', spec_name='arcane',
            spec_id=62, game_version='12.1', content_markdown='使用 [[spell:30451|Arcane Blast]]。')

        response = self.client.post('/api/dashboard/wow-localization/', {
            'object_id': 30451, 'name_zh': '奥术冲击', 'create': True,
            'game_version': 'forged-version', 'kind': 'item', 'name_en': 'Forged Name',
            'icon': 'forged_icon', 'evidence': '伪造依据',
        }, content_type='application/json')

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {
            'id': response.json()['id'], 'kind': 'spell', 'object_id': 30451,
            'game_version': '12.1', 'name_en': 'Arcane Blast',
            'icon': 'spell_nature_lightning',
        })
        row = WowTalentNodeMetadata.all_objects.get(
            localization_only=True, name_kind='spell', reference_id=30451)
        self.assertEqual(row.name_zh, '奥术冲击')
        self.assertEqual(row.name, 'Arcane Blast')
        self.assertEqual(row.icon, 'spell_nature_lightning')
        self.assertIn('系统自动识别', row.localization_evidence)

    def test_create_keeps_the_concrete_default_version_when_major_versions_overlap(self):
        user = get_user_model().objects.create_superuser('同版本分支管理员', password='测试密码')
        self.client.force_login(user)
        WowTalentVersion.objects.create(
            key='ptr-12.1', major_version='12.1', branch='ptr', current_build='12.1.0.999',
            is_active=True, is_default_player_tree=False)
        ClassGuide.objects.create(
            title='正式服攻略', slug='retail-guide', class_name='mage', spec_name='arcane',
            spec_id=62, game_version='12.1', content_markdown='使用 [[spell:330451|Retail Spell]]。')

        response = self.client.post('/api/dashboard/wow-localization/', {
            'object_id': 330451, 'name_zh': '正式服技能', 'create': True,
        }, content_type='application/json')

        self.assertEqual(response.status_code, 200, response.content)
        row = WowTalentNodeMetadata.all_objects.get(name_kind='spell', reference_id=330451)
        self.assertEqual(row.talent_version, self.version)
        self.assertEqual(row.name, 'Retail Spell')

    def test_create_uses_current_authoritative_source_instead_of_guide_version_labels(self):
        user = get_user_model().objects.create_superuser('跨版本名称管理员', password='测试密码')
        self.client.force_login(user)
        self.version.is_default_player_tree = False
        self.version.save(update_fields=['is_default_player_tree'])
        current = WowTalentVersion.objects.create(
            key='retail-12.2', major_version='12.2', branch='retail', current_build='12.2.0.456',
            is_active=True, is_default_player_tree=True)
        WowSpellSnapshot.objects.create(
            branch='wow', locale='enUS', spell_id=430451, name='Current Authoritative Name',
            icon='current_authoritative_icon', snapshot_build='12.2.0.789',
        )
        ClassGuide.objects.create(
            title='旧攻略', slug='old-guide', class_name='mage', spec_name='arcane',
            spec_id=62, game_version='12.1', content_markdown='使用 [[spell:430451|Old Label]]。')
        ClassGuide.objects.create(
            title='新攻略', slug='new-guide', class_name='mage', spec_name='arcane',
            spec_id=62, game_version='12.2', content_markdown='使用 [[spell:430451|Current Label]]。')

        response = self.client.post('/api/dashboard/wow-localization/', {
            'object_id': 430451, 'name_zh': '当前技能', 'create': True,
        }, content_type='application/json')

        self.assertEqual(response.status_code, 200, response.content)
        row = WowTalentNodeMetadata.all_objects.get(name_kind='spell', reference_id=430451)
        self.assertEqual(row.talent_version, current)
        self.assertEqual(row.name, 'Current Authoritative Name')
        self.assertEqual(row.icon, 'current_authoritative_icon')

    def test_create_uses_current_reference_data_even_when_only_old_guide_mentions_id(self):
        user = get_user_model().objects.create_superuser('全局引用管理员', password='测试密码')
        self.client.force_login(user)
        self.version.is_default_player_tree = False
        self.version.is_active = False
        self.version.save(update_fields=['is_default_player_tree', 'is_active'])
        current = WowTalentVersion.objects.create(
            key='retail-12.2', major_version='12.2', branch='retail', current_build='12.2.0.999',
            is_active=True, is_default_player_tree=True,
        )
        WowSpellSnapshot.objects.create(
            branch='wow', locale='enUS', spell_id=830451, name='Current Spell',
            icon='current_icon', snapshot_build='12.2.0.1000',
        )
        ClassGuide.objects.create(
            title='旧版本攻略', slug='old-only-guide', class_name='mage', spec_name='arcane',
            spec_id=62, game_version='12.1', content_markdown='使用 [[spell:830451|Old Guide Label]]。',
        )

        response = self.client.post('/api/dashboard/wow-localization/', {
            'object_id': 830451, 'name_zh': '当前技能', 'create': True,
        }, content_type='application/json')

        self.assertEqual(response.status_code, 200, response.content)
        row = WowTalentNodeMetadata.all_objects.get(name_kind='spell', reference_id=830451)
        self.assertEqual(row.talent_version, current)
        self.assertEqual(row.name, 'Current Spell')
        self.assertEqual(row.icon, 'current_icon')

    def test_write_reuses_global_reference_row_after_current_version_changes(self):
        first, created = write_name(self.term('spell', 930451))
        self.assertTrue(created)
        self.version.is_default_player_tree = False
        self.version.save(update_fields=['is_default_player_tree'])
        WowTalentVersion.objects.create(
            key='retail-12.2', major_version='12.2', branch='retail', current_build='12.2.0.999',
            is_active=True, is_default_player_tree=True,
        )

        updated, created = write_name({
            **self.term('spell', 930451), 'game_version': '12.2', 'name_zh': '全局修正',
        }, overwrite=True)

        self.assertFalse(created)
        self.assertEqual(updated['pk'], first['pk'])
        self.assertEqual(WowTalentNodeMetadata.all_objects.filter(
            name_kind='spell', reference_id=930451).count(), 1)
        self.assertEqual(WowTalentNodeMetadata.all_objects.get(pk=first['pk']).name_zh, '全局修正')

    def test_create_uses_current_snapshot_row_without_snapshot_build_coupling(self):
        user = get_user_model().objects.create_superuser('快照边界管理员', password='测试密码')
        self.client.force_login(user)
        WowSpellSnapshot.objects.create(
            branch='wow', locale='enUS', spell_id=530451, name='Current Snapshot Name',
            icon='current_snapshot_icon', snapshot_build='12.0.0.1')
        WowSpellSnapshot.objects.create(
            branch='wow', locale='zhCN', spell_id=530451, name='错误写入英文列的中文名',
            icon='zh_icon', snapshot_build='12.1.0.123')
        ClassGuide.objects.create(
            title='快照边界攻略', slug='snapshot-boundary-guide', class_name='mage', spec_name='arcane',
            spec_id=62, game_version='12.1', content_markdown='使用 [[spell:530451|Guide Label]]。')

        response = self.client.post('/api/dashboard/wow-localization/', {
            'object_id': 530451, 'name_zh': '攻略中文名', 'create': True,
        }, content_type='application/json')

        self.assertEqual(response.status_code, 200, response.content)
        row = WowTalentNodeMetadata.all_objects.get(name_kind='spell', reference_id=530451)
        self.assertEqual(row.name, 'Current Snapshot Name')
        self.assertEqual(row.icon, 'current_snapshot_icon')
        self.assertIn('WowSpellSnapshot', row.localization_evidence)

    def test_snapshot_only_spell_uses_current_storage_bucket_with_other_branch_fallback(self):
        user = get_user_model().objects.create_superuser('PTR 快照管理员', password='测试密码')
        self.client.force_login(user)
        ptr = WowTalentVersion.objects.create(
            key='ptr-12.1', major_version='12.1', branch='ptr', current_build='12.1.0.999',
            is_active=True, is_default_player_tree=False)
        WowSpellSnapshot.objects.create(
            branch='wowt', locale='enUS', spell_id=630451, name='PTR Spell',
            icon='ptr_icon', snapshot_build='12.1.0.999')

        response = self.client.post('/api/dashboard/wow-localization/', {
            'object_id': 630451, 'name_zh': '测试服技能', 'create': True,
        }, content_type='application/json')

        self.assertEqual(response.status_code, 200, response.content)
        row = WowTalentNodeMetadata.all_objects.get(name_kind='spell', reference_id=630451)
        self.assertEqual(row.talent_version, self.version)
        self.assertEqual(row.name, 'PTR Spell')
        self.assertEqual(row.icon, 'ptr_icon')

    def test_snapshot_only_spell_does_not_require_matching_talent_version_build(self):
        user = get_user_model().objects.create_superuser('无效快照管理员', password='测试密码')
        self.client.force_login(user)
        WowSpellSnapshot.objects.create(
            branch='wow', locale='enUS', spell_id=730451, name='Snapshot Name',
            icon='snapshot_icon', snapshot_build='12.0.0.1')
        WowSpellSnapshot.objects.create(
            branch='wow', locale='zhCN', spell_id=730451, name='中文快照',
            icon='zh_icon', snapshot_build='12.1.0.123')

        response = self.client.post('/api/dashboard/wow-localization/', {
            'object_id': 730451, 'name_zh': '快照技能', 'create': True,
        }, content_type='application/json')

        self.assertEqual(response.status_code, 200, response.content)
        row = WowTalentNodeMetadata.all_objects.get(reference_id=730451)
        self.assertEqual(row.talent_version, self.version)
        self.assertEqual(row.name, 'Snapshot Name')
        self.assertEqual(row.icon, 'snapshot_icon')

    def test_edit_preserves_the_target_concrete_version_across_branches(self):
        user = get_user_model().objects.create_superuser('分支编辑管理员', password='测试密码')
        self.client.force_login(user)
        ptr = WowTalentVersion.objects.create(
            key='ptr-12.1', major_version='12.1', branch='ptr', current_build='12.1.0.999',
            is_active=True, is_default_player_tree=False)
        target = WowTalentNodeMetadata.all_objects.create(
            talent_version=self.version, class_name='Mage', spec_name='Arcane', tree_type='class',
            talent_id=740001, node_id=740101, spell_id=740201, name='Retail Name', name_zh='旧译名')

        response = self.client.post('/api/dashboard/wow-localization/', {
            'object_id': 740001, 'name_zh': '正式服译名', 'record_pk': target.pk,
            'edit_state': {'name_en': 'Retail Name', 'name_zh': '旧译名', 'icon': '',
                           'evidence': '', 'duplicate_count': 1},
        }, content_type='application/json')

        self.assertEqual(response.status_code, 200, response.content)
        target.refresh_from_db()
        self.assertEqual(target.name_zh, '正式服译名')
        self.assertFalse(WowTalentNodeMetadata.all_objects.filter(
            talent_version=ptr, talent_id=740001).exists())

    def test_fractional_reference_ids_are_rejected_for_create_and_edit(self):
        user = get_user_model().objects.create_superuser('编号校验管理员', password='测试密码')
        self.client.force_login(user)
        ClassGuide.objects.create(
            title='编号攻略', slug='identity-guide', class_name='mage', spec_name='arcane',
            spec_id=62, game_version='12.1', content_markdown='使用 [[spell:840001|Identity Spell]]。')
        create_response = self.client.post('/api/dashboard/wow-localization/', {
            'object_id': 840001.5, 'name_zh': '错误编号', 'create': True,
        }, content_type='application/json')
        target = WowTalentNodeMetadata.all_objects.create(
            talent_version=self.version, class_name='Mage', spec_name='Arcane', tree_type='class',
            talent_id=840002, node_id=840102, spell_id=840202, name='Edit Target', name_zh='旧译名')
        edit_response = self.client.post('/api/dashboard/wow-localization/', {
            'object_id': 840002.5, 'name_zh': '错误编辑', 'record_pk': target.pk,
            'edit_state': {'name_en': 'Edit Target', 'name_zh': '旧译名', 'icon': '',
                           'evidence': '', 'duplicate_count': 1},
        }, content_type='application/json')

        self.assertEqual(create_response.status_code, 400, create_response.content)
        self.assertEqual(edit_response.status_code, 400, edit_response.content)
        target.refresh_from_db()
        self.assertEqual(target.name_zh, '旧译名')

    def test_create_rejects_ambiguous_native_names_for_the_same_talent_entry_id(self):
        user = get_user_model().objects.create_superuser('歧义名称管理员', password='测试密码')
        self.client.force_login(user)
        for talent_id, name in ((940101, 'First Choice'), (940102, 'Second Choice')):
            WowTalentNodeMetadata.all_objects.create(
                talent_version=self.version, class_name='Mage', spec_name='Arcane', tree_type='spec',
                talent_id=talent_id, node_id=940001, spell_id=talent_id, name=name, name_zh='')
        ClassGuide.objects.create(
            title='歧义攻略', slug='ambiguous-native-guide', class_name='mage', spec_name='arcane',
            spec_id=62, game_version='12.1', content_markdown='使用 [[talent:940001|First Choice]]。')

        response = self.client.post('/api/dashboard/wow-localization/', {
            'object_id': 940001, 'name_zh': '不可猜测', 'create': True,
        }, content_type='application/json')

        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(WowTalentNodeMetadata.all_objects.filter(
            node_id=940001).exclude(name_zh='').exists())
        self.assertFalse(WowTalentNodeMetadata.all_objects.filter(
            localization_only=True, reference_id=940001).exists())

    def test_management_form_only_exposes_id_and_chinese_name_inputs(self):
        html = render_to_string('dashboard/guide_terms.html')
        editor = re.search(r'<form id="guide-term-form">(.*?)</form>', html, re.S).group(1)
        self.assertIn('name="object_id"', editor)
        self.assertIn('name="name_zh"', editor)
        for generated_field in ('game_version', 'kind', 'name_en', 'icon', 'evidence'):
            self.assertNotIn(f'name="{generated_field}"', editor)
        self.assertIn('<dl data-generated-fields>', editor)
        self.assertNotIn('<dl data-generated-fields hidden>', editor)

    def test_stale_client_without_explicit_create_or_edit_state_is_rejected(self):
        user = get_user_model().objects.create_superuser('旧页面管理员', password='测试密码')
        self.client.force_login(user)
        response = self.client.post('/api/dashboard/wow-localization/', self.term(), content_type='application/json')
        self.assertEqual(response.status_code, 409)
        self.assertFalse(WowTalentNodeMetadata.all_objects.exists())

    def test_management_list_collapses_identical_cross_context_nodes(self):
        user = get_user_model().objects.create_superuser('名称列表管理员', password='测试密码')
        self.client.force_login(user)
        first = WowTalentNodeMetadata.all_objects.create(
            talent_version=self.version, class_name='Mage', spec_name='Arcane', tree_type='class',
            talent_id=99852, node_id=123389, spell_id=111, name='Slayer', name_zh='斩杀者', source='db2_backfill')
        WowTalentNodeMetadata.all_objects.create(
            talent_version=self.version, class_name='Mage', spec_name='Fire', tree_type='class',
            talent_id=99852, node_id=123390, spell_id=222, name='Slayer', name_zh='斩杀者', source='db2_backfill')
        WowTalentNodeMetadata.all_objects.create(
            talent_version=self.version, class_name='Mage', spec_name='Frost', tree_type='class',
            talent_id=99852, node_id=123391, spell_id=333, name='Mountain Thane', name_zh='山丘领主', source='db2_backfill')

        data = self.client.get('/api/dashboard/wow-localization/', {'version': '12.1', 'q': '99852'}).json()

        self.assertEqual(data['total'], 2)
        self.assertEqual(len(data['records']), 2)
        slayer = next(row for row in data['records'] if row['name_en'] == 'Slayer')
        self.assertEqual(slayer['pk'], first.pk)
        self.assertEqual(slayer['duplicate_count'], 2)

    def test_management_list_keeps_byte_distinct_labels_separate(self):
        user = get_user_model().objects.create_superuser('精确名称管理员', password='测试密码')
        self.client.force_login(user)
        for name, node_id in [('Slayer', 123389), ('slayer', 123390)]:
            WowTalentNodeMetadata.all_objects.create(
                talent_version=self.version, class_name='Mage', spec_name='Arcane', tree_type='class',
                talent_id=99852, node_id=node_id, spell_id=node_id, name=name, name_zh='斩杀者')

        data = self.client.get('/api/dashboard/wow-localization/', {'version': '12.1', 'q': '99852'}).json()

        self.assertEqual(data['total'], 2)
        self.assertEqual({row['name_en'] for row in data['records']}, {'Slayer', 'slayer'})

    def test_targeted_edit_updates_only_clicked_label_and_accepts_legacy_blank_evidence(self):
        user = get_user_model().objects.create_superuser('名称编辑管理员', password='测试密码')
        self.client.force_login(user)
        first = WowTalentNodeMetadata.all_objects.create(
            talent_version=self.version, class_name='Mage', spec_name='Arcane', tree_type='class',
            talent_id=99852, node_id=123389, spell_id=111, name='Slayer', name_zh='斩杀者', source='db2_backfill')
        same_label = WowTalentNodeMetadata.all_objects.create(
            talent_version=self.version, class_name='Mage', spec_name='Fire', tree_type='class',
            talent_id=99852, node_id=123390, spell_id=222, name='Slayer', name_zh='斩杀者', source='db2_backfill')
        other_label = WowTalentNodeMetadata.all_objects.create(
            talent_version=self.version, class_name='Mage', spec_name='Frost', tree_type='class',
            talent_id=99852, node_id=123391, spell_id=333, name='Mountain Thane', name_zh='山丘领主', source='db2_backfill')
        payload = {**self.term(identity=99852, name_zh='屠戮者'), 'name_en': 'Slayer',
                   'evidence': '', 'record_pk': first.pk,
                   'edit_state': {'name_en': 'Slayer', 'name_zh': '斩杀者', 'icon': '',
                                  'evidence': '', 'duplicate_count': 2}}

        response = self.client.post('/api/dashboard/wow-localization/', payload, content_type='application/json')

        self.assertEqual(response.status_code, 200, response.content)
        first.refresh_from_db(); same_label.refresh_from_db(); other_label.refresh_from_db()
        self.assertEqual(first.name_zh, '屠戮者')
        self.assertEqual(same_label.name_zh, '屠戮者')
        self.assertEqual(other_label.name_zh, '山丘领主')
        self.assertFalse(WowTalentNodeMetadata.all_objects.filter(localization_only=True).exists())

    def test_secondary_identifier_search_keeps_complete_edit_group(self):
        user = get_user_model().objects.create_superuser('别名名称管理员', password='测试密码')
        self.client.force_login(user)
        first = WowTalentNodeMetadata.all_objects.create(
            talent_version=self.version, class_name='Mage', spec_name='Arcane', tree_type='class',
            talent_id=99852, node_id=123389, spell_id=111, name='Slayer', name_zh='斩杀者')
        second = WowTalentNodeMetadata.all_objects.create(
            talent_version=self.version, class_name='Mage', spec_name='Fire', tree_type='class',
            talent_id=99852, node_id=123390, spell_id=222, name='Slayer', name_zh='斩杀者',
            reference_aliases=[445566])

        data = self.client.get('/api/dashboard/wow-localization/', {
            'version': '12.1', 'kind': 'talent', 'q': '445566'}).json()

        self.assertEqual(data['total'], 1)
        row = data['records'][0]
        self.assertEqual(row['pk'], second.pk)
        self.assertEqual(row['duplicate_count'], 2)
        self.assertIn(445566, row['identifiers'])
        payload = {**self.term(identity=99852, name_zh='屠戮者'), 'name_en': 'Slayer',
                   'record_pk': row['pk'], 'edit_state': {
                       'name_en': row['name_en'], 'name_zh': row['name_zh'], 'icon': row['icon'],
                       'evidence': row['evidence'], 'duplicate_count': row['duplicate_count']}}
        response = self.client.post('/api/dashboard/wow-localization/', payload, content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        first.refresh_from_db(); second.refresh_from_db()
        self.assertEqual(first.name_zh, '屠戮者')
        self.assertEqual(second.name_zh, '屠戮者')

    def test_targeted_edit_rejects_stale_representative_or_group(self):
        user = get_user_model().objects.create_superuser('并发名称管理员', password='测试密码')
        self.client.force_login(user)
        first = WowTalentNodeMetadata.all_objects.create(
            talent_version=self.version, class_name='Mage', spec_name='Arcane', tree_type='class',
            talent_id=99852, node_id=123389, spell_id=111, name='Slayer', name_zh='斩杀者')
        second = WowTalentNodeMetadata.all_objects.create(
            talent_version=self.version, class_name='Mage', spec_name='Fire', tree_type='class',
            talent_id=99852, node_id=123390, spell_id=222, name='Slayer', name_zh='斩杀者')
        state = {'name_en': 'Slayer', 'name_zh': '斩杀者', 'icon': '',
                 'evidence': '', 'duplicate_count': 2}
        payload = {**self.term(identity=99852, name_zh='屠戮者'), 'name_en': 'Slayer',
                   'record_pk': first.pk, 'edit_state': state}

        second.name_zh = '另一译名'; second.save(update_fields=['name_zh'])
        response = self.client.post('/api/dashboard/wow-localization/', payload, content_type='application/json')
        self.assertEqual(response.status_code, 409, response.content)
        first.refresh_from_db(); self.assertEqual(first.name_zh, '斩杀者')

        second.name_zh = '斩杀者'; second.save(update_fields=['name_zh'])
        first.name_zh = '较新译名'; first.save(update_fields=['name_zh'])
        response = self.client.post('/api/dashboard/wow-localization/', payload, content_type='application/json')
        self.assertEqual(response.status_code, 409, response.content)
        first.refresh_from_db(); self.assertEqual(first.name_zh, '较新译名')

    def test_macro_names_do_not_pollute_ordinary_translation(self):
        write_name({**self.term('macro'), 'name_en':'Corruption', 'name_zh':'腐蚀术'})
        glossary = build_guide_glossary([{'type':'html', 'html':'Corruption'}], '12.1')
        self.assertNotIn('Corruption', glossary._terms)
