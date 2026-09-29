"""Frozen reviewed facts gate ownership before display enrichment/deduplication."""
import copy
from types import SimpleNamespace

from django.test import SimpleTestCase

from botend.services.simc_skill_damage import (
    prune_global_damage_talents,
    project_global_skill_effects_for_display,
    project_skill_damage_product_payload,
    reviewed_global_display_effects,
)


class GlobalDisplayIntegrityTests(SimpleTestCase):
    def fact(self, *, entry=101, spell=10, index=1, physical=100, kind='talent', **extra):
        row = {
            'effect_id': f'reviewed_scope:{"天赋" if kind == "talent" else "自身状态"}:{spell}:{entry if kind == "talent" else "None"}',
            'source_type': 'reviewed_scope', 'source_kind': kind,
            'source_class': 'warrior', 'specializations': ['fury'],
            'source_spell_ids': [spell],
            'global_components': [{'spell_id': spell, 'effect_index': index, 'effect_id': physical}],
            'effect_details': [{'source_spell_id': spell, 'effect_index': index,
                                'base_value': 20, 'value_kind': 'percent', 'label': '直接伤害'}],
            'projections': [], 'excluded_before_probe': True,
        }
        return {**row, **extra}

    def static(self, entry=101, **extra):
        return {
            'effect_id': f'dbc_global_talent:{entry}', 'source_type': 'talent',
            'talent_id': entry + 1000, 'source_spell_ids': [10],
            'scope_evidence': 'dbc_global_damage_talent', 'excluded_before_probe': True,
            'global_components': [{'spell_id': 10, 'effect_indices': [1]}],
            'runtime_conditions': [], 'projections': [], **extra,
        }

    def runtime(self, **extra):
        return {
            'effect_id': 'declared_runtime_state:buff.example[self:10]',
            'source_type': 'runtime_state', 'source_spell_ids': [10],
            'scope_evidence': 'declared_global_damage_state', 'excluded_before_probe': True,
            'runtime_conditions': [{'token': 'buff.example', 'scope': 'self', 'spell_id': 10, 'stacks': 1}],
            'runtime_condition': '效果生效时', 'projections': [], **extra,
        }

    def actor(self, facts, effects):
        return {'class': 'warrior', 'specialization': 'fury',
                'reviewed_global_effects': facts, 'global_skill_effects': effects,
                'actions': [{'token': 'local', 'variant': {'talent_id': 1101}}]}

    def test_owner_gate_precedes_enrichment_and_static_runtime_merge(self):
        local_a = {'spell_id': 10, 'effect_index': 9, 'effect_id': 109}
        local_b = {'spell_id': 10, 'effect_index': 13, 'effect_id': 113}
        facts = [self.fact(local_components=[local_a]),
                 self.fact(kind='buff', local_components=[local_b],
                           local_skill_bindings=[{**local_b, 'skill_spell_ids': [55]}])]
        # Old products already inherited fury from unrelated entries: trust the
        # frozen TraitEntry relation, not the previously enriched owner fields.
        effects = [self.static(999, specializations=['fury']), self.static(), self.runtime()]
        actor = self.actor(facts, effects)
        before = copy.deepcopy(actor)
        rows = reviewed_global_display_effects(actor)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['runtime_conditions'], effects[-1]['runtime_conditions'])
        self.assertEqual(rows[0]['local_components'], [local_a, local_b])
        self.assertEqual(rows[0]['local_skill_bindings'], facts[-1]['local_skill_bindings'])
        self.assertNotIn('dbc_global_talent:999', {s['effect_id'] for s in rows[0]['sources']})
        self.assertIn('dbc_global_talent:101', {s['effect_id'] for s in rows[0]['sources']})
        self.assertEqual(actor, before)
        self.assertEqual(reviewed_global_display_effects({**actor, 'global_skill_effects': rows}), rows)

    def test_reviewed_static_absence_is_not_a_legacy_runtime_blacklist(self):
        foreign = self.static(source_spell_ids=[20])
        declared = self.runtime(source_spell_ids=[30])
        legacy = {'effect_id': 'runtime:old', 'source_type': 'runtime_state',
                  'source_spell_ids': [40], 'projections': [{'kind': 'damage_multiplier', 'value': 1.5}]}
        actor = self.actor([], [foreign, declared, legacy])
        self.assertEqual(reviewed_global_display_effects(actor), [legacy])

    def test_condition_stack_scope_value_and_hero_states_are_not_coalesced(self):
        facts = [self.fact(kind='buff')]
        baseline = self.runtime()
        states = [baseline]
        for condition in (
            {'stacks': 2}, {'scope': 'target'}, {'token': 'buff.other'},
            {'target_health_percentage': 34},
        ):
            states.append(self.runtime(runtime_conditions=[{**baseline['runtime_conditions'][0], **condition}]))
        states.extend([
            self.runtime(projections=[{'kind': 'damage_multiplier', 'value': 1.2}]),
            self.runtime(projections=[{'kind': 'damage_multiplier_range', 'minimum': 1.1, 'maximum': 1.3}]),
            self.runtime(value_status='rank_dependent'),
            self.runtime(hero_subtree_id=60),
            self.runtime(configuration={'rank': 2}),
            self.runtime(runtime_condition='仅目标生命低于35%时'),
        ])
        rows = reviewed_global_display_effects(self.actor(facts, states + [copy.deepcopy(baseline)]))
        self.assertEqual(len(rows), len(states))
        for state in states:
            self.assertTrue(any(all(row.get(k) == state.get(k) for k in (
                'runtime_conditions', 'runtime_condition', 'projections', 'value_status', 'hero_subtree_id', 'configuration',
            )) for row in rows))

    def test_talent_and_target_debuff_share_physical_definition_not_two_cards(self):
        facts = [self.fact(), self.fact(kind='debuff')]
        runtime = self.runtime(runtime_conditions=[{'token': 'debuff.example', 'scope': 'target',
                                                    'spell_id': 10, 'stacks': 1}])
        actor = self.actor(facts, [self.static(), runtime])
        rows = reviewed_global_display_effects(actor)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['runtime_conditions'], runtime['runtime_conditions'])
        self.assertEqual(reviewed_global_display_effects({**actor, 'global_skill_effects': rows}), rows)

    def test_owner_and_child_spell_ids_are_not_an_exact_tuple_lookup(self):
        part = {'spell_id': 11, 'effect_index': 2, 'effect_id': 112}
        fact = self.fact(global_components=[part], effect_details=[
            {'source_spell_id': 11, 'effect_index': 2, 'base_value': 3, 'value_kind': 'percent'},
        ])
        effect = self.static(source_spell_ids=[10, 11],
                             global_components=[{'spell_id': 11, 'effect_indices': [2]}])
        rows = reviewed_global_display_effects(self.actor([fact], [effect]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['global_components'], [part])
        self.assertEqual(rows[0]['effect_details'], fact['effect_details'])

    def test_different_physical_components_same_name_and_spell_survive(self):
        facts = [self.fact(kind='buff'), self.fact(kind='buff', index=2, physical=200)]
        effects = [self.runtime(global_components=f['global_components'], display_name='相同名字') for f in facts]
        rows = reviewed_global_display_effects(self.actor(facts, effects))
        self.assertEqual(len(rows), 2)
        self.assertEqual({tuple(p['effect_id'] for p in r['global_components']) for r in rows}, {(100,), (200,)})

    def test_legacy_runtime_other_component_of_reviewed_spell_survives(self):
        legacy = {'effect_id': 'runtime:old', 'source_type': 'runtime_state', 'source_spell_ids': [10],
                  'global_components': [{'spell_id': 10, 'effect_index': 7, 'effect_id': 700}],
                  'projections': [{'kind': 'damage_multiplier', 'value': 1.5}]}
        rows = reviewed_global_display_effects(self.actor([self.fact()], [legacy]))
        self.assertIn(legacy, rows)

    def test_conflicting_native_values_are_not_merged(self):
        a = self.fact(kind='buff')
        b = copy.deepcopy(a)
        b['effect_details'][0]['base_value'] = 30
        rows = reviewed_global_display_effects(self.actor([a, b], []))
        self.assertEqual(len(rows), 2)
        self.assertEqual([r['effect_details'][0]['base_value'] for r in rows], [20, 30])

    def test_catalog_bridge_cannot_merge_conflicting_component_values(self):
        def fact(values):
            parts = [self.fact(kind='buff', index=index, physical=100 + index)
                     for index, value in values]
            return self.fact(
                kind='buff',
                global_components=[part['global_components'][0] for part in parts],
                effect_details=[{**part['effect_details'][0], 'base_value': value}
                                for part, (_, value) in zip(parts, values)],
            )

        # Both records match the bridge separately, but their shared effect 1
        # has conflicting values. Compatibility is not a transitive relation.
        actor = self.actor([
            fact([(1, 20), (2, 20)]),
            fact([(1, 30), (3, 20)]),
            fact([(2, 20), (3, 20)]),
        ], [])
        before = copy.deepcopy(actor)
        rows = reviewed_global_display_effects(actor)
        self.assertEqual(len(rows), 2)
        for row in rows:
            values = {}
            for detail in row['effect_details']:
                key = (detail['source_spell_id'], detail['effect_index'])
                values.setdefault(key, set()).add(detail['base_value'])
            self.assertTrue(all(len(items) == 1 for items in values.values()))
        self.assertEqual(actor, before)
        self.assertEqual(reviewed_global_display_effects({**actor, 'global_skill_effects': rows}), rows)

    def test_catalog_activation_and_value_states_survive_repeated_projection(self):
        for extra in (
            {'rank': 2}, {'stacks': 2}, {'scope': 'target'},
            {'activation_conditions': [{'spell_id': 20}]},
            {'scenario_tokens': ['buff.other']}, {'affected_target_counts': [2]},
            {'talent_configuration': {'rank': 2}}, {'dbc_base_multiplier': 1.3},
            {'runtime_condition': '仅目标生命低于35%时'},
        ):
            with self.subTest(extra=extra):
                actor = self.actor([self.fact(kind='buff'), self.fact(kind='buff', **extra)], [])
                rows = reviewed_global_display_effects(actor)
                self.assertEqual(len(rows), 2)
                self.assertTrue(any(all(row.get(k) == v for k, v in extra.items()) for row in rows))
                self.assertEqual(reviewed_global_display_effects({**actor, 'global_skill_effects': rows}), rows)

    def test_ambiguous_catalog_does_not_drop_runtime_observation(self):
        facts = [self.fact(kind='buff'), self.fact(kind='buff', index=2, physical=200)]
        observation = self.runtime(projections=[{'kind': 'damage_multiplier', 'value': 1.7}])
        actor = self.actor(facts, [observation])
        rows = reviewed_global_display_effects(actor)
        self.assertIn(observation, rows)
        self.assertEqual(len(rows), 3)
        self.assertEqual(reviewed_global_display_effects({**actor, 'global_skill_effects': rows}), rows)

    def test_definition_different_target_coverage_is_not_attached(self):
        actor = self.actor([self.fact(), self.fact(kind='buff')], [
            self.static(affected_target_counts=[2]), self.runtime(affected_target_counts=[1]),
        ])
        rows = reviewed_global_display_effects(actor)
        self.assertEqual(len(rows), 2)
        self.assertEqual([row['affected_target_counts'] for row in rows], [[2], [1]])
        self.assertEqual(reviewed_global_display_effects({**actor, 'global_skill_effects': rows}), rows)

    def test_invalid_or_contradictory_trait_identity_cannot_borrow_owner(self):
        for extra in ({'trait_entry_id': '101'}, {'trait_entry_id': 102},
                      {'effect_id': 'dbc_global_talent:0101'}, {'owner_spell_id': 999},
                      {'effect_id': 'dbc_global_talent:0101', 'trait_entry_id': 101}):
            with self.subTest(extra=extra):
                source = self.static(**extra)
                rows = reviewed_global_display_effects(self.actor([self.fact()], [source]))
                self.assertFalse(any(r.get('source_type') == 'talent' for r in rows))
                self.assertFalse(any(s.get('scope_evidence') == 'dbc_global_damage_talent'
                                     for r in rows for s in r.get('sources', [])))

    def test_read_projection_and_generation_share_rules_without_reprojecting_actions(self):
        actor = self.actor([self.fact(), self.fact(kind='buff')], [self.static(), self.runtime()])
        payload = {'actors': [actor], 'identity': {'schema_revision': 44}}
        before = copy.deepcopy(payload)
        projected = project_global_skill_effects_for_display(payload)
        self.assertEqual(len(projected['actors'][0]['global_skill_effects']), 1)
        self.assertEqual(projected['actors'][0]['actions'], before['actors'][0]['actions'])
        self.assertIs(projected['actors'][0]['actions'], actor['actions'])
        self.assertEqual(project_global_skill_effects_for_display(projected), projected)
        self.assertEqual(payload, before)
        actor['actions'] = []
        generated = project_skill_damage_product_payload({'actors': [actor]})
        self.assertEqual(generated['actors'][0]['global_skill_effects'], projected['actors'][0]['global_skill_effects'])

    def test_static_source_exports_structured_entry_identity(self):
        talent = SimpleNamespace(pk=999, node_id=101)
        catalog = {101: {'spell_id': 10, 'global_components': [{'spell_id': 10, 'effect_indices': [1]}],
                         'evidence': 'dbc_global_damage_talent', 'dbc_base_multiplier': None,
                         'has_rank_scaling': True}}
        *_, effects = prune_global_damage_talents([talent], [], {}, catalog)
        self.assertEqual(effects[0]['trait_entry_id'], 101)
        self.assertEqual(effects[0]['owner_spell_id'], 10)
        self.assertEqual(effects[0]['value_status'], 'rank_dependent')
