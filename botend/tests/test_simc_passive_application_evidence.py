"""Application evidence, not a DBC relationship, authorizes division."""
import copy
import unittest

from botend.services.simc_skill_passive_projection import normalize_native_specialization_passives
from botend.tests.test_simc_skill_passive_projection import native_execute_actor


class PassiveApplicationEvidenceTests(unittest.TestCase):
    def test_legacy_candidate_keeps_amounts_and_reports_missing_proof(self):
        actor = native_execute_actor(proven=False)
        before = copy.deepcopy(actor)
        effects = normalize_native_specialization_passives(actor)
        self.assertEqual(effects, [])
        self.assertEqual(actor, before)
        diagnostics = []
        normalize_native_specialization_passives(actor, diagnostics=diagnostics)
        self.assertTrue(diagnostics)
        self.assertEqual({row['reason'] for row in diagnostics}, {'missing_runtime_application'})
        self.assertTrue(all(row['source_spell_id'] == 137050 for row in diagnostics))


    def test_product_projection_retains_damage_and_exposes_diagnostics(self):
        from botend.services.simc_skill_damage import project_skill_damage_product_payload
        actor = native_execute_actor()
        original = copy.deepcopy(actor)
        projected = project_skill_damage_product_payload({'actors': [actor]})['actors'][0]
        self.assertEqual(actor, original)
        self.assertEqual(projected['global_skill_effects'], [])
        self.assertEqual(len(projected['specialization_passive_diagnostics']), 2)
        expected = sum(a['baseline']['direct']['expected'] for a in original['actions'])
        self.assertAlmostEqual(sum(a['product']['final_normalized_damage']
                                   for a in projected['actions']), expected)
        for row in projected['actions']:
            for component in row['product']['formula_components']:
                self.assertEqual(component['runtime_factors'], [1.525])


if __name__ == '__main__':
    unittest.main()
