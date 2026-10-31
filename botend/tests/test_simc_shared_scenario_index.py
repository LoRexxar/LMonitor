"""Pair-local index ownership: share keys and read-only lists, never inputs."""
import copy
import weakref
from collections import Counter
from unittest import mock

from django.test import SimpleTestCase

from botend.services import simc_skill_damage as damage
from botend.tests.test_simc_skill_scenario_indexing import action, amount, scenario, variant
from botend.tests.test_simc_variant_metadata_budget import OldProvider


class Graph(dict):
    pass


class ScenarioList(list):
    pass


class SharedScenarioIndexTests(SimpleTestCase):
    def capture_indexes(self, high, low, variants=()):
        snapshots = []
        original = damage.complete_cast_damage_components

        def capture(*args, **kwargs):
            lookup = kwargs.get('scenario_lookup')
            if lookup is not None:
                # Observe live role entries before flatten deliberately releases them.
                closure = dict(zip(lookup.__code__.co_freevars,
                                   (cell.cell_contents for cell in lookup.__closure__)))
                snapshots.append(dict(closure['scenario_indexes']))
            return original(*args, **kwargs)

        with mock.patch.object(damage, 'complete_cast_damage_components', side_effect=capture):
            rows = damage.flatten_single_talent_damage_variants(high, low, variants)
        return rows, snapshots

    def test_one_identity_pass_shares_amount_and_metadata_keys(self):
        states = [scenario(index, amount(120 + index)) for index in range(3)]
        states.extend([None, 'ignored', {'buffs': [], 'values': amount(999)}])
        source = action(scenarios=states)
        actor = {'actions': [source]}
        before = copy.deepcopy(actor)
        with mock.patch.object(damage, '_scenario_identity', wraps=damage._scenario_identity) as identity:
            rows, snapshots = self.capture_indexes(actor, actor)
        self.assertEqual(sum('values' in call.args[0] for call in identity.call_args_list), 4)
        _, amounts, metadata = snapshots[0][id(source)]
        self.assertEqual(len(amounts), 3)
        self.assertEqual(len(metadata), 4)
        self.assertIs(metadata[()], source['scenarios'][-1])
        for amount_key, metadata_key in zip(amounts, metadata):
            self.assertIs(amount_key, metadata_key)
        self.assertEqual(actor, before)
        rows[1]['baseline']['direct']['hit'] = -1
        self.assertEqual(actor, before)

    def test_distinct_health_shells_share_only_the_same_list(self):
        for shared in (True, False):
            with self.subTest(shared=shared):
                high = action(scenarios=[scenario(0, amount(120))])
                low = action(hit=80)
                low['scenarios'] = high['scenarios'] if shared else copy.deepcopy(high['scenarios'])
                actors = ({'actions': [high]}, {'actions': [low]})
                before = copy.deepcopy(actors)
                with mock.patch.object(damage, '_scenario_identity', wraps=damage._scenario_identity) as identity:
                    rows, snapshots = self.capture_indexes(*actors)
                self.assertEqual(sum('values' in call.args[0] for call in identity.call_args_list),
                                 1 if shared else 2)
                high_index, low_index = (snapshots[0][id(source)] for source in (high, low))
                self.assertIs(high_index[0], high)
                self.assertIs(low_index[0], low)
                for position in (1, 2):
                    if shared:
                        self.assertIs(high_index[position], low_index[position])
                    else:
                        self.assertIsNot(high_index[position], low_index[position])
                state_rows = [row for row in rows if row['variant']['scenario_tokens']]
                self.assertEqual(len(state_rows), 2)
                self.assertEqual([row['affected_target_counts'] for row in state_rows], [[1, 5], [1, 5]])
                self.assertEqual(actors, before)

    def test_duplicate_first_metadata_empty_identity_and_legacy_amount(self):
        first = scenario(0, amount(120))
        duplicate = copy.deepcopy(first)
        duplicate['affected_target_counts'] = [99]
        empty = {'buffs': [], 'values': amount(500), 'affected_target_counts': [7]}
        legacy = {'active_buffs': ['legacy'], 'amount': amount(130)}
        source = action(scenarios=[first, duplicate, empty, legacy])
        actor = {'actions': [source]}
        rows, snapshots = self.capture_indexes(actor, actor)
        _, amounts, metadata = snapshots[0][id(source)]
        first_key = next(iter(amounts))
        self.assertIs(amounts[first_key], first['values'])
        self.assertIs(metadata[first_key], first)
        self.assertIs(metadata[()], empty)
        self.assertNotIn((), amounts)
        self.assertIs(amounts[(('legacy', '', 0, 1),)], legacy['amount'])
        self.assertEqual(rows[1]['affected_target_counts'], [1, 5])
        for left, right in zip(amounts, (key for key in metadata if key)):
            self.assertIs(left, right)
        duplicate['values'] = amount(999)
        before = copy.deepcopy(actor)
        with self.assertRaisesRegex(ValueError, '^exporter 同一 scenario identity 返回冲突数值：buff.state_0$'):
            damage.flatten_single_talent_damage_variants(actor, actor, ())
        self.assertEqual(actor, before)

    def test_non_list_keeps_two_pass_generator_and_tuple_behavior(self):
        for kind in ('generator', 'tuple', 'none'):
            with self.subTest(kind=kind):
                states = [scenario(0, amount(120))]
                source = action()
                source['scenarios'] = (iter(states) if kind == 'generator' else
                                       tuple(states) if kind == 'tuple' else None)
                actor = {'actions': [source]}
                rows, snapshots = self.capture_indexes(actor, actor)
                _, amounts, metadata = snapshots[0][id(source)]
                self.assertEqual(len(amounts), 0 if kind == 'none' else 1)
                self.assertEqual(len(metadata), 1 if kind == 'tuple' else 0)
                if kind == 'generator':
                    self.assertNotIn('affected_target_counts', rows[1])
                    self.assertEqual(list(source['scenarios']), [])
        self.assertEqual(damage._scenario_amounts(None), {})

    def test_old_provider_and_generator_variants_preserve_first_source_children(self):
        def pair(child_hit, retained_hit):
            reference = {'actions': [action(), action('child', 2, hit=50, child=True),
                                     action('retained', 3, hit=retained_hit, child=True)]}
            high = {'actions': [action(hit=110, scenarios=[scenario(0, amount(120))]),
                                action('child', 2, hit=child_hit, child=True),
                                action('retained', 3, hit=retained_hit, child=True)]}
            item = variant(reference, high)
            item['low'] = {'actions': [dict(source) for source in high['actions']]}
            return item

        variants = [pair(50, 70), pair(60, 80)]
        before = copy.deepcopy(variants)
        expected = damage.flatten_single_talent_damage_variants({}, {}, variants)
        for provider in (OldProvider(variants), iter(variants)):
            self.assertEqual(damage.flatten_single_talent_damage_variants({}, {}, provider), expected)
        passive = [row for row in expected if not row['variant']['scenario_tokens']]
        self.assertEqual([(row['token'], row['baseline']['direct']['hit'],
                           row.get('cast_component_unchanged', False)) for row in passive],
                         [('cast', 110, False), ('cast', 110, False),
                          ('child', 60, False), ('retained', 70, True)])
        self.assertEqual(variants, before)

    def test_shared_indexes_are_pair_local_and_release_before_next_load(self):
        refs = []
        test = self
        calls = Counter()
        original = damage._scenario_identity

        class Pairs:
            def iter_metadata(self):
                return iter(())
            iter_action_metadata = iter_metadata

            def __iter__(self):
                for index in range(3):
                    # No collect(): borrowed nested graphs must be gone immediately.
                    test.assertTrue(all(ref() is None for ref in refs))
                    values = Graph(amount(120))
                    states = ScenarioList([scenario(0, values)])
                    high = Graph(action(hit=110))
                    high['scenarios'] = states
                    low = Graph(dict(high))
                    refs.extend(weakref.ref(value) for value in (values, states, high, low))
                    item = variant({'actions': [action()]}, {'actions': [high]}, talent_id=index + 1)
                    item['low'] = {'actions': [low]}
                    yield item
                    del item, high, low, states, values

        def tracked(state):
            if 'values' in state:
                calls['identity'] += 1
            return original(state)

        with mock.patch.object(damage, '_scenario_identity', new=tracked):
            rows = damage.flatten_single_talent_damage_variants({}, {}, Pairs())
        self.assertTrue(rows)
        self.assertEqual(calls['identity'], 3)
        self.assertTrue(all(ref() is None for ref in refs))

    def test_same_list_is_reindexed_between_base_and_each_pair(self):
        source = action(hit=110, scenarios=[scenario(0, amount(120))])
        actor = {'actions': [source]}
        pairs = [variant({'actions': [action()]}, actor, talent_id=index) for index in (1, 2)]
        with mock.patch.object(damage, '_scenario_identity', wraps=damage._scenario_identity) as identity:
            self.assertTrue(damage.flatten_single_talent_damage_variants(actor, actor, pairs))
        self.assertEqual(sum('values' in call.args[0] for call in identity.call_args_list), 3)
