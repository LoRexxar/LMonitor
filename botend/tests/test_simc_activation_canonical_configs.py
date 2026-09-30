from types import SimpleNamespace
from unittest import TestCase

from botend.services.simc_skill_damage import (
    SimcSkillDamageSnapshotService, plan_unique_talent_actor_configs,
)
from botend.tests.test_simc_skill_activation_context import trait


class CanonicalActivationConfigTests(TestCase):
    def test_reuses_materialized_reference_and_context_without_scaffold(self):
        for context_replaces_scaffold in (False, True):
            with self.subTest(context_replaces_scaffold=context_replaces_scaffold):
                scaffold = trait(1, choice=100)
                replacement = trait(2, choice=100)
                unlock, target = trait(10), trait(30)
                talents = [replacement, unlock, target]
                prerequisites = {30: [replacement]}
                if context_replaces_scaffold:
                    prerequisites[10] = [replacement]
                ordinary = plan_unique_talent_actor_configs(
                    talents, scaffold_talents=[scaffold],
                    talent_prerequisites=prerequisites,
                )
                actors = {a['name']: {
                    'selected_trait_ids': [t.node_id for t in a['selected_talents']],
                    'actions': [{'spell_id': 300}] if unlock in a['selected_talents'] else [],
                } for a in ordinary['actors']}
                service = object.__new__(SimcSkillDamageSnapshotService)
                service._native_talent_catalog = [{
                    'trait_entry_id': 10, 'spell_id': 200,
                    'effects': [{'type': 6, 'subtype': 332, 'misc1': 100, 'value': 300}],
                }]
                plan = service._activation_context_plan(
                    talents, [scaffold], prerequisites, ordinary,
                    SimpleNamespace(load=lambda health, name: actors.get(name)),
                )
                if context_replaces_scaffold:
                    pair = next(p for p in plan['pairs'] if p['talent'] is target)
                    configs = {a['name']: a['selected_talents'] for a in [*ordinary['actors'], *plan['actors']]}
                    self.assertEqual({t.node_id for t in configs[pair['reference_name']]}, {2, 10})
                    self.assertEqual({t.node_id for t in configs[pair['selected_name']]}, {2, 10, 30})
                else:
                    self.assertTrue(any(s['talent_entry_id'] == 30 for s in plan['skipped']))
