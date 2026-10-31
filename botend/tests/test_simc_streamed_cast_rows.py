"""Private replayable compressed rows retain the complete public row contract."""
import copy
import gc
import json
import weakref
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from botend.services import simc_skill_damage as damage
from botend.tests.test_simc_compact_cast_rows import part, typed, TrackedDict
from botend.tests.test_simc_owned_product_projection import leaf
from botend.models import WowSpellSnapshot
from botend.services.wow_spell_display import sync_spell_display


def complete_pairs(*, compact=False, stream=False):
    groups = {}
    for rows, source in [
        ([part('main')], None),
        ([part('main', 31)], {'actions': [part('main'), part('child', 20), part('first-only', 7)]}),
        ([part('child', 99)], {'actions': [part('wrong-source', 80)]}),
        ([part('other', owner=10)], None),
    ]:
        damage.complete_cast_damage_components(
            rows, {id(row): source for row in rows} if source else {},
            grouped=groups, finalize=False, compact_cast_rows=compact)
    rows = damage.complete_cast_damage_components(
        [], {}, grouped=groups, compact_cast_rows=compact, stream_cast_rows=stream)
    if not (compact and stream):
        damage.attach_runtime_product_metrics({'actions': rows})
    return rows


class StreamedCastRowsTests(SimpleTestCase):
    def test_default_lists_replay_metrics_order_first_source_and_late_original(self):
        expected = complete_pairs()
        compact = complete_pairs(compact=True)
        rows = complete_pairs(compact=True, stream=True)
        self.assertIs(type(expected), list)
        self.assertIs(type(compact), list)
        self.assertNotIsInstance(rows, list)
        self.assertEqual(len(rows), len(expected))
        self.assertEqual(typed(compact), typed(expected))
        self.assertEqual(typed(list(rows)), typed(expected))
        self.assertEqual(typed(list(rows)), typed(expected))
        self.assertEqual(typed(rows[:]), typed(expected))
        self.assertEqual(typed(rows[-1]), typed(expected[-1]))
        self.assertEqual([r['token'] for r in rows], ['main', 'main', 'child', 'first-only', 'other'])
        self.assertEqual(rows[2]['baseline']['direct']['hit'], 99)
        self.assertEqual(rows[3]['baseline']['direct']['product']['current_talent_damage'], 7)
        with self.assertRaises(TypeError):
            json.dumps(rows)  # Never masquerade as an empty C-encoder list.
        rows[0]['baseline']['direct']['hit'] = -1
        self.assertEqual(rows[0]['baseline']['direct']['hit'], 30)
        self.assertEqual(typed(rows[::-1]), typed(expected[::-1]))
        with self.assertRaises(IndexError):
            rows[len(rows)]
        with self.assertRaises(TypeError):
            rows[0] = {}

    def test_ready_row_append_preserves_non_json_types_and_complete_metrics(self):
        row = part('main')
        row['payload'] = {'\ud800': (None, True, 1, 1.0, -0.0),
                          ('tuple', 2): [b'bytes', {'z': 2, 'a': 1}],
                          3: 'int', 4.5: 'float', None: 'none'}
        damage.attach_runtime_product_metrics({'actions': [row]})
        rows = damage._ReplayableCastRows()
        rows.append(row)
        self.assertEqual(typed(rows[0]), typed(row))
        self.assertEqual(typed(list(rows)), typed([row]))

    def test_flatten_complete_types_values_and_metrics_equal_for_all_flags(self):
        base = {'actions': [part('main'), part('child', 20)]}
        variants = [{'talent': {'id': n, 'node_id': n, 'name': str(n)},
                     'high': {'actions': [part('main', 40 + n), part('child', 20)]},
                     'low': {'actions': [part('main', 40 + n), part('child', 20)]}}
                    for n in range(1, 5)]
        expected = damage.flatten_single_talent_damage_variants(base, base, variants)
        for compact, stream in [(False, True), (True, False), (True, True)]:
            actual = damage.flatten_single_talent_damage_variants(
                base, base, variants, compact_cast_rows=compact, stream_cast_rows=stream)
            if not (compact and stream):
                self.assertIs(type(actual), list)
            self.assertEqual(typed(list(actual)), typed(expected))
            self.assertEqual(json.dumps(list(actual)), json.dumps(expected))

    def test_finalization_and_consumption_keep_no_decoded_row_cache(self):
        groups = {}
        for n in range(20):
            damage.complete_cast_damage_components(
                [part(str(n), owner=n)], {}, grouped=groups,
                compact_cast_rows=True, finalize=False)
        unpack = damage._unpack_cast_row
        references = []
        maximum = [0]
        attach = damage.attach_runtime_product_metrics
        enriched = []

        def attach_one(actor):
            self.assertEqual(len(actor['actions']), 1)
            enriched.append(actor['actions'][0]['token'])
            return attach(actor)

        def observe(stored):
            gc.collect()
            live = sum(ref() is not None for ref in references)
            maximum[0] = max(maximum[0], live)
            self.assertLessEqual(live, 1)
            row = TrackedDict(unpack(stored))
            references.append(weakref.ref(row))
            return row

        with patch.object(damage, '_unpack_cast_row', new=observe), \
             patch.object(damage, 'attach_runtime_product_metrics', new=attach_one):
            rows = damage.complete_cast_damage_components(
                [], {}, grouped=groups, compact_cast_rows=True, stream_cast_rows=True)
            self.assertEqual(groups, {})
            self.assertFalse(any(ref() is not None for ref in references))
            self.assertEqual(enriched, [str(n) for n in range(20)])
            for _ in range(2):
                for row in rows:
                    self.assertIn('product', row['baseline']['direct'])
                del row
            for row in damage._consume_owned_action_rows(rows):
                self.assertIn('product', row['baseline']['direct'])
            del row
        gc.collect()
        self.assertFalse(any(ref() is not None for ref in references))
        self.assertEqual(list(rows), [None] * 20)
        self.assertEqual(len(rows), 20)


class StreamedProductRowsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        WowSpellSnapshot.objects.create(
            branch='wow', locale='zhCN', spell_id=5308, name='Execute',
            name_zh='斩杀', description='真实数据库说明。', snapshot_build='12.1.0.69497')
        sync_spell_display(branch='retail', apply=True)

    def test_complete_product_owned_projection_and_sqlite_localization_order(self):
        raw = [leaf('execute'), leaf('execute_dot', 'tick'),
               leaf('strike_mh'), leaf('strike_oh'), leaf('execute', hero=[61]),
               leaf('execute', hero=[60]), {'supported': False}]
        raw[1]['reporting_root_token'] = 'execute'
        raw[0]['baseline']['direct']['runtime_layers'] = {
            'specialization_passive_effects': [{'source_spell_id': 5308}]}
        # leaf's fixture has product-only amounts; supply real native inputs.
        for row in raw:
            for dbc_component in row.get('dbc_scaling', {}).values():
                dbc_component['normalized_base'] = 100.
            for component in row.get('baseline', {}).values():
                component.update(hit=120., crit=240., crit_multiplier=2., crit_chance=.2, expected=144.)
        groups = {}
        damage.complete_cast_damage_components(
            copy.deepcopy(raw), {}, grouped=groups, finalize=False, compact_cast_rows=True)
        streamed = damage.complete_cast_damage_components(
            [], {}, grouped=groups, compact_cast_rows=True, stream_cast_rows=True)
        damage.attach_runtime_product_metrics({'actions': raw})
        payload = lambda rows: {'actors': [{'class': 'warrior', 'specialization': 'fury', 'actions': rows}]}
        expected = damage.localize_skill_damage_payload(
            damage.project_skill_damage_product_payload(payload(raw)))
        references = []
        unpack = damage._unpack_cast_row

        def observe(stored):
            gc.collect()
            self.assertLessEqual(sum(ref() is not None for ref in references), 1)
            row = TrackedDict(unpack(stored))
            references.append(weakref.ref(row))
            return row

        with patch.object(damage, '_unpack_cast_row', new=observe):
            actual = damage.project_skill_damage_product_payload(payload(streamed), take_ownership=True)
        gc.collect()
        self.assertTrue(references)
        self.assertFalse(any(ref() is not None for ref in references))
        self.assertIs(type(actual['actors'][0]['actions']), list)
        self.assertEqual(list(streamed), [None] * len(raw))
        actual = damage.localize_skill_damage_payload(actual, take_ownership=True)
        self.assertEqual(typed(actual), typed(expected))
        self.assertEqual(json.dumps(actual, ensure_ascii=False), json.dumps(expected, ensure_ascii=False))
        self.assertEqual(actual['actors'][0]['actions'][0]['display_name'], '斩杀')
        self.assertTrue(actual['actors'][0]['actions'])
