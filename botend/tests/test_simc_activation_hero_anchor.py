from types import SimpleNamespace
from unittest import TestCase
from botend.services import simc_skill_activation_context as context
from botend.tests.test_simc_skill_activation_context import trait


class HeroAnchorTests(TestCase):
    def test_independent_ordinary_evidence_plans_only_matching_selector(self):
        unlock, hero, target = trait(10), trait(20, subtree=7), trait(30)
        anchors = [SimpleNamespace(tree_type='hero_anchor', node_id=90)]
        ordinary = [{'name': 'ordinary', 'selected_talents': [hero]}]
        def load(health, name):
            return {'selected_trait_ids': [20, 90]}
        mapping = context.prove_hero_anchor_selectors(ordinary, load, anchors)
        self.assertEqual(mapping, {7: 90})
        plan = context.plan_activation_context_pairs(
            [unlock, hero, target], candidates=[{'trait_entry_id': 10, 'replacement_spell_id': 300}],
            contexts={10: {'selected_talents': [unlock, hero], 'actor': {
                'selected_trait_ids': [10, 20, 90], 'actions': [{'spell_id': 300}]}}},
            hero_anchor_selectors=mapping)
        pair = next(p for p in plan['pairs'] if p['talent'].node_id == 30)
        self.assertEqual(pair['expected_reference_trait_entry_ids'], [10, 20, 90])
        def pair_load(health, name):
            return {'selected_trait_ids': [10, 20, 90] + ([30] if name == pair['selected_name'] else []), 'actions': []}
        context.materialize_activation_pair(pair, pair_load)
        for bad in (91, 999):
            with self.assertRaises(ValueError):
                context.materialize_activation_pair(pair, lambda h, n: {
                    **pair_load(h, n), 'selected_trait_ids': pair_load(h, n)['selected_trait_ids'] + [bad]})
        self.assertEqual(context.prove_hero_anchor_selectors(ordinary, load, []), {})
        self.assertEqual(context.prove_hero_anchor_selectors(ordinary, lambda h,n: {
            'selected_trait_ids': [20,90] if h == 100 else [20]}, anchors), {})
        self.assertEqual(context.prove_hero_anchor_selectors(ordinary, lambda h,n: {
            'selected_trait_ids': [20,90,999]}, anchors), {})
        conflicting = ordinary + [{'name': 'other', 'selected_talents': [trait(21, subtree=8)]}]
        self.assertEqual(context.prove_hero_anchor_selectors(conflicting, lambda h,n: {
            'selected_trait_ids': [20 if n == 'ordinary' else 21,90]}, anchors), {})
