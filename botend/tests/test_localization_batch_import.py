"""Batch import keeps real name/display semantics without per-term rebuilds."""
from unittest.mock import patch
from django.test import TestCase
from django.core.exceptions import ValidationError
from botend.models import WowTalentVersion, WowTalentNodeMetadata, WowSpellSnapshot
from botend.services import wow_localization as names
from botend.services import wow_spell_display as display


class BatchNameImportTests(TestCase):
    def setUp(self):
        self.version, _ = WowTalentVersion.objects.update_or_create(key='retail', defaults={
            'major_version': '12.1', 'branch': 'retail', 'is_active': True, 'status': 'active',
            'source_dir': '', 'current_build': '12.1.0.123', 'is_default_player_tree': True})

    def term(self, identity, **changes):
        return dict(game_version='retail', kind='spell', object_id=identity,
                    name_en='Source name', name_zh='来源中文', icon='spell_arcane_blast',
                    evidence='权威名称', **changes)

    def test_batch_dedup_conflicts_native_nodes_and_real_display_once(self):
        old = WowTalentNodeMetadata.all_objects.create(talent_version=self.version,
            localization_only=True, name_kind='spell', reference_id=930001,
            name='Manual', name_zh='人工中文', icon='manual_icon', localization_evidence='人工证据')
        node = WowTalentNodeMetadata.objects.create(talent_version=self.version,
            class_name='Mage', spec_name='Arcane', node_id=930003, talent_id=930004,
            display_spell_id=930005, name='Native', name_zh='原生中文')
        a, b = self.term(930001), self.term(930002)
        talent = {**self.term(930003), 'kind': 'talent', 'name_en': 'Native'}
        item = {**self.term(930002), 'kind': 'item', 'name_zh': '物品中文'}
        with patch.object(display, 'sync_spell_display', wraps=display.sync_spell_display) as sync:
            records = names.write_names([a, b, b, talent, item])
        self.assertEqual(len(records), 4)
        self.assertEqual(sync.call_count, 1)
        self.assertEqual(sync.call_args.kwargs, {'branch': 'retail', 'spell_ids': {930001, 930002, 930005}, 'apply': True})
        old.refresh_from_db(); node.refresh_from_db()
        self.assertEqual((old.name, old.name_zh, old.icon, old.localization_evidence), ('Manual', '人工中文', 'manual_icon', '人工证据'))
        self.assertEqual(node.reference_aliases, [930003])
        self.assertEqual(WowSpellSnapshot.objects.get(branch='wow', locale='zhCN', spell_id=930001).display_data['default']['name_zh'], '人工中文')
        self.assertEqual(WowSpellSnapshot.objects.get(branch='wow', locale='zhCN', spell_id=930002).display_data['default']['name_zh'], '来源中文')
        self.assertEqual(WowTalentNodeMetadata.all_objects.get(name_kind='item', reference_id=930002).name_zh, '物品中文')
        before = list(WowTalentNodeMetadata.all_objects.order_by('pk').values())
        displays = list(WowSpellSnapshot.objects.order_by('pk').values())
        with patch.object(display, 'sync_spell_display', wraps=display.sync_spell_display) as sync:
            names.write_names([a, b, talent, item])
        self.assertEqual(sync.call_count, 1, 'retry still verifies final display rather than silently skipping it')
        self.assertEqual(list(WowTalentNodeMetadata.all_objects.order_by('pk').values()), before)
        self.assertEqual(list(WowSpellSnapshot.objects.order_by('pk').values()), displays)

    def test_same_spell_id_stays_separate_across_active_branches(self):
        WowTalentVersion.objects.update_or_create(key='ptr', defaults={
            'major_version': '12.1.5', 'branch': 'ptr', 'is_active': True, 'status': 'active',
            'source_dir': '', 'current_build': '12.1.5.123'})
        with patch.object(display, 'sync_spell_display', wraps=display.sync_spell_display) as sync:
            names.write_names([self.term(930031), {**self.term(930031), 'game_version': 'ptr', 'name_zh': '测试服中文'}])
        self.assertEqual(sync.call_count, 2)
        self.assertEqual({call.kwargs['branch'] for call in sync.call_args_list}, {'retail', 'ptr'})
        self.assertEqual(WowSpellSnapshot.objects.get(branch='wow', locale='zhCN', spell_id=930031).display_data['default']['name_zh'], '来源中文')
        self.assertEqual(WowSpellSnapshot.objects.get(branch='wowxptr', locale='zhCN', spell_id=930031).display_data['default']['name_zh'], '测试服中文')

    def test_invalid_batch_is_validated_before_any_write(self):
        with self.assertRaises(ValidationError):
            names.write_names([self.term(930011), {**self.term(930012), 'object_id': False}])
        self.assertFalse(WowTalentNodeMetadata.all_objects.filter(reference_id__in=[930011, 930012]).exists())

    def test_display_failure_rolls_back_names_and_real_display_writes(self):
        original = display.sync_spell_display
        def fail_after_sync(**kwargs):
            original(**kwargs)
            raise RuntimeError('display publication failed')
        with patch.object(display, 'sync_spell_display', side_effect=fail_after_sync):
            with self.assertRaisesMessage(RuntimeError, 'display publication failed'):
                names.write_names([self.term(930021)])
        self.assertFalse(WowTalentNodeMetadata.all_objects.filter(reference_id=930021).exists())
        self.assertFalse(WowSpellSnapshot.objects.filter(spell_id=930021).exists())
