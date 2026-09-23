"""Exact-build icon identity for talent entries, not physical slots."""
import csv
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from botend.models import WowTalentNodeMetadata, WowTalentVersion
from botend.portal.talent_simulator import _decorate_render_model
from botend.wow.talents.icon_source import resolve_talent_entry_icon
from botend.wow.talents.metadata import TalentMetadataProvider
from botend.wow.talents.view_model import build_talent_view_model


class TalentEntryIconSourceTests(SimpleTestCase):
    def test_standard_spell_icon_wins_over_active_state_icon(self):
        entries = {112112: 117117}
        definitions = {117117: {'OverrideIcon': '0', 'VisibleSpellID': '386164', 'SpellID': '386164'}}
        spells = {386164: {'SpellIconFileDataID': '132349', 'ActiveIconFileDataID': '136116'}}
        icons = {132349: 'ability_warrior_offensivestance', 136116: 'spell_nature_wispsplode'}
        self.assertEqual(resolve_talent_entry_icon(112112, entries, definitions, spells, icons),
                         (132349, 'ability_warrior_offensivestance'))

    def test_choice_entries_use_their_own_definition_override(self):
        entries = {117528: 1, 123405: 2}
        definitions = {
            1: {'OverrideIcon': '0', 'VisibleSpellID': '111', 'SpellID': '111'},
            2: {'OverrideIcon': '1526594', 'VisibleSpellID': '222', 'SpellID': '222'},
        }
        spells = {111: {'SpellIconFileDataID': '132349', 'ActiveIconFileDataID': '0'}}
        icons = {132349: 'ability_warrior_offensivestance', 1526594: 'inv_misc_scales_basilliskorange'}
        self.assertEqual(resolve_talent_entry_icon(117528, entries, definitions, spells, icons),
                         (132349, 'ability_warrior_offensivestance'))
        self.assertEqual(resolve_talent_entry_icon(123405, entries, definitions, spells, icons),
                         (1526594, 'inv_misc_scales_basilliskorange'))

    def test_unresolved_standard_file_id_never_substitutes_active_icon(self):
        entries = {112112: 117117}
        definitions = {117117: {'OverrideIcon': '0', 'SpellID': '386164'}}
        spells = {386164: {'SpellIconFileDataID': '132349', 'ActiveIconFileDataID': '136116'}}
        self.assertEqual(resolve_talent_entry_icon(112112, entries, definitions, spells,
                                                    {136116: 'spell_nature_wispsplode'}),
                         (132349, ''))


class TalentIconReconciliationTests(TestCase):
    def test_unsourced_choice_entry_keeps_code_slot_without_fake_questionmark_icon(self):
        version = WowTalentVersion.objects.create(
            key='icon-choice-test', current_build='12.1.0.test', branch='retail',
        )
        for entry, spell, name, icon in (
            (124883, 124883, '', ''),
            (124884, 1271431, 'Amplified Rush', 'ability_monk_rushingjadewind'),
        ):
            WowTalentNodeMetadata.objects.create(
                talent_version=version, class_name='Monk', spec_name='Mistweaver',
                tree_type='spec', source='db2_backfill', node_id=entry,
                talent_id=101103, spell_id=spell, name=name, icon=icon,
                row=1200, column=3600,
            )
        nodes = TalentMetadataProvider(talent_version=version).get_full_tree_nodes('Monk', 'Mistweaver')
        model = _decorate_render_model(build_talent_view_model(
            nodes, class_name='Monk', spec_name='Mistweaver')['render_model'])
        option_map = {o['node_id']: o for t in model['trees'] for n in t['nodes']
                      for o in n.get('choice_options') or []}
        self.assertEqual(set(option_map), {124883, 124884})
        self.assertTrue(option_map[124883]['is_unresolved'])
        self.assertEqual(option_map[124883]['icon_url'], '')
        self.assertFalse(option_map[124884]['is_unresolved'])
        self.assertEqual(option_map[124884]['icon'], 'ability_monk_rushingjadewind')

    def test_dry_run_then_apply_corrects_each_choice_entry_only_at_matching_build(self):
        version = WowTalentVersion.objects.create(
            key='icon-test', current_build='12.1.0.test', branch='retail',
        )
        for entry, spell in ((117528, 111), (123405, 222)):
            WowTalentNodeMetadata.objects.create(
                talent_version=version, class_name='Warrior', spec_name='Protection',
                tree_type='class', source='db2_backfill', node_id=entry,
                talent_id=94931, spell_id=spell, icon='spell_nature_wispsplode',
            )
        tables = {
            'TraitNodeEntry': [
                {'ID': '117528', 'TraitDefinitionID': '1'},
                {'ID': '123405', 'TraitDefinitionID': '2'},
            ],
            'TraitNodeXTraitNodeEntry': [
                {'TraitNodeEntryID': '117528', 'TraitNodeID': '94931'},
                {'TraitNodeEntryID': '123405', 'TraitNodeID': '94931'},
            ],
            'TraitDefinition_enUS': [
                {'ID': '1', 'OverrideIcon': '0', 'VisibleSpellID': '111', 'SpellID': '111'},
                {'ID': '2', 'OverrideIcon': '1526594', 'VisibleSpellID': '222', 'SpellID': '222'},
            ],
            'SpellMisc': [
                {'SpellID': '111', 'SpellIconFileDataID': '132349',
                 'ActiveIconFileDataID': '136116'},
            ],
            'file_data_icon_cache': [
                {'FileDataID': '132349', 'IconName': 'ability_warrior_offensivestance'},
                {'FileDataID': '1526594', 'IconName': 'inv_misc_scales_basilliskorange'},
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / version.current_build
            root.mkdir()
            for name, rows in tables.items():
                with (root / f'{name}.csv').open('w', newline='') as stream:
                    writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
                    writer.writeheader()
                    writer.writerows(rows)
            args = dict(version_key=version.key, build=version.current_build, dump_dir=str(root))
            call_command('reconcile_talent_icons', **args)
            self.assertEqual(set(WowTalentNodeMetadata.objects.filter(
                talent_version=version).values_list('icon', flat=True)), {'spell_nature_wispsplode'})
            with self.assertRaises(CommandError):
                call_command('reconcile_talent_icons', **{**args, 'build': 'wrong'}, apply=True)
            call_command('reconcile_talent_icons', **args, apply=True)
            self.assertEqual(dict(WowTalentNodeMetadata.objects.filter(
                talent_version=version).values_list('node_id', 'icon')),
                {117528: 'ability_warrior_offensivestance',
                 123405: 'inv_misc_scales_basilliskorange'})
