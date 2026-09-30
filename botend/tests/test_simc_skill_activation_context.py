from types import SimpleNamespace
from unittest import TestCase

from botend.services.simc_skill_activation_context import (
    discover_replacement_candidates, plan_activation_context_pairs,
    validate_activation_pair, materialize_activation_pair,
)


def trait(entry, *, choice=None, subtree=None):
    return SimpleNamespace(pk=entry, node_id=entry, tree_type='hero' if subtree else 'spec',
                           max_points=1, talent_id=choice, db2_subtree_id=subtree)


class ActivationContextTests(TestCase):
    def test_dbc_direction_runtime_gate_and_exact_marginal_pair(self):
        unlock, driver, target, unrelated = map(trait, (10, 20, 30, 40))
        catalog = {'talent_catalog': [
            {'trait_entry_id': 20, 'spell_id': 200, 'effects': [
                {'type': 6, 'subtype': 332, 'misc1': 100, 'value': 300, 'trigger': 999,
                 'index': 2, 'id': 22}], 'dbc_scope_effects': []},
            {'trait_entry_id': 10, 'spell_id': 110, 'effects': [],
             'dbc_scope_effects': [{'affected_spells': [{'spell_id': 200}]}]},
            {'trait_entry_id': 40, 'spell_id': 400, 'effects': [], 'dbc_scope_effects': []},
        ]}
        candidates = discover_replacement_candidates(catalog, [unlock, driver, target, unrelated])
        self.assertEqual({c['trait_entry_id'] for c in candidates}, {10, 20})
        self.assertEqual({c['replacement_spell_id'] for c in candidates}, {300})
        plan = plan_activation_context_pairs(
            [unlock, driver, target, unrelated], candidates=candidates,
            contexts={10: {'selected_talents': [unlock], 'actor': {
                'selected_trait_ids': [10], 'actions': [{'spell_id': 300}]}},
                20: {'selected_talents': [driver], 'actor': {'actions': []}}},
        )
        pair = next(p for p in plan['pairs'] if p['talent'].node_id == 30)
        actors = {a['name']: a for a in plan['actors']}
        self.assertEqual([t.node_id for t in actors[pair['reference_name']]['selected_talents']], [10])
        self.assertEqual({t.node_id for t in actors[pair['selected_name']]['selected_talents']}, {10, 30})
        self.assertEqual(pair['activation_context']['trait_entry_ids'], [10])
        self.assertEqual(pair['activation_context']['action_spell_ids'], [300])
        self.assertEqual(len(plan['pairs']), 3)
        validate_activation_pair({'selected_trait_ids': [10]}, {'selected_trait_ids': [10, 30]}, 30)
        with self.assertRaisesRegex(ValueError, 'marginal'):
            validate_activation_pair({'selected_trait_ids': [10]}, {'selected_trait_ids': [10, 30, 40]}, 30)
        with self.assertRaisesRegex(ValueError, 'budget'):
            plan_activation_context_pairs([unlock, target, unrelated], candidates=candidates,
                contexts={10: {'selected_talents': [unlock], 'actor': {
                    'selected_trait_ids': [10], 'actions': [{'spell_id': 300}]}}}, max_pairs=1)

    def test_projection_keeps_cast_children_and_explicit_context(self):
        target = trait(30)
        reference = {'selected_trait_ids': [10], 'actions': [
            {'spell_id': 300}, {'spell_id': 301, 'reporting_root_spell_id': 300},
            {'spell_id': 999}]}
        selected = {**reference, 'selected_trait_ids': [10, 30]}
        pair = {'talent': target, 'reference_name': 'ref', 'selected_name': 'sel',
                'activation_context': {'trait_entry_ids': [10], 'action_spell_ids': [300]}}
        variant = materialize_activation_pair(pair, lambda health, name: reference if name == 'ref' else selected)
        self.assertEqual([a['spell_id'] for a in variant['high']['actions']], [300, 301])
        self.assertEqual(variant['talent']['node_id'], 30)
        self.assertEqual(variant['activation_context']['trait_entry_ids'], [10])
        self.assertEqual(len(reference['actions']), 3)
        with self.assertRaisesRegex(ValueError, 'Missing'):
            materialize_activation_pair(pair, lambda *args: None)

    def test_conflicts_and_target_in_context_are_not_probed(self):
        unlock, target = trait(10, choice=5), trait(30, choice=5)
        plan = plan_activation_context_pairs([unlock, target], candidates=[{
            'trait_entry_id': 10, 'replacement_spell_id': 300, 'driver_spell_id': 200}],
            contexts={10: {'selected_talents': [unlock], 'actor': {
                'selected_trait_ids': [10], 'actions': [{'spell_id': 300}]}}})
        self.assertEqual(plan['pairs'], [])
        self.assertEqual(plan['skipped'][0]['reason'], 'incompatible_choice_or_subtree')
