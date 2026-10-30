"""中央天赋展示的分支、幂等和只读契约回归。"""
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from botend.models import WowSpellSnapshot, WowSpellEffectSnapshot, WowTalentNodeMetadata, WowTalentVersion
from botend.services.wow_spell_display import SpellDisplayReader, sync_spell_display
from botend.services.wow_talent_display import refresh_aggregate_talents
from botend.services.simc_skill_descriptions import attach_skill_damage_descriptions
from botend.services.simc_hero_talents import resolve_hero_talent_names
from botend.wow.talents.metadata import TalentMetadataProvider
from botend.wow.talents.versioning import TalentVersionResolver


class CentralSpellDisplayTests(TestCase):
    def setUp(self):
        self.retail, _ = WowTalentVersion.objects.update_or_create(key='retail', defaults={
            'branch': 'retail', 'is_active': True, 'status': 'active', 'current_build': '99.0.0.12345'})
        self.ptr, _ = WowTalentVersion.objects.update_or_create(key='ptr', defaults={
            'branch': 'ptr', 'is_active': True, 'status': 'active', 'current_build': '99.1.0.12346'})
        self.node = WowTalentNodeMetadata.objects.create(talent_version=self.retail,
            class_name='Warrior', spec_name='Fury', tree_type='spec', node_id=990001,
            talent_id=990002, spell_id=990003, source='db2_backfill', name='Test Talent',
            description_zh='造成$s1点伤害。', row=1, column=1)
        WowSpellEffectSnapshot.objects.create(branch='wow', locale='zhCN', spell_id=990003,
            effect_index=0, base_points='100', snapshot_build='old')
        WowTalentNodeMetadata.all_objects.create(talent_version=self.retail, localization_only=True,
            name_kind='spell', reference_id=990003, name='Test Talent', name_zh='中央天赋', icon='spell_test')
        self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('离线检查不得联网'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_preview_writes_nothing_and_repeated_merge_is_noop(self):
        output = StringIO()
        call_command('sync_central_spell_display', branch='retail', stdout=output)
        self.assertFalse(WowSpellSnapshot.objects.exists())
        first = sync_spell_display(branch='retail', apply=True)
        row = WowSpellSnapshot.objects.get(spell_id=990003)
        before = row.updated_at
        second = sync_spell_display(branch='retail', apply=True)
        row.refresh_from_db()
        self.assertEqual(first['created'], 1)
        self.assertEqual((second['created'], second['updated']), (0, 0))
        self.assertEqual(row.updated_at, before)
        self.assertEqual(WowSpellSnapshot.objects.count(), 1)

    def test_tree_aggregate_and_simc_share_compiled_chinese_without_parsing(self):
        sync_spell_display(branch='retail', apply=True)
        with patch('botend.wow.spell_text.SpellTextResolver.resolve', side_effect=AssertionError('页面不得解析')):
            tree = TalentMetadataProvider(talent_version=self.retail).get_full_tree_nodes('Warrior', 'Fury')[0]
            payload = {'talents': [{'node_id': self.node.node_id}]}
            refresh_aggregate_talents(payload, class_name='Warrior', spec_name='Fury')
            actor = {'class': 'warrior', 'spec': 'fury', 'actions': [{'spell_id': self.node.spell_id}]}
            attach_skill_damage_descriptions(actor, game_build='another-build')
        self.assertEqual(tree['name'], '中央天赋')
        self.assertEqual(tree['icon'], 'spell_test')
        self.assertEqual(tree['description_zh'], '造成100点伤害。')
        self.assertEqual(payload['talents'][0]['description_zh'], tree['description_zh'])
        self.assertEqual(actor['actions'][0]['description_zh'], tree['description_zh'])

    def test_ptr_data_never_fills_retail(self):
        WowTalentNodeMetadata.objects.create(talent_version=self.ptr, class_name='Warrior', spec_name='Fury',
            tree_type='spec', node_id=990005, talent_id=990006, spell_id=990007, description_zh='测试服机制')
        sync_spell_display(branch='ptr', apply=True)
        self.assertEqual(SpellDisplayReader('retail').get(990007), {})
        actor = {'class': 'warrior', 'spec': 'fury', 'actions': [{'spell_id': 990007}]}
        attach_skill_damage_descriptions(actor)
        self.assertNotIn('description_zh', actor['actions'][0])
        attach_skill_damage_descriptions(actor, branch='ptr')
        self.assertEqual(actor['actions'][0]['description_zh'], '测试服机制')

    def test_same_spell_keeps_spec_variants_in_one_record(self):
        WowTalentNodeMetadata.objects.create(talent_version=self.retail, class_name='Warrior', spec_name='Arms',
            tree_type='spec', node_id=990008, talent_id=990009, spell_id=990003, description_zh='武器专精机制')
        sync_spell_display(branch='retail', apply=True)
        reader = SpellDisplayReader()
        self.assertEqual(reader.get(990003, class_name='Warrior', spec_name='Arms')['description_zh'], '武器专精机制')
        self.assertEqual(reader.get(990003, class_name='Warrior', spec_name='Fury')['description_zh'], '造成100点伤害。')
        self.assertEqual(WowSpellSnapshot.objects.filter(spell_id=990003).count(), 1)

    def test_build_does_not_gate_spell_fallback(self):
        WowSpellSnapshot.objects.create(branch='wow', locale='zhCN', spell_id=990010,
            name_zh='已有中文', description='已有说明', snapshot_build='old')
        sync_spell_display(branch='retail', apply=True)
        result = TalentMetadataProvider(talent_version=self.retail)._lookup_spell_snapshot(990010)
        self.assertEqual(result['name'], '已有中文')

    def test_usage_defaults_cannot_switch_to_ptr(self):
        self.ptr.is_default_simulator = True
        self.ptr.save()
        for usage in ('simulator', 'stats', 'player_tree'):
            self.assertEqual(TalentVersionResolver.get_default(usage), self.retail)

    def test_hero_parser_keeps_explicit_branch(self):
        parser = Mock(return_value={'talent_render_model': {'trees': [{'tree_type': 'hero', 'nodes': [
            {'tree_type': 'hero', 'db2_subtree_id': 1, 'selected': True}]}]}})
        resolver = Mock(return_value='验证英雄树')
        resolve_hero_talent_names('验证字符串', 'warrior_fury', use_ptr=True,
                                 build_api_view=parser, hero_name_resolver=resolver)
        self.assertEqual(parser.call_args.kwargs['version_key'], 'ptr')
        resolver.assert_called_once_with(1, branch='ptr', class_name='Warrior', spec_name='Fury')

    def test_probe_only_reads_selected_branch(self):
        from botend.services.simc_skill_damage import SimcSkillDamageSnapshotService
        other = WowTalentNodeMetadata.objects.create(talent_version=self.ptr, class_name='Warrior', spec_name='Fury',
            tree_type='spec', node_id=990011, spell_id=990012)
        service = SimcSkillDamageSnapshotService(Mock(), backend=SimpleNamespace(identifier='production'))
        profile = SimpleNamespace(spec='fury', class_name='warrior')
        self.assertEqual([n.pk for n in service._talent_entries(profile)], [self.node.pk])
        service.backend.identifier = 'ptr'
        self.assertEqual([n.pk for n in service._talent_entries(profile)], [other.pk])

    def test_retired_node_is_not_read_but_still_exists(self):
        self.node.is_current = False
        self.node.save()
        self.assertFalse(WowTalentNodeMetadata.objects.filter(pk=self.node.pk).exists())
        self.assertTrue(WowTalentNodeMetadata.all_objects.filter(pk=self.node.pk).exists())

    def test_name_supplements_can_differ_between_branches(self):
        WowTalentNodeMetadata.all_objects.create(talent_version=self.ptr, localization_only=True,
            name_kind='spell', reference_id=990003, name='Test Talent', name_zh='测试服名称')
        sync_spell_display(branch='retail', apply=True)
        sync_spell_display(branch='ptr', apply=True)
        self.assertEqual(SpellDisplayReader('retail').get(990003)['name_zh'], '中央天赋')
        self.assertEqual(SpellDisplayReader('ptr').get(990003)['name_zh'], '测试服名称')

    def test_effect_merge_preserves_primary_keys_and_does_not_repeat_writes(self):
        from botend.management.commands.fetch_spell_effects import Command
        def rows():
            return [WowSpellEffectSnapshot(branch='wowxptr', locale='zhCN', spell_id=990020,
                effect_index=0, base_points='20', snapshot_build='source')]
        command = Command()
        self.assertEqual(command._write_effect_rows(rows()), 1)
        before = WowSpellEffectSnapshot.objects.get(spell_id=990020)
        self.assertEqual(command._write_effect_rows(rows()), 0)
        self.assertEqual(WowSpellEffectSnapshot.objects.get(spell_id=990020).pk, before.pk)
        changed = rows()
        changed[0].base_points = '25'
        self.assertEqual(command._write_effect_rows(changed), 1)
        before.refresh_from_db()
        self.assertEqual(before.base_points, '25')

    def test_compilation_never_reads_unscoped_latest_dump(self):
        with patch('botend.wow.spell_text._dump_file', side_effect=AssertionError('不可读取其他分支的 latest')):
            sync_spell_display(branch='ptr', apply=True)
            sync_spell_display(branch='retail', apply=True)

    def test_common_text_is_not_duplicated_per_node(self):
        sync_spell_display(branch='retail', apply=True)
        payload = WowSpellSnapshot.objects.get(spell_id=990003).display_data
        self.assertEqual(payload['default']['description_zh'], '造成100点伤害。')
        self.assertFalse(any('description_zh' in delta for delta in payload['contexts'].values()))

    def test_legacy_ptr_alias_merge_keeps_description_on_repeat(self):
        WowSpellSnapshot.objects.create(branch='wowt', locale='zhCN', spell_id=990030,
            name_zh='旧测试服技能', description='造成$s1点伤害。')
        WowSpellEffectSnapshot.objects.create(branch='wowt', locale='zhCN', spell_id=990030,
            effect_index=0, base_points='30')
        sync_spell_display(branch='ptr', apply=True)
        first = SpellDisplayReader('ptr').get(990030)
        self.assertEqual(first['description_zh'], '造成30点伤害。')
        second = sync_spell_display(branch='ptr', apply=True)
        self.assertEqual(second['updated'], 0)
        self.assertEqual(SpellDisplayReader('ptr').get(990030), first)

    def test_reader_fetches_each_batch_once_and_never_writes(self):
        sync_spell_display(branch='retail', apply=True)
        reader = SpellDisplayReader()
        with self.assertNumQueries(1):
            reader.preload([990003, 990003, 999999])
            for _ in range(5):
                self.assertEqual(reader.get(990003)['name_zh'], '中央天赋')
                self.assertEqual(reader.get(999999), {})

    def test_apl_catalog_uses_requested_branch_without_build_gate(self):
        from botend.services.simc_apl.catalog import query_symbol_catalog
        WowTalentNodeMetadata.objects.create(talent_version=self.ptr, class_name='Warrior', spec_name='Fury',
            tree_type='spec', node_id=990001, spell_id=990003, name_zh='测试服天赋', description_zh='测试服描述')
        sync_spell_display(branch='retail', apply=True)
        sync_spell_display(branch='ptr', apply=True)
        rows = query_symbol_catalog(None, 'unrelated-build', 'warrior', 'fury', branch='ptr')
        self.assertEqual([(row.name, row.description) for row in rows], [('测试服天赋', '测试服描述')])

    def test_apl_specialization_skills_do_not_require_build(self):
        from botend.models import WowSpecSpellMapSnapshot
        from botend.services.simc_apl.catalog import query_symbol_catalog
        WowSpecSpellMapSnapshot.objects.create(branch='wowxptr', spec_id=72, spell_id=990040)
        WowSpellSnapshot.objects.create(branch='wowxptr', locale='zhCN', spell_id=990040,
                                       name_zh='测试服专精技能', description='测试服技能说明')
        sync_spell_display(branch='ptr', apply=True)
        rows = query_symbol_catalog(None, None, 'warrior', 'fury', spec_id=72, branch='ptr')
        self.assertEqual([(row.name, row.description) for row in rows], [('测试服专精技能', '测试服技能说明')])

    def test_english_description_is_reported_incomplete(self):
        WowSpellSnapshot.objects.create(branch='wowxptr', locale='zhCN', spell_id=990041,
                                       name_zh='未翻译技能', description='Untranslated description')
        report = sync_spell_display(branch='ptr', apply=True)
        self.assertEqual(report['incomplete'], 1)
        self.assertEqual(WowSpellSnapshot.objects.get(spell_id=990041).display_data['status'], 'partial')

    def test_icon_import_requires_selected_branch_source(self):
        with patch('builtins.open', side_effect=AssertionError('不得读取全局目录')):
            with self.assertRaisesMessage(CommandError, '当前分支尚无 DB2 来源目录'):
                call_command('fetch_talent_icons', version_key='ptr', stdout=StringIO())
