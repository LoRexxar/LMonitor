"""Only private rows about to be packed may borrow read-only native subgraphs."""
import copy
import gc
import weakref
from unittest.mock import patch

from django.test import SimpleTestCase

from botend.services import simc_skill_damage as damage
from botend.tests.test_simc_compact_cast_rows import typed


class CopyTrackedList(list):
    deepcopy_calls = 0

    def __deepcopy__(self, memo):
        type(self).deepcopy_calls += 1
        result = type(self)()
        memo[id(self)] = result
        result.extend(copy.deepcopy(value, memo) for value in self)
        return result


class AmountDict(dict):
    pass


class ComponentDict(dict):
    pass


def amount(hit, *, scenario=False):
    return AmountDict(
        direct=ComponentDict(
            hit=hit, crit=hit * 2, crit_multiplier=2., crit_chance=.2,
            expected=hit * 1.2,
            runtime_layers={'da_multiplier': 1.75 if scenario else 1.25,
                            'target_da_multiplier': 1.1 if scenario else 1.},
            runtime_factor_layers=[{'old': 'replace only on scenario rows'}],
            audit=CopyTrackedList([{'order': [3, 1, 2]}])),
        tick=ComponentDict(
            hit=hit / 2, runtime_layers={'ta_multiplier': 1.5 if scenario else 1.2},
            audit=CopyTrackedList([{'tick': True}])),
        audit=CopyTrackedList([{'amount': [None, True, 1, 1., -0.0]}]))


def actor(hit):
    main = {
        'token': 'main', 'spell_id': 100, 'supported': True, 'player_skill': True,
        'reporting_root_component': True, 'reporting_root_token': 'cast',
        'reporting_root_spell_id': 1000,
        'baseline': amount(hit),
        'scenarios': [{'buffs': [{'token': 'state', 'scope': 'buff', 'spell_id': 42,
                                 'stacks': 2}],
                       'values': amount(hit + 10, scenario=True),
                       'affected_target_counts': [1, 2, 5]}],
        'payload': CopyTrackedList([{'\ud800': (None, True, 1, 1., -0.0),
                                    ('tuple', 2): [b'bytes', {'z': 2, 'a': 1}]}]),
        'activation_conditions': [{'kind': 'native', 'entries': [10]}],
        'hero_subtree_ids': [999],  # Unproved ownership must still be removed.
    }
    child = {
        **main, 'token': 'child', 'spell_id': 101, 'player_skill': False,
        'baseline': amount(7), 'scenarios': [],
        'payload': CopyTrackedList([{'child': [1, 2]}]),
    }
    return {'actions': [main, child], 'selected_trait_ids': [10]}


def fixture():
    base_high, base_low = actor(30), actor(25)
    high, low = actor(50), actor(40)
    variants = [{
        'talent': {'id': 20, 'node_id': 20, 'name': 'Talent', 'tree_type': 'spec'},
        'high': high, 'low': low,
        'reference_high': base_high, 'reference_low': base_low,
        'reference_traits': [{'trait_entry_id': 10, 'name': 'Fixed'}],
        'activation_context': {
            'traits': [{'trait_entry_id': 10, 'name': 'Fixed', 'hero_subtree_id': 61}],
            'evidence': [{'trait_entry_id': 10}], 'trait_entry_ids': [10],
        },
    }]
    return base_high, base_low, variants


