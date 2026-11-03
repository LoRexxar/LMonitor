"""独立原始证据必须能发现未声明状态、数值残留及不完整的上表。"""
from unittest import TestCase

from scripts.audit_simc_scope_completeness import inspect_actor, all_racial_sources, trait_authority


class ScopeCompletenessTests(TestCase):
    def test_granted_specialization_extends_restricted_nodes_without_restricting_universal_nodes(self):
        traits = [
            {'trait_entry_id': 1, 'class_id': 11, 'tree_index': 1, 'subtree_id': 0,
             'spec_ids': [102, 103, 104], 'starter_spec_ids': [105], 'spell_id': 900101},
            {'trait_entry_id': 2, 'class_id': 11, 'tree_index': 1, 'subtree_id': 0,
             'spec_ids': [], 'starter_spec_ids': [103], 'spell_id': 900103}]
        _, spells, _ = self.racial_fixture()
        spells[900103] = {'class_family': 0, 'effects': []}
        _, _, owners = trait_authority(traits, spells)
        self.assertEqual(owners[11, 900101]['specs'], {102, 103, 104, 105})
        self.assertTrue(owners[11, 900103]['shared'])

    def test_raw_hero_selection_overrules_missing_or_wrong_node_specialization(self):
        traits = [
            {'trait_entry_id': 1, 'class_id': 12, 'tree_index': 4, 'subtree_id': 126, 'spec_ids': [1480], 'spell_id': 0},
            {'trait_entry_id': 2, 'class_id': 12, 'tree_index': 3, 'subtree_id': 126, 'spec_ids': [577], 'spell_id': 900101}]
        _, spells, _ = self.racial_fixture()
        selections, entries, owners = trait_authority(traits, spells)
        self.assertEqual(selections[12, 126], {1480})
        self.assertEqual(owners[12, 900101], {'specs': {1480}, 'shared': False})
        self.assertEqual(owners[12, 900102], {'specs': {1480}, 'shared': False})

    def test_registered_source_missing_entire_upper_row_is_detected(self):
        actor, spells = self.fixture()
        actor.update(scope_inventory_version=4, probe_context_inventory_version=1,
                     native_registration_inventory_version=1, native_passive_spell_ids=[900001],
                     race_mask='1', class_mask=1, racial_policy='exclude_all', excluded_racial_spell_ids=[900999],
                     normalized_scope_effects=[{'spell_id': 900001, 'effect_index': 1, 'actual_base_value': 0}])
        errors = inspect_actor(actor, spells, 4, racial_catalog=[{'spell_id': 900999}], native_sources=[900001])['错误']
        self.assertIn('上表遗漏原生实际注册的整项来源', [e['问题'] for e in errors])

    def test_native_racial_registration_without_dbc_trigger_edge_is_discovered(self):
        actor, spells, catalog = self.racial_fixture()
        spells[900103] = {'class_family': 0, 'effects': [dict(spells[900102]['effects'][0])]}
        errors = inspect_actor(actor, spells, 4, racial_catalog=catalog, native_racials=[900103])['错误']
        self.assertIn('种族排除清单与原始全集不一致', [e['问题'] for e in errors])
        self.assertIn('种族分量未完整归零回读', [e['问题'] for e in errors])

    def racial_fixture(self):
        spells = {900101: {'class_family': 0, 'effects': [
            {'index': 1, 'type': 6, 'subtype': 0, 'trigger': 900102}]},
            900102: {'class_family': 9, 'effects': [
            {'index': 1, 'type': 6, 'subtype': 0, 'trigger': 0}]}}
        catalog = [{'spell_id': 900101}]
        for spell in spells.values():
            for effect in spell['effects']:
                effect.update(flags=[0, 0, 0, 0], misc1=0, misc2=0, value=0, mastery_coefficient=0)
        actor = {'normalized_scope_effects': [], 'reviewed_global_effects': [], 'actions': [],
                 'racial_policy': 'exclude_all', 'excluded_racial_spell_ids': [900101, 900102],
                 'normalized_racial_effects': [dict(spell_id=sid, effect_index=1,
                     actual_base_value=0, actual_ap_coefficient=0, actual_sp_coefficient=0,
                     actual_scale_coefficient=0) for sid in spells]}
        return actor, spells, catalog

    def test_all_races_and_foreign_family_trigger_effects_are_independently_excluded(self):
        actor, spells, catalog = self.racial_fixture()
        self.assertEqual(all_racial_sources(spells, catalog), {900101, 900102})
        self.assertEqual(inspect_actor(actor, spells, 4, racial_catalog=catalog)['错误'], [])
        actor['excluded_racial_spell_ids'] = [900101]
        self.assertIn('种族排除清单与原始全集不一致',
                      [e['问题'] for e in inspect_actor(actor, spells, 4, racial_catalog=catalog)['错误']])

    def test_racial_scaling_and_skill_condition_leaks_are_rejected(self):
        actor, spells, catalog = self.racial_fixture()
        actor['normalized_racial_effects'][1]['actual_ap_coefficient'] = 0.25
        actor['actions'] = [{'token': '普通技能', 'activation_conditions': [{'spell_id': 900102}]}]
        errors = [e['问题'] for e in inspect_actor(actor, spells, 4, racial_catalog=catalog)['错误']]
        self.assertIn('种族分量未完整归零回读', errors)
        self.assertIn('种族能力仍进入技能或条件', errors)

    def fixture(self):
        spell = {'class_family': 4, 'effects': [{'type': 174, 'subtype': 531,
                 'index': 1, 'misc1': 0, 'flags': [0, 0, 0, 0], 'value': 25, 'mastery_coefficient': 0}]}
        actor = {'normalized_scope_effects': [], 'reviewed_global_effects': [], 'actions': []}
        return actor, {900001: spell}

    def test_undeclared_global_is_discovered_from_raw_universe(self):
        actor, spells = self.fixture()
        self.assertEqual(inspect_actor(actor, spells, 4)['错误'][0]['问题'], '独立发现未归一化分量')

    def test_zero_readback_does_not_hide_an_incomplete_upper_table(self):
        actor, spells = self.fixture()
        actor['normalized_scope_effects'] = [{'spell_id': 900001, 'effect_index': 1, 'actual_base_value': 0}]
        actor['reviewed_global_effects'] = [{'source_spell_ids': [900001], 'global_components': []}]
        self.assertEqual(inspect_actor(actor, spells, 4)['错误'][0]['问题'], '上表来源缺少公共分量')

    def test_actual_factor_is_checked_even_when_zero_readback_claims_success(self):
        actor, spells = self.fixture()
        actor['normalized_scope_effects'] = [{'spell_id': 900001, 'effect_index': 1, 'actual_base_value': 0}]
        actor['actions'] = [{'nested_probe': {'runtime_factor_sources': [
            {'source_spell_id': 900001, 'effect_index': 1, 'factor': 1.25}]}}]
        self.assertEqual(inspect_actor(actor, spells, 4)['错误'][0]['问题'], '公共分量仍参与原生运算')

    def test_global_activation_is_allowed_but_ordinary_damage_dimension_is_rejected(self):
        actor, spells = self.fixture()
        actor['normalized_scope_effects'] = [{'spell_id': 900001, 'effect_index': 1, 'actual_base_value': 0}]
        actor['actions'] = [{'token': 'replacement', 'activation_conditions': [{'spell_id': 900001}]}]
        self.assertEqual(inspect_actor(actor, spells, 4)['错误'], [])
        actor['actions'][0]['scenarios'] = [{'buffs': [{'spell_id': 900001}]}]
        self.assertEqual(inspect_actor(actor, spells, 4)['错误'][0]['问题'], '原生普通场景仍枚举全局状态')
