from types import SimpleNamespace
from unittest import TestCase
from botend.services.simc_skill_activation_context import discover_replacement_candidates
from botend.services.simc_skill_activation_context import plan_activation_context_pairs
from botend.services.simc_skill_activation_context import validate_activation_pair
from botend.services.simc_skill_activation_context import materialize_activation_pair
from botend.services.simc_skill_damage import SimcSkillDamageSnapshotService
from botend.services.simc_skill_damage import plan_unique_talent_actor_configs
from botend.services import simc_skill_activation_context as context


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


class CanonicalActivationConfigTests(TestCase):
    def test_reuses_materialized_reference_and_context_without_scaffold(self):
        for context_replaces_scaffold in (False, True):
            with self.subTest(context_replaces_scaffold=context_replaces_scaffold):
                scaffold = trait(1, choice=100)
                replacement = trait(2, choice=100)
                unlock, target = trait(10), trait(30)
                talents = [replacement, unlock, target]
                prerequisites = {30: [replacement]}
                if context_replaces_scaffold:
                    prerequisites[10] = [replacement]
                ordinary = plan_unique_talent_actor_configs(
                    talents, scaffold_talents=[scaffold],
                    talent_prerequisites=prerequisites,
                )
                actors = {a['name']: {
                    'selected_trait_ids': [t.node_id for t in a['selected_talents']],
                    'actions': [{'spell_id': 300}] if unlock in a['selected_talents'] else [],
                } for a in ordinary['actors']}
                service = object.__new__(SimcSkillDamageSnapshotService)
                service._native_talent_catalog = [{
                    'trait_entry_id': 10, 'spell_id': 200,
                    'effects': [{'type': 6, 'subtype': 332, 'misc1': 100, 'value': 300}],
                }]
                plan = service._activation_context_plan(
                    talents, [scaffold], prerequisites, ordinary,
                    SimpleNamespace(load=lambda health, name: actors.get(name)),
                )
                if context_replaces_scaffold:
                    pair = next(p for p in plan['pairs'] if p['talent'] is target)
                    configs = {a['name']: a['selected_talents'] for a in [*ordinary['actors'], *plan['actors']]}
                    self.assertEqual({t.node_id for t in configs[pair['reference_name']]}, {2, 10})
                    self.assertEqual({t.node_id for t in configs[pair['selected_name']]}, {2, 10, 30})
                else:
                    self.assertTrue(any(s['talent_entry_id'] == 30 for s in plan['skipped']))


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


class MaskReplacementSelectorTests(TestCase):
    def test_unspecified_original_does_not_erase_valid_replacement_or_fake_readiness(self):
        unlock=trait(10)
        catalog={'talent_catalog':[{'trait_entry_id':10,'spell_id':20,'effects':[
            {'type':6,'subtype':332,'misc1':0,'misc2':3,'value':30,'flags':[0,0,16,0],'id':50,'index':7}]}]}
        candidates=discover_replacement_candidates(catalog,[unlock])
        self.assertEqual(len(candidates),1)
        self.assertEqual(candidates[0]['original_spell_id'],0)
        self.assertEqual(candidates[0]['replacement_spell_id'],30)
        plan=plan_activation_context_pairs([unlock],candidates=candidates,contexts={10:{
            'selected_talents':[unlock],'actor':{'selected_trait_ids':[10],'actions':[]}}})
        self.assertEqual(plan['context_count'],0)
        self.assertEqual(plan['pairs'],[])

    def test_malformed_or_negative_original_is_still_rejected(self):
        for original in (-1, None, '0', True):
            with self.subTest(original=original),self.assertRaisesRegex(ValueError,'Invalid DBC'):
                discover_replacement_candidates({'talent_catalog':[{
                    'trait_entry_id':10,'spell_id':20,'effects':[{
                        'type':6,'subtype':332,'misc1':original,'value':30}]}]},[trait(10)])


class ActivationRuntimeGateTests(TestCase):
    def pair(self):
        unlock, target = trait(10), trait(30)
        plan = plan_activation_context_pairs(
            [unlock, target], candidates=[{'trait_entry_id':10, 'replacement_spell_id':300}],
            contexts={10:{'selected_talents':[unlock], 'actor':{
                'selected_trait_ids':[10], 'actions':[{'spell_id':300}]}}},
        )
        return plan['pairs'][0]

    def materialize(self, ref_high, selected_high, ref_low=None, selected_low=None):
        sets = {(100,'reference'):ref_high, (100,'selected'):selected_high,
                (34,'reference'):ref_high if ref_low is None else ref_low,
                (34,'selected'):selected_high if selected_low is None else selected_low}
        pair = self.pair()
        def load(health, name):
            kind='reference' if name==pair['reference_name'] else 'selected'
            return {'selected_trait_ids':sets[health,kind], 'actions':[{'spell_id':300}]}
        return materialize_activation_pair(pair,load)

    def test_missing_fixed_context_on_both_sides_is_not_a_valid_pair(self):
        with self.assertRaisesRegex(ValueError, 'context'):
            self.materialize([99], [99,30])

    def test_unplanned_shared_runtime_traits_are_not_silently_accepted(self):
        with self.assertRaisesRegex(ValueError, 'selection'):
            self.materialize([10,99], [10,99,30])

    def test_target_health_does_not_change_fixed_talent_selection(self):
        with self.assertRaisesRegex(ValueError, 'selection'):
            self.materialize([10], [10,30], [10,99], [10,99,30])

    def test_exact_pair_and_declared_implicit_baseline_traits_remain_valid(self):
        self.assertEqual(self.materialize([10],[10,30])['high']['selected_trait_ids'],[10,30])
        unlock, target=trait(10),trait(30)
        plan=plan_activation_context_pairs([unlock,target],
            candidates=[{'trait_entry_id':10,'replacement_spell_id':300}],
            contexts={10:{'selected_talents':[unlock], 'actor':{
                'selected_trait_ids':[10,99], 'actions':[{'spell_id':300}]}}},
            implicit_trait_entry_ids=[99])
        pair=plan['pairs'][0]
        result=materialize_activation_pair(pair, lambda health,name:{
            'selected_trait_ids':[10,99]+([30] if name==pair['selected_name'] else []),
            'actions':[{'spell_id':300}]})
        self.assertEqual(result['reference_low']['selected_trait_ids'],[10,99])
