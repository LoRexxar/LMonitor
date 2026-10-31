"""Metadata consumers must not decode scenario graphs to count or reject talents."""
import copy
import gc
import json
import tempfile
import weakref
from types import SimpleNamespace
from unittest import TestCase, mock

from botend.services import simc_skill_damage as damage
from botend.services.simc_skill_activation_context import plan_activation_context_pairs
from botend.tests.test_simc_reference_condition_context import actor


class Graph(dict):
    pass


def talent(entry, *, declared=False, hero=False):
    return SimpleNamespace(
        pk=entry, node_id=entry, talent_id=entry, max_points=1,
        tree_type='hero' if hero else 'spec', db2_subtree_id=60 if hero else None,
        name=f'Trait {entry}', name_zh=f'天赋{entry}',
        description='Increases your damage dealt by 10%.' if declared else '',
        description_zh='',
    )


class OldProvider:
    """Existing reiterable providers do not need to implement new protocols."""
    def __init__(self, rows):
        self.rows = rows

    def __iter__(self):
        yield from self.rows


class VariantMetadataBudgetTests(TestCase):
    def make_variants(self, root, talents, *, ownership=False):
        spool = damage._CanonicalActorSpool(root)
        plan = damage.plan_unique_talent_actor_configs(talents)
        for health in (100, 34):
            actors = []
            for config in plan['actors']:
                selected = config['selected_talents']
                value = 1.1 if selected else 1.0
                row = actor(value)
                row.update(name=config['name'], selected_trait_ids=[t.node_id for t in selected],
                           global_damage_policy='exclude_before_probe')
                # Two roots are needed for positive global scope evidence.
                second = copy.deepcopy(row['actions'][0])
                second.update(token='second', spell_id=501,
                              reporting_root_token='second', reporting_root_spell_id=501)
                row['actions'].append(second)
                for index, action in enumerate(row['actions']):
                    action['player_skill'] = not (ownership and index == 1)
                    action['baseline']['direct']['runtime_layers'] = {
                        field: value if field == 'da_multiplier' else 1.0
                        for field in damage._RUNTIME_LAYER_FIELDS['direct']
                    }
                    action['selected_trait_effects'] = [
                        {'trait_entry_id': t.node_id, 'source_spell_id': 700,
                         'effect_index': 1, 'unneeded_description': 'not an action graph'}
                        for t in selected if not ownership or index == 1
                    ]
                actors.append(row)
            spool.store_many(health, actors)
        variants = damage._SpoolBackedTalentVariants(
            profile=SimpleNamespace(spec='fixture'), talents=talents,
            actor_plan=plan, actor_spool=spool,
        )
        return spool, plan, variants

    def test_false_text_classification_never_decodes_actors(self):
        with tempfile.TemporaryDirectory() as root:
            spool, _, variants = self.make_variants(root, [talent(10), talent(20, hero=True)])
            with mock.patch.object(spool, 'load', wraps=spool.load) as load:
                self.assertEqual(damage.classify_global_damage_modifiers(variants), [])
            self.assertEqual(load.call_count, 0, 'count/text rejection must use only metadata')

    def test_declared_state_pass_retains_full_coverage_without_graph_decode(self):
        with tempfile.TemporaryDirectory() as root:
            spool, _, variants = self.make_variants(root, [talent(10), talent(20)])
            rows = list(variants)
            base = spool.load(100, 'skill_damage_base')
            expected = damage.classify_global_skill_effects(base, base, rows)
            with mock.patch.object(spool, 'load', wraps=spool.load) as load, mock.patch.object(
                damage, '_collect_declared_global_state_effects',
                wraps=damage._collect_declared_global_state_effects,
            ) as collect:
                actual = damage.classify_global_skill_effects(base, base, variants)
            self.assertEqual(actual, expected)
            self.assertEqual(load.call_count, 0, 'declared/excluded state collection uses real metadata')
            self.assertEqual(collect.call_count, 12)  # base + two pairs, four sources each

    def test_flatten_decodes_only_the_final_pass_and_preserves_complete_output(self):
        with tempfile.TemporaryDirectory() as root:
            spool, _, variants = self.make_variants(root, [talent(10), talent(20, hero=True)])
            rows = list(variants)
            before = copy.deepcopy(rows)
            base = spool.load(100, 'skill_damage_base')
            expected = damage.flatten_single_talent_damage_variants(base, base, rows)
            self.assertTrue(expected)
            with mock.patch.object(spool, 'load', wraps=spool.load) as load:
                actual = damage.flatten_single_talent_damage_variants(base, base, variants)
            self.assertEqual(actual, expected)
            self.assertEqual(rows, before)
            self.assertEqual(load.call_count, 4, 'hero/action metadata are light; identical health content decodes once')

    def test_native_hero_ownership_uses_effect_facts_not_similar_action_names(self):
        with tempfile.TemporaryDirectory() as root:
            spool, _, variants = self.make_variants(root, [talent(20, hero=True)], ownership=True)
            full = list(variants)
            base = spool.load(100, 'skill_damage_base')
            frozen = copy.deepcopy((base, full))
            expected = damage.flatten_single_talent_damage_variants(base, base, full)
            self.assertTrue(any(row.get('hero_subtree_ids') == [60] for row in expected))
            self.assertFalse(any(row.get('hero_subtree_ids') for row in expected if row['token'] != 'second'))
            self.assertEqual(damage.flatten_single_talent_damage_variants(base, base, variants), expected)
            self.assertEqual(damage.flatten_single_talent_damage_variants(base, base, OldProvider(full)), expected)
            self.assertEqual((base, full), frozen)

    def test_text_filter_loads_only_unique_declared_rows_and_preserves_fallbacks(self):
        talents = [talent(10), talent(20, declared=True), talent(30, declared=True)]
        # Duplicate talent identity must be counted, not silently deduplicated.
        talents.append(copy.copy(talents[-1]))
        with tempfile.TemporaryDirectory() as root:
            spool, _, variants = self.make_variants(root, talents)
            rows = list(variants)
            original = copy.deepcopy(rows)
            expected = damage.classify_global_damage_modifiers(rows)
            self.assertEqual([row['talent_id'] for row in expected], [20])
            with mock.patch.object(spool, 'load', wraps=spool.load) as load:
                actual = damage.classify_global_damage_modifiers(variants)
            self.assertEqual(actual, expected)
            self.assertEqual(load.call_count, 2)
            for provider in (OldProvider(rows), iter(rows)):
                self.assertEqual(damage.classify_global_damage_modifiers(provider), expected)
            base = spool.load(100, 'skill_damage_base')
            global_expected = damage.classify_global_skill_effects(base, base, rows)
            with mock.patch.object(spool, 'load', wraps=spool.load) as load:
                self.assertEqual(damage.classify_global_skill_effects(base, base, variants), global_expected)
            self.assertEqual(load.call_count, 4)  # Two text-selected passes, each with two distinct payloads.
            self.assertEqual(damage.classify_global_skill_effects(base, base, OldProvider(rows)), global_expected)
            self.assertEqual(rows, original)

    def test_metadata_and_filter_preserve_missing_alias_and_reference_errors(self):
        with tempfile.TemporaryDirectory() as root:
            spool, plan, variants = self.make_variants(root, [talent(10), talent(20)])
            selected = 'skill_damage_talent_10_trait_10'
            reference = 'skill_damage_reference_20_trait_20'
            del plan['aliases'][selected]
            full = list(variants)
            self.assertEqual([row['talent']['id'] for row in full], [20])
            with mock.patch.object(spool, 'load', side_effect=AssertionError('full graph decode')):
                self.assertEqual([row['talent'] for row in variants.iter_metadata()],
                                 [row['talent'] for row in full])
                self.assertEqual(list(variants.iter_filtered(lambda _: False)), [])
            # Reference presence must be checked even when the text predicate rejects.
            plan['aliases'][reference]['canonical_name'] = 'missing'
            for consume in (lambda: list(variants), lambda: list(variants.iter_metadata()),
                            lambda: list(variants.iter_filtered(lambda _: False))):
                with self.assertRaisesRegex(ValueError, '缺少对应前置 runtime actor'):
                    consume()

    def test_missing_canonical_selection_and_malformed_alias_match_full_iteration(self):
        with tempfile.TemporaryDirectory() as root:
            spool, plan, variants = self.make_variants(root, [talent(10)])
            selected = plan['aliases']['skill_damage_talent_10_trait_10']
            name = selected['canonical_name']
            selected['canonical_name'] = 'missing'
            self.assertEqual(list(variants), [])
            with mock.patch.object(spool, 'load', side_effect=AssertionError('full graph decode')):
                self.assertEqual(list(variants.iter_metadata()), [])
                self.assertEqual(list(variants.iter_filtered(lambda _: True)), [])
            selected['canonical_name'] = name
            del selected['talent_effectiveness']
            for consume in (lambda: list(variants), variants.iter_metadata,
                            lambda: variants.iter_filtered(lambda _: False)):
                with self.assertRaisesRegex(KeyError, 'talent_effectiveness'):
                    list(consume())

    def test_partial_health_and_shared_canonical_aliases_preserve_order_and_identity(self):
        with tempfile.TemporaryDirectory() as root:
            spool, plan, variants = self.make_variants(root, [talent(10), talent(20)])
            selected = 'skill_damage_talent_10_trait_10'
            del spool._paths[(34, plan['aliases'][selected]['canonical_name'])]
            # Distinct logical names may point at the same physical actor.
            plan['aliases']['skill_damage_talent_20_trait_20']['canonical_name'] = plan['aliases'][selected]['canonical_name']
            full = list(variants)
            with mock.patch.object(spool, 'load', side_effect=AssertionError('full graph decode')):
                metadata = list(variants.iter_metadata())
                actions = list(variants.iter_action_metadata())
            self.assertEqual([row['talent'] for row in metadata], [row['talent'] for row in full])
            for light, actual in zip(actions, full):
                for key in ('high', 'low', 'reference_high', 'reference_low'):
                    if actual[key] is None:
                        self.assertIsNone(light[key])
                    else:
                        self.assertEqual(light[key]['name'], actual[key]['name'])
                        self.assertEqual(light[key]['talent_effectiveness'], actual[key]['talent_effectiveness'])
                        self.assertEqual([damage._action_identity(a) for a in light[key]['actions']],
                                         [damage._action_identity(a) for a in actual[key]['actions']])

    def context_variants(self, root):
        unlock, target = talent(10), talent(30, hero=True)
        # Exercise materialize's string normalization rather than a second conversion.
        target.name = None
        plan = plan_activation_context_pairs([unlock, target],
            candidates=[{'trait_entry_id': 10, 'replacement_spell_id': 500}],
            contexts={10: {'selected_talents': [unlock], 'actor': {
                'selected_trait_ids': [10], 'actions': [{'spell_id': 500}]}}})
        spool = damage._CanonicalActorSpool(root)
        for health in (100, 34):
            actors = []
            for config in plan['actors']:
                row = actor(1.1 if target in config['selected_talents'] else 1.0)
                row.update(name=config['name'], selected_trait_ids=[t.node_id for t in config['selected_talents']])
                # An out-of-allowlist action must never affect hero attribution.
                extra = copy.deepcopy(row['actions'][0])
                extra.update(token='excluded', spell_id=999, reporting_root_spell_id=999)
                row['actions'].append(extra)
                actors.append(row)
            spool.store_many(health, actors)
        return spool, plan, damage._ActivationContextVariants([], plan['pairs'], spool)

    def test_context_metadata_reuses_materialize_validator_and_root_allowlist(self):
        with tempfile.TemporaryDirectory() as root:
            spool, plan, variants = self.context_variants(root)
            full = list(variants)
            expected = damage.flatten_single_talent_damage_variants({'actions': []}, {'actions': []}, full)
            with mock.patch.object(spool, 'load', wraps=spool.load) as load:
                self.assertEqual(damage.flatten_single_talent_damage_variants(
                    {'actions': []}, {'actions': []}, variants), expected)
            self.assertEqual(load.call_count, 4)
            with mock.patch.object(spool, 'load', side_effect=AssertionError('full graph decode')):
                self.assertEqual([r['talent'] for r in variants.iter_metadata()], [r['talent'] for r in full])
                light = list(variants.iter_action_metadata())
            self.assertEqual(light[0]['talent']['name'], '')
            self.assertEqual([a['spell_id'] for a in light[0]['high']['actions']], [500])
            self.assertEqual(light[0]['activation_context'], full[0]['activation_context'])
            # Corrupt the actual frozen facts at just one health. All protocols reject.
            key = (34, plan['pairs'][0]['selected_name'])
            spool._identities[key]['selected_trait_ids'].append(999)
            for consume in (variants.iter_metadata, variants.iter_action_metadata):
                with self.assertRaisesRegex(ValueError, 'Activation pair|selection mismatch'):
                    list(consume())

    def test_context_protocols_reject_the_same_invalid_expected_selection(self):
        with tempfile.TemporaryDirectory() as root:
            _, plan, variants = self.context_variants(root)
            plan['pairs'][0]['expected_selected_trait_entry_ids'].append(999)
            for consume in (lambda: iter(variants), variants.iter_metadata, variants.iter_action_metadata):
                with self.assertRaisesRegex(ValueError, 'Activation runtime selection mismatch'):
                    list(consume())

    def test_action_manifest_is_detached_minimal_and_does_not_retain_graphs(self):
        with tempfile.TemporaryDirectory() as root:
            spool = damage._CanonicalActorSpool(root)
            scenarios = Graph(payload=[1, 2, 3])
            action = Graph(token='derived', spell_id=500, supported=True, player_skill=False,
                scenarios=[scenarios], selected_trait_effects=[{'trait_entry_id': 10,
                'source_spell_id': 700, 'effect_index': 2, 'large': {'drop': True}}])
            refs = [weakref.ref(action), weakref.ref(scenarios)]
            source = {'name': 'physical', 'selected_trait_ids': [10], 'actions': [action]}
            original = copy.deepcopy(source)
            spool.store_many(100, [source])
            self.assertEqual(source, original)
            self.assertEqual(spool._paths[(100, 'physical')].read_text(),
                             json.dumps(source, ensure_ascii=False, separators=(',', ':')))
            facts = spool.load_action_metadata(100, 'physical')
            self.assertEqual(facts, {'name': 'physical', 'selected_trait_ids': [10], 'actions': [{
                'token': 'derived', 'spell_id': 500, 'supported': True, 'player_skill': False,
                'selected_trait_effects': [{'trait_entry_id': 10, 'source_spell_id': 700, 'effect_index': 2}],
            }]})
            facts['actions'][0]['selected_trait_effects'][0]['effect_index'] = 99
            self.assertEqual(spool.load_action_metadata(100, 'physical')['actions'][0]['selected_trait_effects'][0]['effect_index'], 2)
            self.assertIsNone(spool.load_action_metadata(34, 'physical'))
            del source, original, action, scenarios
            gc.collect()
            self.assertTrue(all(ref() is None for ref in refs))

    def test_filtered_iterator_close_and_exhaustion_release_decoded_graphs(self):
        with tempfile.TemporaryDirectory() as root:
            spool, _, variants = self.make_variants(root, [talent(10), talent(20)])
            original_load = spool.load
            refs = []

            def tracked(*args):
                row = original_load(*args)
                for index, action in enumerate(row['actions']):
                    row['actions'][index] = Graph(action)
                    refs.append(weakref.ref(row['actions'][index]))
                return row

            with mock.patch.object(spool, 'load', side_effect=tracked):
                iterator = variants.iter_filtered(lambda _: True)
                item = next(iterator)
                del item
                iterator.close()
                gc.collect()
                self.assertTrue(all(ref() is None for ref in refs))
                for item in variants.iter_filtered(lambda _: True):
                    pass
                del item
                gc.collect()
                self.assertTrue(all(ref() is None for ref in refs))
