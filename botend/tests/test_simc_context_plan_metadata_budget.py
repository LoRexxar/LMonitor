"""Exercise the production context planner against real canonical spool files."""
import tempfile
from types import SimpleNamespace
from unittest import TestCase, mock

from botend.services import simc_skill_damage as damage


def talent(entry, *, hero=False):
    return SimpleNamespace(
        pk=entry, node_id=entry, talent_id=entry, max_points=1,
        tree_type='hero' if hero else 'spec', db2_subtree_id=60 if hero else None,
        name=f'Trait {entry}', name_zh=f'天赋{entry}',
    )


class ContextPlanMetadataBudgetTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.spool = damage._CanonicalActorSpool(self.directory.name)
        self.talents = [talent(10), talent(20), talent(30, hero=True)]
        self.ordinary = damage.plan_unique_talent_actor_configs(self.talents)
        self.anchor = SimpleNamespace(node_id=90, tree_type='hero_anchor')
        self.service = damage.SimcSkillDamageSnapshotService.__new__(
            damage.SimcSkillDamageSnapshotService)
        # Unsorted DBC roots exercise both direct and reporting-root identities.
        self.service._native_talent_catalog = [{
            'trait_entry_id': 10, 'spell_id': 200,
            'effects': [{'type': 6, 'subtype': 332, 'misc1': 100, 'value': root,
                         'id': root, 'index': index}
                        for index, root in enumerate((504, 502, 501, 503))],
        }]
        for health in (100, 34):
            rows = []
            for config in self.ordinary['actors']:
                traits = config['selected_talents']
                rows.append({
                    'name': config['name'],
                    'selected_trait_ids': [1] + [t.node_id for t in traits]
                                          + ([90] if any(t.tree_type == 'hero' for t in traits) else []),
                    'actions': [
                        {'spell_id': 501, 'baseline': {'direct': {'amount': 11}}},
                        {'spell_id': 900, 'reporting_root_spell_id': 502,
                         'scenarios': [{'damage': 12}]},
                        {'spell_id': 503, 'reporting_root_spell_id': 0},
                        {'spell_id': 901, 'reporting_root_spell_id': 504},
                        {'spell_id': 999},
                    ],
                })
            self.spool.store_many(health, rows)

    def plan(self, provider):
        return self.service._activation_context_plan(
            self.talents, [], {}, self.ordinary, provider, [self.anchor])

    def canonical_name(self, entry):
        return self.ordinary['aliases'][
            f'skill_damage_talent_{entry}_trait_{entry}']['canonical_name']

    def change_selection(self, health, name, selected):
        # Re-export into a fresh real spool: do not forge only its metadata cache
        # or overwrite an occupied shard ordinal when changing an existing actor.
        rows = {h: [self.spool.load(h, n) for target, n in self.spool._paths
                    if target == h] for h in (100, 34)}
        for actor in rows[health]:
            if actor['name'] == name:
                actor['selected_trait_ids'] = selected
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.spool = damage._CanonicalActorSpool(directory.name)
        for h, actors in rows.items():
            self.spool.store_many(h, actors)

    def assert_light_matches_full(self):
        # Legacy provider deliberately exposes only full load, not fake light graphs.
        with mock.patch.object(self.spool, 'load', wraps=self.spool.load) as full:
            expected = self.plan(SimpleNamespace(load=self.spool.load))
            self.assertGreater(full.call_count, 0)
        with mock.patch.object(self.spool, 'load', wraps=self.spool.load) as full:
            actual = self.plan(self.spool)
            self.assertEqual(actual, expected)
            self.assertEqual(full.call_count, 0, 'context planning must not decode full actor graphs')
        return actual

    def test_real_planner_preserves_four_roots_order_and_hero_anchor_with_zero_full_loads(self):
        plan = self.assert_light_matches_full()
        self.assertEqual(plan['context_count'], 1)
        self.assertEqual([p['talent'].node_id for p in plan['pairs']], [20, 30])
        for pair in plan['pairs']:
            self.assertEqual(pair['activation_context']['action_spell_ids'], [501, 502, 503, 504])
            self.assertEqual(pair['expected_reference_trait_entry_ids'], [1, 10])
        self.assertEqual(plan['pairs'][1]['expected_selected_trait_entry_ids'], [1, 10, 30, 90])
        metadata = self.spool.load_action_metadata(100, self.canonical_name(10))
        self.assertEqual(metadata['selected_trait_ids'], [1, 10])
        self.assertEqual([a.get('reporting_root_spell_id') or a.get('spell_id')
                          for a in metadata['actions']], [501, 502, 503, 504, 999])
        self.assertTrue(all('baseline' not in a and 'scenarios' not in a
                            for a in metadata['actions']))

    def test_each_optional_loader_falls_back_independently(self):
        expected = self.plan(SimpleNamespace(load=self.spool.load))
        for capability in ('load_identity', 'load_action_metadata'):
            with self.subTest(capability=capability):
                provider = SimpleNamespace(load=self.spool.load,
                    **{capability: getattr(self.spool, capability)})
                self.assertEqual(self.plan(provider), expected)

    def test_context_uses_actions_but_baseline_and_hero_proof_use_identity(self):
        with mock.patch.object(self.spool, 'load_action_metadata',
                               wraps=self.spool.load_action_metadata) as actions, \
                mock.patch.object(self.spool, 'load_identity',
                                  wraps=self.spool.load_identity) as identity, \
                mock.patch.object(self.spool, 'load', wraps=self.spool.load) as full:
            self.plan(self.spool)
        self.assertEqual(actions.call_args_list, [mock.call(100, self.canonical_name(10))])
        for health in (100, 34):
            self.assertIn(mock.call(health, 'skill_damage_base'), identity.call_args_list)
            self.assertIn(mock.call(health, self.canonical_name(30)), identity.call_args_list)
        self.assertEqual(full.call_count, 0)

    def test_missing_candidate_actor_still_provides_no_readiness(self):
        del self.spool._paths[(100, self.canonical_name(10))]
        plan = self.assert_light_matches_full()
        self.assertEqual(plan, {'actors': [], 'pairs': [], 'skipped': [], 'context_count': 0})

    def test_missing_candidate_selection_still_provides_no_readiness(self):
        self.change_selection(100, self.canonical_name(10), [1])
        self.assertEqual(self.assert_light_matches_full()['context_count'], 0)

    def test_baseline_mismatch_and_one_missing_probe_keep_exact_error(self):
        for case in ('mismatch', 'missing'):
            with self.subTest(case=case):
                key = (34, 'skill_damage_base')
                if case == 'mismatch':
                    self.change_selection(*key, [2])
                else:
                    del self.spool._paths[key]
                messages = []
                for provider in (SimpleNamespace(load=self.spool.load), self.spool):
                    with self.assertRaises(ValueError) as error:
                        self.plan(provider)
                    messages.append(str(error.exception))
                self.assertEqual(messages, [
                    'Ordinary baseline selection differs between target-health probes.'] * 2)

    def test_both_missing_baselines_preserve_legacy_empty_selection(self):
        for health in (100, 34):
            del self.spool._paths[(health, 'skill_damage_base')]
        plan = self.assert_light_matches_full()
        self.assertEqual(plan['pairs'][0]['expected_reference_trait_entry_ids'], [10])
        self.assertEqual(plan['pairs'][1]['expected_selected_trait_entry_ids'], [10, 30])

    def test_missing_hero_probe_cannot_prove_an_anchor(self):
        del self.spool._paths[(34, self.canonical_name(30))]
        plan = self.assert_light_matches_full()
        self.assertEqual(plan['pairs'][1]['expected_selected_trait_entry_ids'], [1, 10, 30])
