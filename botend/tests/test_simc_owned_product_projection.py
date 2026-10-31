import copy
import gc
import json
import weakref
import zlib
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from botend.models import WowSpellSnapshot
from botend.services import simc_skill_damage as damage
from botend.services.wow_spell_display import sync_spell_display


def leaf(token, component='direct', hero=None):
    row = {
        'token': token, 'name': token, 'spell_id': 5308, 'supported': True,
        'reporting_root_token': token, 'reporting_root_spell_id': 5308,
        'reporting_root_component': True,
        'variant': {'talent_id': None, 'runtime_conditions': []},
        'dbc_scaling': {component: {'attack_power_coefficient': 1.0,
                                   'spell_power_coefficient': 0.0}},
        'baseline': {component: {'damage_equivalent_count': 2.0, 'product': {
            'dbc_base_damage_min': 100.0, 'dbc_base_damage_max': 100.0,
            'current_talent_damage': 120.0, 'crit_damage': 240.0,
            'crit_multiplier': 2.0, 'actual_crit_chance': 0.2,
            'normalized_expected': 144.0}}},
    }
    if hero:
        row['hero_subtree_ids'] = hero
    return row


class WeakRow(dict):
    pass


class OwnedProductProjectionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        WowSpellSnapshot.objects.create(
            branch='wow', locale='zhCN', spell_id=5308, name='Execute',
            name_zh='斩杀', description='真实数据库说明。', snapshot_build='12.1.0.69497')
        sync_spell_display(branch='retail', apply=True)

    def test_ownership_key_preserves_lone_surrogates_and_scalar_distinctions(self):
        names = [json.loads('"\\ud800"'), json.loads('"\\udfff"'),
                 '\ud83d\ude00', '\U0001f600']
        keys = []
        for name in names:
            with self.subTest(codepoints=[ord(c) for c in name]):
                row = leaf('execute')
                row['variant']['talent_name'] = name
                key, _heroes = damage._action_variant_ownership_key(row)
                original = json.dumps(row['variant'], ensure_ascii=False,
                                      sort_keys=True, separators=(',', ':'))
                self.assertEqual(zlib.decompress(key).decode('utf-8', 'surrogatepass'), original)
                keys.append(key)
                payload = {'actors': [{'actions': [row]}]}
                before = copy.deepcopy(payload)
                borrowed = damage.project_skill_damage_product_payload(payload)
                owned = damage.project_skill_damage_product_payload(copy.deepcopy(payload), take_ownership=True)
                self.assertEqual(payload, before)
                self.assertEqual(borrowed, owned)
                self.assertEqual(len(owned['actors'][0]['actions']), 1)
                self.assertEqual(owned['actors'][0]['actions'][0]['variant']['talent_name'], name)
        self.assertEqual(len(set(keys)), len(names))

    def test_real_generator_transfers_owned_payload_through_both_boundaries(self):
        service = damage.SimcSkillDamageSnapshotService(
            SimpleNamespace(simc_revision='a' * 40, game_build='12.1.0.69497', schema_revision=51),
            backend=SimpleNamespace(identifier='production'))
        profile = SimpleNamespace(class_name='warrior', spec='fury')
        talent = SimpleNamespace(pk=1, node_id=1, spell_id=5308, tree_type='spec',
                                 name='Execute', name_zh='斩杀', talent_version_id=None)
        projector = damage.project_skill_damage_product_payload
        localizer = damage.localize_skill_damage_payload
        seen = []

        def project(payload, *, take_ownership=False):
            self.assertTrue(take_ownership)
            result = projector(payload, take_ownership=take_ownership)
            self.assertIs(result, payload)
            seen.append('project')
            return result

        def localize(payload, *, take_ownership=False):
            self.assertTrue(take_ownership)
            result = localizer(payload, take_ownership=take_ownership)
            self.assertIs(result, payload)
            seen.append('localize')
            return result

        def export(_profile, _talents, *, actor_plan=None, **kwargs):
            return {'actors': [{'name': spec['name'], 'class': 'warrior',
                                'spec': 'fury', 'actions': []}
                               for spec in actor_plan or []], 'unresolved': []}

        with patch.object(service, '_talent_entries', return_value=[talent]), \
             patch.object(service, '_spec_root_scaffold', return_value=[talent]), \
             patch.object(service, '_talent_prerequisite_map', return_value={1: []}), \
             patch.object(service, '_hero_talent_trees', return_value=[]), \
             patch.object(service, '_implicit_prerequisite_nodes', return_value=[]), \
             patch.object(service, '_global_damage_talent_catalog', return_value={}), \
             patch.object(service, '_run_profile_export', new=export), \
             patch.object(damage, 'project_skill_damage_product_payload', new=project), \
             patch.object(damage, 'localize_skill_damage_payload', new=localize):
            actor, unresolved, raw_count = service._generate_profile_product_actor(profile)
        self.assertEqual(seen, ['project', 'localize'])
        self.assertEqual(actor['actions'], [])
        self.assertEqual((unresolved, raw_count), ([], 0))

    def test_borrowed_and_owned_full_results_and_identity(self):
        first = leaf('execute')
        first['first_source'] = {'facts': ['中文', 1, 1.0]}
        tick = leaf('execute_dot', 'tick')
        tick['reporting_root_token'] = 'execute'
        rows = [first, tick, leaf('strike_mh'), leaf('strike_oh'),
                leaf('execute', hero=[61]), leaf('execute', hero=[60]),
                {'supported': False}]
        payload = {'identity': {'game_build': '12.1.0.69497'}, 'actors': [{
            'class': 'warrior', 'specialization': 'fury', 'actions': rows,
            'global_skill_effects': [{'source_type': 'runtime_state',
                'source_kind': 'buff', 'source_token': 'buff.test',
                'source_spell_ids': [5308], 'partial': True}],
        }]}
        original = copy.deepcopy(payload)
        projected = damage.project_skill_damage_product_payload(payload)
        projected_before = copy.deepcopy(projected)
        expected = damage.localize_skill_damage_payload(projected)
        self.assertEqual(projected, projected_before)
        self.assertEqual(payload, original)
        actor = payload['actors'][0]
        actual = damage.project_skill_damage_product_payload(payload, take_ownership=True)
        self.assertIs(actual, payload)
        self.assertIs(actual['actors'][0], actor)
        self.assertTrue(all(row is None for row in rows))
        self.assertIs(damage.localize_skill_damage_payload(actual, take_ownership=True), actual)
        self.assertEqual(actual, expected)
        self.assertEqual(json.dumps(actual, ensure_ascii=False), json.dumps(expected, ensure_ascii=False))
        self.assertEqual(actual['actors'][0]['actions'][0]['display_name'], '斩杀')
        self.assertEqual(actual['actors'][0]['actions'][0]['component_count'], 2)
        self.assertEqual(actual['actors'][0]['actions'][1]['component_count'], 2)

    def test_owned_consumes_previous_row_before_processing_next(self):
        rows = [WeakRow(leaf('first')), WeakRow(leaf('second'))]
        references = [weakref.ref(row) for row in rows]
        original = damage._action_variant_ownership_key
        seen = []

        def observe(row):
            if row['token'] == 'second':
                gc.collect()
                self.assertIsNone(references[0]())
                seen.append(True)
            return original(row)

        # A Mock's call history would itself retain the row under observation.
        with patch.object(damage, '_action_variant_ownership_key', new=observe):
            damage.project_skill_damage_product_payload({'actors': [{'actions': rows}]}, take_ownership=True)
        gc.collect()
        self.assertTrue(seen)
        self.assertTrue(all(reference() is None for reference in references))

    def test_consuming_iterator_does_not_cache_rows_even_after_exception(self):
        rows = [WeakRow({'value': 1}), WeakRow({'value': 2})]
        reference = weakref.ref(rows[0])
        iterator = damage._consume_owned_action_rows(rows)
        row = next(iterator)
        self.assertIsNone(rows[0])
        del row
        gc.collect()
        self.assertIsNone(reference())
        try:
            for row in iterator:
                raise ValueError('stop')
        except ValueError:
            pass
        last = weakref.ref(row)
        del row
        gc.collect()
        self.assertIsNone(last())
        self.assertEqual(rows, [None, None])

    def test_ownership_key_lossless_canonical_json_and_distinctions(self):
        variants = [{}, {'x': None}, {'x': 1}, {'x': 1.0}, {'x': True},
                    {'x': '中文😀'}, {'x': '中⽂😀'}]
        keys = []
        for variant in variants:
            key, heroes = damage._action_variant_ownership_key({
                'variant': variant, 'hero_subtree_ids': [61, 60, 61, True, -1]})
            self.assertIsInstance(key, bytes)
            self.assertEqual(zlib.decompress(key).decode('utf-8'), json.dumps(
                variant, ensure_ascii=False, sort_keys=True, separators=(',', ':')))
            self.assertEqual(heroes, (60, 61))
            keys.append(key)
        self.assertEqual(len(set(keys)), len(variants))
        self.assertEqual(damage._action_variant_ownership_key({'variant': {'x': 1}}),
                         damage._action_variant_ownership_key({'variant': {
                             'x': 1, 'reference_available': False,
                             'reference_unavailable_reason': '中文'}}))
