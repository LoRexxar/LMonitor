"""State-only classification and immediate nested-graph release contracts."""
import copy
import gc
import tempfile
import weakref
from types import SimpleNamespace
from unittest import TestCase, mock

from botend.services import simc_skill_damage as damage
from botend.tests import test_simc_variant_metadata_budget as budget
from botend.tests.test_simc_variant_metadata_budget import talent, OldProvider
from botend.tests.test_simc_skill_scenario_indexing import action, amount, scenario, variant


class Graph(dict):
    pass


class StateMetadataLifecycleTests(TestCase):
    make_variants = budget.VariantMetadataBudgetTests.make_variants

    def test_excluded_classification_matches_full_without_graph_decode(self):
        with tempfile.TemporaryDirectory() as root:
            spool, _, variants = self.make_variants(root, [talent(10), talent(20)])
            # Freeze real state facts through store_many, not a derived catalog.
            stored = [(key, spool.load(*key)) for key in spool._paths]
            spool = damage._CanonicalActorSpool(root + '/states')
            variants.actor_spool = spool
            for key, row in stored:
                row.update(global_damage_states=[{
                    'token': 'buff.reviewed', 'scope': 'self', 'spell_id': 700,
                    'name': 'Reviewed state', 'partial_state': True, 'available': True,
                    'excluded_before_probe': True, 'dbc_base_multiplier': 1.1,
                    'scope_basis': 'reviewed_dbc_native_effect_scope',
                }], reviewed_global_effects=[{
                    'source_spell_ids': [700], 'class': 'test', 'spec': 'test',
                    'partial_state': True, 'local_components': [{'effect_index': 2}],
                    'local_skill_bindings': [{'skill_spell_ids': [500]}],
                    'local_scope_evidence': 'reviewed fixture',
                }])
                spool.store_many(key[0], [row])
            full = list(variants)
            base = spool.load(100, 'skill_damage_base')
            before = copy.deepcopy((base, full))
            expected = damage.classify_global_skill_effects(base, base, full)
            self.assertTrue(expected)
            with mock.patch.object(spool, 'load', side_effect=AssertionError('state pass decoded graph')):
                self.assertEqual(damage.classify_global_skill_effects(base, base, variants), expected)
            self.assertEqual(damage.classify_global_skill_effects(base, base, OldProvider(full)), expected)
            self.assertEqual((base, full), before)

    def test_state_manifest_is_exact_detached_and_releases_nested_graphs(self):
        with tempfile.TemporaryDirectory() as root:
            spool = damage._CanonicalActorSpool(root)
            values = Graph(amount())
            source_action = Graph(action(scenarios=[scenario(0, values)]))
            refs = [weakref.ref(source_action), weakref.ref(values)]
            facts = dict(name='physical', **{'class': 'test'}, spec='test', specialization='test',
                         global_damage_policy='exclude_before_probe',
                         global_damage_states=[{'token': 'buff.x', 'spell_id': 7}],
                         reviewed_global_effects=[{'source_spell_ids': [7]}])
            source = {**copy.deepcopy(facts), 'actions': [source_action],
                      'baseline': values, 'scenarios': [values], 'selected_trait_ids': [10]}
            spool.store_many(100, [source])
            light = spool.load_state_metadata(100, 'physical')
            self.assertEqual(light, facts)
            light['global_damage_states'][0]['spell_id'] = -1
            self.assertEqual(spool.load_state_metadata(100, 'physical'), facts)
            self.assertIsNone(spool.load_state_metadata(34, 'physical'))
            del source, source_action, values
            gc.collect()
            self.assertTrue(all(ref() is None for ref in refs))

    def test_mixed_policy_and_partial_health_reload_whole_pair(self):
        for role in ('high', 'low', 'reference_high', 'reference_low', 'partial'):
            partial = role == 'partial'
            with self.subTest(role=role), tempfile.TemporaryDirectory() as root:
                spool, plan, variants = self.make_variants(root, [talent(10)])
                selected = plan['aliases']['skill_damage_talent_10_trait_10']['canonical_name']
                if partial:
                    del spool._paths[(34, selected)]
                else:
                    logical = ('skill_damage_reference_10_trait_10' if role.startswith('reference')
                               else 'skill_damage_talent_10_trait_10')
                    key = (34 if role.endswith('low') else 100, plan['aliases'][logical]['canonical_name'])
                    stored = [(pair, spool.load(*pair)) for pair in spool._paths]
                    spool = damage._CanonicalActorSpool(root + '/mixed')
                    variants.actor_spool = spool
                    for pair, row in stored:
                        if pair == key:
                            row['global_damage_policy'] = 'legacy'
                        spool.store_many(pair[0], [row])
                full = list(variants)
                with mock.patch.object(spool, 'load', wraps=spool.load) as load:
                    self.assertEqual(list(variants.iter_state_metadata()), full)
                self.assertEqual(load.call_count, 2 if partial else 3)
                self.assertIn('actions', list(variants.iter_state_metadata())[0]['high'])
                self.assertEqual(damage.classify_global_skill_effects({}, {}, variants),
                                 damage.classify_global_skill_effects({}, {}, full))

    def test_presence_alias_selection_order_and_provider_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            spool, plan, variants = self.make_variants(root, [talent(10), talent(20)])
            full = list(variants)
            self.assertEqual([r['talent'] for r in variants.iter_state_metadata()], [r['talent'] for r in full])
            old = SimpleNamespace(load=spool.load)
            legacy = damage._SpoolBackedTalentVariants(profile=variants.profile, talents=variants.talents,
                actor_plan=plan, actor_spool=old)
            self.assertEqual(list(legacy.iter_state_metadata()), full)
            selected = 'skill_damage_talent_10_trait_10'
            del plan['aliases'][selected]
            self.assertEqual([r['talent']['id'] for r in variants.iter_state_metadata()], [20])
            reference = plan['aliases']['skill_damage_reference_20_trait_20']
            reference['canonical_name'] = 'missing'
            for consume in (lambda: list(variants), lambda: list(variants.iter_state_metadata())):
                with self.assertRaisesRegex(ValueError, '缺少对应前置 runtime actor'):
                    consume()
            reference['canonical_name'] = 'skill_damage_base'
            del reference['talent_effectiveness']
            with self.assertRaisesRegex(KeyError, 'talent_effectiveness'):
                list(variants.iter_state_metadata())

    def test_flatten_releases_previous_nested_graph_before_next_pair_load(self):
        test = self
        refs = []
        class Pairs:
            def iter_metadata(self):
                return iter(())
            iter_action_metadata = iter_metadata
            def __iter__(self):
                for index in range(3):
                    gc.collect()
                    test.assertTrue(all(ref() is None for ref in refs), 'previous pair graph still live')
                    def make_actor(hit):
                        values = Graph(amount(hit + 20))
                        baseline = Graph(amount(hit))
                        parent = Graph(action(hit=hit, scenarios=[scenario(0, values)]))
                        parent['baseline'] = baseline
                        child = Graph(action('child', 2, hit=50, child=True))
                        refs.extend(weakref.ref(value) for value in (parent, child, baseline, values))
                        return {'actions': [parent, child]}
                    item = variant(make_actor(100), make_actor(110), talent_id=index + 1)
                    item['reference_low'] = make_actor(100)
                    item['low'] = make_actor(120)
                    yield item
                    del item
        self.assertTrue(damage.flatten_single_talent_damage_variants({}, {}, Pairs()))
        gc.collect()
        self.assertTrue(all(ref() is None for ref in refs))

    def test_spool_producer_releases_previous_nested_graph_before_loading_next(self):
        refs = []
        test = self
        def load(health, name):
            if name == 'next':
                gc.collect()
                test.assertTrue(all(ref() is None for ref in refs), 'producer retained previous graph')
            values = Graph(amount())
            source = Graph(action())
            source['baseline'] = values
            if name == 'previous':
                refs.extend([weakref.ref(source), weakref.ref(values)])
            return {'actions': [source]}
        aliases = {}
        for entry, name in ((10, 'previous'), (20, 'next')):
            for kind in ('talent', 'reference'):
                aliases[f'skill_damage_{kind}_{entry}_trait_{entry}'] = {
                    'canonical_name': name, 'talent_effectiveness': 'active'}
        variants = damage._SpoolBackedTalentVariants(profile=SimpleNamespace(spec='fixture'),
            talents=[talent(10), talent(20)], actor_plan={'aliases': aliases}, actor_spool=SimpleNamespace(load=load))
        iterator = iter(variants)
        item = next(iterator)
        del item
        item = next(iterator)
        iterator.close()
