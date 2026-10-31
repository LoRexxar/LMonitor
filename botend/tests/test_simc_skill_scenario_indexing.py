"""Bound scenario indexing work without changing runtime or cast provenance."""
import copy
import gc
import weakref
from collections import Counter
from unittest import mock

from django.test import SimpleTestCase

from botend.services import simc_skill_damage as damage


def amount(hit=100.0, multiplier=1.0):
    return {
        'direct': {
            'hit': hit * multiplier, 'crit': hit * multiplier * 2,
            'expected': hit * multiplier * 1.2, 'crit_multiplier': 2.0,
            'crit_chance': 0.2, 'damage_equivalent_count': 1.0,
            'runtime_layers': {
                field: multiplier if field == 'da_multiplier' else 1.0
                for field in damage._RUNTIME_LAYER_FIELDS['direct']
            },
        },
        'tick': None, 'unresolved_reason': None,
    }


def scenario(index, values, *, token=None):
    return {
        'buffs': [{'token': token or f'buff.state_{index}', 'scope': 'self',
                   'spell_id': 1000 + index, 'stacks': 1}],
        'values': values,
        'affected_target_counts': [1, 5],
    }


def action(token='cast', spell_id=1, *, hit=100.0, multiplier=1.0,
           scenarios=(), child=False):
    return {
        'token': token, 'spell_id': spell_id, 'supported': True,
        'player_skill': not child, 'harmful': True,
        'reporting_root_token': 'cast' if child else token,
        'reporting_root_spell_id': 1 if child else spell_id,
        'reporting_root_component': True,
        'baseline': amount(hit, multiplier), 'scenarios': list(scenarios),
    }


def variant(reference, selected, *, talent_id=7):
    return {
        'talent': {'id': talent_id, 'node_id': talent_id + 100,
                   'name': 'Indexed talent', 'tree_type': 'spec'},
        'reference_high': reference, 'reference_low': reference,
        'high': selected, 'low': selected,
    }