class CompactRowCopyOnWriteTests(SimpleTestCase):
    def test_compact_only_skips_recursive_copies_but_preserves_complete_contract(self):
        inputs = fixture()
        original = typed(inputs)
        expected = damage.flatten_single_talent_damage_variants(*inputs)
        self.assertEqual(typed(inputs), original)
        expected_value = typed(expected)
        self.assertGreater(CopyTrackedList.deepcopy_calls, 0)
        self.assertTrue(any(row.get('cast_component_unchanged') for row in expected))
        self.assertTrue(any(row['variant']['runtime_conditions'] for row in expected))
        self.assertTrue(any(row['variant'].get('activation_context') for row in expected))
        self.assertTrue(any(row['variant'].get('reference_context') for row in expected))
        self.assertTrue(any('血量' in row['variant']['runtime_condition'] for row in expected))

        pack = damage._pack_cast_row
        packed_sources = []
        native_payload_ids = {id(action['payload'])
                              for native in (inputs[0], inputs[1],
                                             inputs[2][0]['high'], inputs[2][0]['low'])
                              for action in native['actions']}

        def observe_pack(row):
            # Do not keep row arguments: that would itself defeat release checks.
            packed_sources.append(id(row['payload']) in native_payload_ids)
            return pack(row)

        for stream in (False, True):
            with self.subTest(stream=stream):
                CopyTrackedList.deepcopy_calls = 0
                with patch.object(damage, '_pack_cast_row', new=observe_pack):
                    result = damage.flatten_single_talent_damage_variants(
                        *inputs, compact_cast_rows=True, stream_cast_rows=stream)
                self.assertEqual(CopyTrackedList.deepcopy_calls, 0,
                                 'private immediately-packed rows must not recursively clone read-only payloads')
                self.assertEqual(typed(list(result)), expected_value)
                self.assertEqual(typed(inputs), original)
                if stream:
                    result[0]['payload'][0]['changed'] = True
                    result[0]['baseline']['direct']['hit'] = -1
                    self.assertEqual(typed(list(result)), expected_value)
                else:
                    result[0]['payload'][0]['changed'] = True
                    result[0]['baseline']['direct']['hit'] = -1
                    self.assertNotIn('changed', result[1]['payload'][0])
                self.assertEqual(typed(inputs), original)
        self.assertTrue(any(packed_sources), 'compact rows borrow metadata only up to the pack boundary')

        # Public/default rows keep their old deepcopy isolation, including custom
        # list/dict types; metadata unsupported by pickle is still valid here.
        class Target:
            pass
        target = Target()
        inputs[0]['actions'][0]['weakref'] = weakref.ref(target)
        CopyTrackedList.deepcopy_calls = 0
        result = damage.flatten_single_talent_damage_variants(*inputs)
        self.assertGreater(CopyTrackedList.deepcopy_calls, 0)
        self.assertIs(type(result), list)
        self.assertIs(result[0]['weakref'](), target)
        self.assertIs(type(result[0]['payload']), CopyTrackedList)
        self.assertIs(type(result[0]['baseline']), AmountDict)
        self.assertIs(type(result[0]['baseline']['direct']), ComponentDict)
        result[0]['payload'][0]['changed'] = True
        self.assertNotIn('changed', result[1]['payload'][0])
        self.assertNotIn('changed', inputs[0]['actions'][0]['payload'][0])

    def test_pack_is_detached_before_producer_reuses_source_and_keeps_no_payload_reference(self):
        outer = self
        payload_refs = []
        after_yield = []

        class Variants:
            def iter_metadata(self):
                return iter(())

            def iter_action_metadata(self):
                return iter(())

            def __iter__(self):
                for index in range(4):
                    native = actor(50 + index)
                    payload = native['actions'][0]['payload']
                    payload_refs.append(weakref.ref(payload))
                    before = typed(native)
                    yield {'talent': {'id': index + 1, 'node_id': index + 1},
                           'high': native, 'low': native,
                           'reference_high': {'actions': []},
                           'reference_low': {'actions': []}}
                    outer.assertEqual(typed(native), before)
                    # All original/child rows have been captured before advancing
                    # the producer. Mutation must not reach retained packed rows.
                    payload[0]['reused'] = index
                    native['actions'][0]['baseline']['direct']['hit'] = -100
                    after_yield.append(index)
                    del native, payload

        expected = damage.flatten_single_talent_damage_variants(
            {'actions': []}, {'actions': []}, Variants())
        payload_refs.clear()
        after_yield.clear()
        result = damage.flatten_single_talent_damage_variants(
            {'actions': []}, {'actions': []}, Variants(),
            compact_cast_rows=True, stream_cast_rows=True)
        self.assertEqual(after_yield, list(range(4)))
        self.assertEqual(typed(list(result)), typed(expected))
        gc.collect()
        self.assertTrue(payload_refs)
        self.assertFalse(any(ref() is not None for ref in payload_refs))
        self.assertFalse(any('reused' in row['payload'][0] for row in result))

    def test_shared_scenario_components_keep_legacy_last_write_and_types(self):
        # deepcopy used to preserve direct/tick aliases within an amount. A
        # per-component shallow clone must not silently change last-write values.
        inputs = fixture()
        native = inputs[2][0]['high']['actions'][0]
        scenario_amount = native['scenarios'][0]['values']
        scenario_amount['tick'] = scenario_amount['direct']
        native['baseline']['tick']['runtime_layers'] = {'da_multiplier': 2.}
        original = typed(inputs)
        expected = damage.flatten_single_talent_damage_variants(*inputs)
        actual = damage.flatten_single_talent_damage_variants(*inputs, compact_cast_rows=True)
        self.assertEqual(typed(actual), typed(expected))
        self.assertEqual(typed(inputs), original)
        shared = [row for row in actual if row['token'] == 'main'
                  and row['variant']['talent_id'] == 20
                  and row['variant']['runtime_conditions']
                  and not row['variant']['runtime_condition'].startswith('血量')][0]
        self.assertIs(shared['baseline']['direct'], shared['baseline']['tick'])
