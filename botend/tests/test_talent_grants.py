"""Starter grants are physical entry facts, not root/spell guesses."""
from django.test import SimpleTestCase, TestCase

from botend.models import WowTalentNodeMetadata, WowTalentVersion
from botend.portal.talent_simulator import _merge_nodes_for_simulator
from botend.wow.talents.parser import normalize_talent_payload
from botend.wow.talents.metadata import TalentMetadataProvider


class TalentStarterDisplayTests(SimpleTestCase):
    def test_empty_protection_tree_grants_two_entries_without_spending_points(self):
        raw = [
            {'tree_type': 'class', 'node_id': 112112, 'talent_id': 90261,
             'spell_id': 386164, 'flags': 8, 'parents': [], 'granted': True},
            {'tree_type': 'class', 'node_id': 112182, 'talent_id': 90325,
             'spell_id': 386196, 'flags': 8, 'parents': []},
            {'tree_type': 'class', 'node_id': 112187, 'talent_id': 90330,
             'spell_id': 386208, 'flags': 8, 'parents': [], 'granted': True},
        ]
        nodes = normalize_talent_payload(raw, 'Warrior', 'Protection')['nodes']
        merged = _merge_nodes_for_simulator(nodes, decoded_states={}, active_hero_subtree=0)
        by_entry = {n['node_id']: n for n in merged}
        self.assertEqual(
            [(by_entry[n]['points'], by_entry[n]['selected'], by_entry[n]['purchased'])
             for n in (112112, 112182, 112187)],
            [(1, True, False), (0, False, None), (1, True, False)],
        )

    def test_explicit_import_state_overrides_metadata_grant(self):
        raw = [{'tree_type': 'class', 'node_id': 112187, 'talent_id': 90330,
                'spell_id': 386208, 'flags': 8, 'parents': [],
                'granted': True, 'points': 1, 'selected': True, 'purchased': True}]
        nodes = normalize_talent_payload(raw, 'Warrior', 'Protection')['nodes']
        self.assertEqual(nodes[0]['purchased'], True)
        merged = _merge_nodes_for_simulator(nodes, decoded_states={
            'class:112187': {'points': 1, 'selected': True, 'purchased': True}
        }, active_hero_subtree=0)
        self.assertEqual((merged[0]['points'], merged[0]['purchased']), (1, True))


class TalentStarterSnapshotTests(TestCase):
    def test_versioned_entry_ids_not_spells_grant_matching_physical_nodes(self):
        version = WowTalentVersion.objects.create(
            key='grant-test', current_build='12.1.0.test', branch='retail',
            granted_entries_json={'build': '12.1.0.test', 'specs': {'73': [112112, 112187]}},
        )
        for entry, physical, spell in ((112112, 90261, 386164),
                                       (112182, 90325, 386196),
                                       (112187, 90330, 386208)):
            WowTalentNodeMetadata.objects.create(
                talent_version=version, class_name='Warrior', spec_name='Protection',
                tree_type='class', source='db2_backfill', node_id=entry,
                talent_id=physical, spell_id=spell, name=str(spell),
                row=900, column=2400 if entry != 112187 else 4800, flags=8,
            )
        provider = TalentMetadataProvider(talent_version=version)
        nodes = provider.get_full_tree_nodes('Warrior', 'Protection')
        by_id = {n['node_id']: n for n in nodes}
        self.assertEqual({entry for entry, n in by_id.items() if n['granted']},
                         {112112, 112187})
        parsed = normalize_talent_payload(nodes, 'Warrior', 'Protection')['nodes']
        self.assertEqual({n['node_id'] for n in parsed if n['points'] == 1 and n['purchased'] is False},
                         {112112, 112187})
        version.current_build = '12.1.0.next'
        version.save(update_fields=['current_build'])
        self.assertFalse(any(n['granted'] for n in TalentMetadataProvider(
            talent_version=version).get_full_tree_nodes('Warrior', 'Protection')))

    def test_grant_source_joins_spec_node_group_and_universal_conditions(self):
        from botend.wow.talents.grants import derive_granted_entries
        tables = {
            'TraitCond': [
                {'ID': '1', 'CondType': '2', 'GrantedRanks': '1', 'SpecSetID': '9'},
                {'ID': '2', 'CondType': '2', 'GrantedRanks': '1', 'SpecSetID': '0'},
                {'ID': '3', 'CondType': '1', 'GrantedRanks': '1', 'SpecSetID': '9'},
            ],
            'SpecSetMember': [{'SpecSet': '9', 'ChrSpecializationID': '73'}],
            'TraitNodeXTraitCond': [
                {'TraitCondID': '1', 'TraitNodeID': '90261'},
                {'TraitCondID': '2', 'TraitNodeID': '71933'},
                {'TraitCondID': '3', 'TraitNodeID': '90325'},
            ],
            'TraitNodeGroupXTraitCond': [{'TraitCondID': '1', 'TraitNodeGroupID': '10'}],
            'TraitNodeGroupXTraitNode': [{'TraitNodeGroupID': '10', 'TraitNodeID': '90330'}],
            'TraitNodeXTraitNodeEntry': [
                {'TraitNodeID': '90261', 'TraitNodeEntryID': '112112'},
                {'TraitNodeID': '90330', 'TraitNodeEntryID': '112187'},
                {'TraitNodeID': '71933', 'TraitNodeEntryID': '91441'},
            ],
        }
        metadata = [
            {'class_name': 'Warrior', 'spec_name': 'Protection', 'talent_id': 90261, 'node_id': 112112},
            {'class_name': 'Warrior', 'spec_name': 'Protection', 'talent_id': 90330, 'node_id': 112187},
            {'class_name': 'Warrior', 'spec_name': 'Fury', 'talent_id': 90330, 'node_id': 112187},
            {'class_name': 'Warlock', 'spec_name': 'Affliction', 'talent_id': 71933, 'node_id': 91441},
        ]
        got = derive_granted_entries(tables, metadata)
        self.assertEqual(got['73'], [112112, 112187])
        self.assertNotIn('72', got)
        self.assertEqual(got['265'], [91441])

    def test_same_slot_same_spell_prefers_current_specs_granted_entry(self):
        version = WowTalentVersion.objects.create(
            key='stance-test', current_build='12.1.0.test', branch='retail',
            granted_entries_json={'build': '12.1.0.test', 'specs': {'71': [112184, 114643]}},
        )
        for entry, physical, spell, column in (
            (112112, 90261, 386164, 2400), (112184, 90327, 386164, 2404),
            (112187, 90330, 386208, 4800), (114643, 92537, 386208, 4804),
        ):
            WowTalentNodeMetadata.objects.create(
                talent_version=version, class_name='Warrior', spec_name='Arms',
                tree_type='class', source='db2_backfill', node_id=entry,
                talent_id=physical, spell_id=spell, row=900, column=column,
            )
        nodes = TalentMetadataProvider(talent_version=version).get_full_tree_nodes('Warrior', 'Arms')
        self.assertEqual({n['node_id'] for n in nodes}, {112184, 114643})
        self.assertTrue(all(n['granted'] for n in nodes))
        self.assertEqual({entry for n in nodes for entry in n['node_aliases']},
                         {112112, 112184, 112187, 114643})
