"""Negative execution-boundary cases around captured native outputs."""

import copy
from dataclasses import replace
import unittest

from botend.services.simc_skill_passive_evidence import verify_passive_applications
from botend.tests.test_simc_passive_counterfactual_projection import captured_probes


class NativePassiveExecutionBoundaryTests(unittest.TestCase):
    def test_same_input_and_binary_cannot_authorize_different_health(self):
        on, off = captured_probes()
        off[0] = replace(off[0], target_health_percentage=20.0)
        with self.assertRaisesRegex(ValueError, "identity"):
            verify_passive_applications(on, off)

    def test_missing_or_invalid_health_is_not_a_default_high_health_run(self):
        for value in (None, True, 0, -1, 101, float("nan"), float("inf"), "100"):
            with self.subTest(health=value):
                on, off = captured_probes()
                on = replace(on, target_health_percentage=value)
                off = [replace(probe, target_health_percentage=value) for probe in off]
                with self.assertRaisesRegex(ValueError, "health"):
                    verify_passive_applications(on, off)

    def test_off_only_scenario_rejects_the_entire_comparison(self):
        on, off = captured_probes("frost_fever")
        action = off[0].payload["actors"][0]["actions"][0]
        extra = copy.deepcopy(action["scenarios"][0])
        extra["buffs"][0]["stacks"] += 1
        action["scenarios"].append(extra)
        with self.assertRaisesRegex(ValueError, "scenario_identity_set_changed"):
            verify_passive_applications(on, off)

    def test_off_only_component_is_not_invisible_to_validation(self):
        on, off = captured_probes()
        amount = off[0].payload["actors"][0]["actions"][0]["baseline"]
        self.assertIsNone(amount["tick"])
        amount["tick"] = copy.deepcopy(amount["direct"])
        with self.assertRaisesRegex(ValueError, "component_identity_set_changed"):
            verify_passive_applications(on, off)

    def test_output_receipt_keeps_target_health(self):
        on, off = captured_probes()
        proof = verify_passive_applications(on, off)
        self.assertEqual(proof.get("target_health_percentage"), 100.0)
        self.assertTrue(proof["components"])