class ScenarioIndexingTests(SimpleTestCase):
    def test_flatten_reuses_one_index_per_action_for_base_and_talent_casts(self):
        for count in (4, 12):
            for talent in (False, True):
                with self.subTest(count=count, talent=talent):
                    parent = action(hit=110 if talent else 100, scenarios=[
                        scenario(index, amount(120 + index)) for index in range(count)
                    ])
                    child = action('child', 2, hit=50, child=True, scenarios=[
                        scenario(index, amount(50)) for index in range(count)
                    ])
                    # Identical duplicates retain first metadata rather than last.
                    duplicate = copy.deepcopy(parent['scenarios'][0])
                    duplicate['affected_target_counts'] = [20]
                    parent['scenarios'].append(duplicate)
                    selected = {'actions': [parent, child]}
                    reference = {'actions': [action(), action('child', 2, hit=50, child=True)]}
                    base = {} if talent else selected
                    variants = [variant(reference, selected)] if talent else []
                    frozen = copy.deepcopy((base, variants))
                    calls = Counter()
                    original = damage._scenario_amounts

                    def tracked(source, **kwargs):
                        if source is not None:
                            calls[id(source)] += 1
                        return original(source, **kwargs)

                    with mock.patch.object(damage, '_scenario_amounts', new=tracked):
                        rows = damage.flatten_single_talent_damage_variants(base, base, variants)
                    self.assertEqual((base, variants), frozen)
                    self.assertEqual([
                        (row['token'], row['variant']['scenario_tokens'],
                         row['baseline']['direct']['hit'], row.get('cast_component_unchanged', False))
                        for row in rows
                    ], [
                        ('cast', [], 110 if talent else 100, False), ('child', [], 50, True),
                        *[entry for index in range(count) for entry in (
                            ('cast', [f'buff.state_{index}'], 120 + index, False),
                            ('child', [f'buff.state_{index}'], 50, True),
                        )],
                    ])
                    self.assertEqual(rows[2]['affected_target_counts'], [1, 5])
                    self.assertEqual(rows[3]['affected_target_counts'], [1, 5])
                    self.assertLessEqual(max(calls.values()), 1, calls)
                    rows[3]['baseline']['direct']['hit'] = -1
                    self.assertEqual((base, variants), frozen)

    def test_flatten_keeps_first_source_and_later_original_overrides_child(self):
        def pair(child_hit, retained_hit):
            reference = {'actions': [action(), action('child', 2, hit=50, child=True),
                                     action('retained', 3, hit=retained_hit, child=True)]}
            selected = {'actions': [action(hit=110), action('child', 2, hit=child_hit, child=True),
                                    action('retained', 3, hit=retained_hit, child=True)]}
            return variant(reference, selected)

        variants = [pair(50, 70), pair(60, 80)]
        frozen = copy.deepcopy(variants)
        rows = damage.flatten_single_talent_damage_variants({}, {}, variants)
        self.assertEqual([
            (row['token'], row['baseline']['direct']['hit'], row.get('cast_component_unchanged', False))
            for row in rows
        ], [('cast', 110, False), ('cast', 110, False), ('child', 60, False), ('retained', 70, True)])
        self.assertEqual(variants, frozen)

    def test_flatten_lazy_pairs_release_old_action_indexes(self):
        class RawAction(dict):
            pass

        refs = []
        test = self

        class Pairs:
            def __iter__(self):
                previous = []
                for index in range(4):
                    if len(previous) >= 2:
                        gc.collect()
                        test.assertTrue(all(ref() is None for ref in previous[-2]))
                    parent = RawAction(action(hit=110, scenarios=[scenario(0, amount(120))]))
                    child = RawAction(action('child', 2, hit=50, child=True,
                                             scenarios=[scenario(0, amount(50))]))
                    current = [weakref.ref(parent), weakref.ref(child)]
                    refs.extend(current)
                    previous.append(current)
                    selected = {'actions': [parent, child]}
                    item = variant({'actions': [action()]}, selected, talent_id=index + 1)
                    yield item
                    del parent, child, selected, item

        rows = damage.flatten_single_talent_damage_variants({}, {}, Pairs())
        self.assertTrue(rows)
        gc.collect()
        self.assertTrue(all(ref() is None for ref in refs))

    def runtime_pair(self, count):
        reference, selected = {'actions': []}, {'actions': []}
        for root_index, token in enumerate(('a', 'b')):
            reference['actions'].append(action(token, root_index + 1, scenarios=[
                scenario(index, amount(multiplier=1.1)) for index in range(count)
            ]))
            selected['actions'].append(action(token, root_index + 1, multiplier=1.1, scenarios=[
                scenario(index, amount(multiplier=(
                    1.452 if index % 3 == 0 else
                    1.21 if index % 3 == 1 or root_index == 1 else 1.573
                ))) for index in range(count)
            ]))
        return reference, selected

    def test_runtime_pair_indexes_are_linear_across_accepted_noop_and_rejected_tokens(self):
        for count in (3, 12):
            with self.subTest(count=count):
                reference, selected = self.runtime_pair(count)
                frozen = copy.deepcopy((reference, selected))
                calls = Counter()
                original = damage._scenario_amounts

                def tracked(source):
                    calls[id(source)] += 1
                    return original(source)

                with mock.patch.object(damage, '_scenario_amounts', new=tracked):
                    candidates = damage._runtime_layer_candidates(reference, selected)
                expected_tokens = {damage._scenario_identity(scenario(index, {}))
                                   for index in range(count) if index % 3 == 0}
                self.assertEqual(set(candidates), expected_tokens)
                for candidate in candidates.values():
                    self.assertAlmostEqual(candidate['multiplier'], 1.2)
                    self.assertEqual(candidate['runtime_layer'], 'da_multiplier')
                    self.assertEqual(candidate['runtime_components'], ['direct'])
                    self.assertEqual(candidate['evidence_roots'], [
                        {'token': 'a', 'spell_id': 1}, {'token': 'b', 'spell_id': 2},
                    ])
                    self.assertEqual(candidate['evidence_root_count'], 2)
                    self.assertEqual(candidate['evidence_component_count'], 2)
                self.assertEqual((reference, selected), frozen)
                self.assertEqual(len(calls), 4)
                self.assertLessEqual(max(calls.values()), 1, calls)

    def test_runtime_selected_only_state_reuses_actor_token_indexes(self):
        reference, selected = self.runtime_pair(6)
        for actor in (reference, selected):
            for source in actor['actions']:
                for index, state in enumerate(source['scenarios']):
                    state['buffs'][0]['token'] = f'buff.trigger_{index}'
                    state['values'] = amount(multiplier=2.0 if actor is reference else 1.32)
        selected['actions'].extend({'token': f'trigger_{index}', 'supported': False}
                                   for index in range(6))
        frozen = copy.deepcopy((reference, selected))
        original = damage._canonical_runtime_state_token
        calls = Counter()

        def tracked(value):
            if value in ('a', 'b'):
                calls[value] += 1
            return original(value)

        with mock.patch.object(damage, '_canonical_runtime_state_token', new=tracked):
            candidates = damage._runtime_layer_candidates(reference, selected)
        self.assertEqual(len(candidates), 7)  # Passive and all six selected-only states.
        self.assertAlmostEqual(candidates[()]['multiplier'], 1.1)
        for tokens, candidate in candidates.items():
            if tokens:
                self.assertAlmostEqual(candidate['multiplier'], 1.2)
        self.assertEqual((reference, selected), frozen)
        self.assertEqual(calls, {'a': 2, 'b': 2})

    def test_runtime_cache_is_call_local_and_duplicate_scenarios_still_fail_closed(self):
        reference, selected = self.runtime_pair(1)
        state = selected['actions'][0]['scenarios'][0]
        selected['actions'][0]['scenarios'].append(copy.deepcopy(state))
        tokens = damage._scenario_identity(state)
        self.assertIn(tokens, damage._runtime_layer_candidates(reference, selected))
        for source in selected['actions']:
            for row in source['scenarios']:
                row['values'] = amount(multiplier=1.573)
        self.assertAlmostEqual(
            damage._runtime_layer_candidates(reference, selected)[tokens]['multiplier'], 1.3,
        )
        selected['actions'][0]['scenarios'][-1]['values'] = amount(multiplier=1.8)
        with self.assertRaisesRegex(ValueError, 'scenario identity'):
            damage._runtime_layer_candidates(reference, selected)
        with self.assertRaisesRegex(ValueError, 'scenario identity'):
            damage.flatten_single_talent_damage_variants(selected, selected, [])

    def test_runtime_preserves_early_returns_and_missing_scenario_coverage(self):
        reference, selected = self.runtime_pair(1)
        # An unpaired reference must not be eagerly indexed or raise new errors.
        unpaired = action('unpaired', 99, scenarios=[scenario(0, amount()), scenario(0, amount(999))])
        self.assertEqual(damage._runtime_layer_candidates({'actions': [unpaired]}, selected), {})
        # Duplicate action identities remain fail-closed before scenario validation.
        duplicate = copy.deepcopy(selected)
        duplicate['actions'].append(copy.deepcopy(duplicate['actions'][0]))
        duplicate['actions'][0]['scenarios'].append(scenario(0, amount(999)))
        self.assertEqual(damage._runtime_layer_candidates(reference, duplicate), {})
        # Missing reference scenario falls back to its baseline; selected absence
        # must not turn one compared root into full cross-root corroboration.
        for source in reference['actions']:
            source['scenarios'] = []
        selected['actions'][1]['scenarios'] = []
        self.assertEqual(damage._runtime_layer_candidates(reference, selected), {})
        selected['actions'][0]['scenarios'] = []
        candidates = damage._runtime_layer_candidates(reference, selected)
        self.assertEqual(set(candidates), {()})
        self.assertAlmostEqual(candidates[()]['multiplier'], 1.1)
