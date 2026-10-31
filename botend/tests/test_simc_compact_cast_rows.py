"""Lossless internal cast retention; no native exports or external pickle input."""
import copy
import gc
import pickle
import weakref
from unittest.mock import patch

from django.test import SimpleTestCase
from botend.services import simc_skill_damage as damage


class TrackedDict(dict):
    pass


def part(token, value=30, owner=5):
    return {'token': token, 'spell_id': 1, 'supported': True,
            'reporting_root_component': True, 'reporting_root_token': 'cast',
            'reporting_root_spell_id': 100, 'baseline': {'direct': {'hit': value}},
            'scenarios': [], 'variant': {'talent_id': owner, 'runtime_condition': '',
                                        'runtime_conditions': [], 'reference_available': True}}


def typed(value):
    if isinstance(value, dict):
        return (type(value), [(typed(k), typed(v)) for k, v in value.items()])
    if isinstance(value, (tuple, list)):
        return (type(value), [typed(v) for v in value])
    return (type(value), value)


class CompactCastRowsTests(SimpleTestCase):
    def test_cross_pair_budget_types_first_source_later_original_and_release(self):
        normal, compact = {}, {}
        packed_calls = []
        # Observe real packing, not a replacement implementation.
        pack = getattr(damage, '_pack_cast_row', None)
        self.assertIsNotNone(pack, 'cross-pair rows must have internal packed storage')
        count = [0]
        def counted_pack(row):
            count[0] += 1
            return pack(row)
        # A Mock records row arguments and would itself defeat weakref checks.
        with patch.object(damage, '_pack_cast_row', new=counted_pack):
            def pair(rows, actor=None):
                source = {id(r): actor for r in rows} if actor else {}

                ordinary = copy.deepcopy(rows)
                ordinary_sources = {id(r): copy.deepcopy(actor) for r in ordinary} if actor else {}
                damage.complete_cast_damage_components(ordinary, ordinary_sources, grouped=normal, finalize=False)
                damage.complete_cast_damage_components(rows, source, grouped=compact, finalize=False,
                                                       compact_cast_rows=True)
                packed_calls.append(count[0])

            row = TrackedDict(part('main'))
            row['payload'] = {'\ud800': (None, True, 1, 1.0, -0.0),
                              ('tuple', 2): [b'bytes', {'z': 2, 'a': 1}],
                              3: 'int', 4.5: 'float', None: 'none'}
            row_ref = weakref.ref(row)
            pair([row])  # No source yet: cannot decide children until later.
            del row
            gc.collect()
            self.assertIsNone(row_ref())
            actor = TrackedDict(actions=[part('main'), part('child', 20), part('first-only', 7)])
            actor_ref = weakref.ref(actor)
            pair([part('main', 31)], actor)
            del actor
            gc.collect()
            self.assertIsNone(actor_ref())
            # New source must not replace first source; late original suppresses child.
            pair([part('child', 99)], {'actions': [part('child'), part('wrong-source', 80)]})
            for owner in range(10, 30):
                pair([part('other', owner=owner)])
            self.assertEqual(packed_calls, [1, 4, 5, *range(6, 26)])
            self.assertTrue(all(isinstance(r, damage._PackedCastRow)
                                for s in compact.values() for k in ('rows', 'children') for r in s[k]))
            expected = damage.complete_cast_damage_components([], {}, grouped=normal)
            actual = damage.complete_cast_damage_components([], {}, grouped=compact, compact_cast_rows=True)
            self.assertEqual(typed(actual), typed(expected))
            self.assertEqual([r['token'] for r in actual[:4]], ['main', 'main', 'child', 'first-only'])
            self.assertEqual(actual[2]['baseline']['direct']['hit'], 99)
            self.assertEqual(compact, {})
            self.assertEqual(count[0], 25)

    def test_flatten_full_output_matches_and_compacts_each_pair(self):
        base = {'actions': [part('main'), part('child', 20)]}
        variants = [{'talent': {'id': n, 'node_id': n, 'name': str(n)},
                     'high': {'actions': [part('main', 40 + n), part('child', 20)]},
                     'low': {'actions': [part('main', 40 + n), part('child', 20)]}}
                    for n in range(1, 5)]
        expected = damage.flatten_single_talent_damage_variants(base, base, variants)
        with patch.object(damage, '_pack_cast_row', wraps=damage._pack_cast_row) as spy:
            actual = damage.flatten_single_talent_damage_variants(base, base, variants, compact_cast_rows=True)
        self.assertGreater(spy.call_count, 0)
        self.assertEqual(typed(actual), typed(expected))

    def test_real_codec_compression_budget_and_roundtrip(self):
        row = part('main')
        row['payload'] = [{'label': 'repeated-state', 'amount': 123.125, 'conditions': [1, 2, None]}] * 200
        packed = damage._pack_cast_row(row)
        self.assertLess(len(packed.data), len(pickle.dumps(row, protocol=5)) // 2)
        self.assertEqual(typed(damage._unpack_cast_row(packed)), typed(row))

    def test_compact_flatten_releases_streamed_actors_before_next_pair(self):
        refs = []
        outer = self
        class Variants:
            def __iter__(self):
                for index in range(12):
                    gc.collect()
                    # The caller's loop variable can retain the preceding yield,
                    # but no older actor may survive in grouped cast storage.
                    outer.assertLessEqual(sum(ref() is not None for ref in refs), 1)
                    actor = TrackedDict(actions=[part('main', index + 40), part('child', 20)])
                    refs.append(weakref.ref(actor))
                    yield {'talent': {'id': index, 'node_id': index, 'tree_type': 'spec'},
                           'high': actor, 'low': actor,
                           'reference_high': {'actions': []}, 'reference_low': {'actions': []}}
                    del actor
        rows = damage.flatten_single_talent_damage_variants(
            {'actions': []}, {'actions': []}, Variants(), compact_cast_rows=True,
        )
        self.assertTrue(rows)
        gc.collect()
        self.assertFalse(any(ref() is not None for ref in refs))
