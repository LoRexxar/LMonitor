"""Native counterfactual proofs use captured outputs, not applied flags."""

import copy
from dataclasses import replace
import gzip
import json
from pathlib import Path
import unittest

from botend.services.simc_skill_passive_evidence import (
    PassiveProbeExport,
    verify_passive_applications,
)


def captured_probes(name="obliterate"):
    path = (
        Path(__file__).parent
        / "fixtures"
        / f"simc_passive_counterfactual_{name}.json.gz"
    )
    data = json.loads(gzip.decompress(path.read_bytes()))

    def probe(payload):
        # Archived fixtures were captured with the CLI default of 100% health.
        return PassiveProbeExport(
            payload,
            data["input_sha256"],
            data["binary_sha256"],
            target_health_percentage=100.0,
        )

    return probe(data["ordinary"]), [probe(p) for p in data["counterfactuals"]]


class NativePassiveCounterfactualProofTests(unittest.TestCase):
    def test_only_causally_proven_effect_is_authorized(self):
        on, off = captured_probes()
        before = copy.deepcopy(on.payload)
        result = verify_passive_applications(on, off)
        rows = result["components"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], ["obliterate_damage", 222024])
        self.assertEqual(rows[0]["effects"], [[137006, 0]])
        self.assertEqual(rows[0]["factor"], 1.02)
        self.assertEqual(on.payload, before)

    def test_single_source_does_not_need_an_unrelated_global_joint(self):
        on, off = captured_probes()
        off = [
            p
            for p in off
            if len(p.payload["parser_counterfactual"]["excluded_effects"]) == 1
        ]
        self.assertEqual(len(verify_passive_applications(on, off)["components"]), 1)

    def test_global_union_does_not_authorize_a_smaller_component_subset(self):
        on, off = captured_probes("frost_fever")
        result = verify_passive_applications(on, off)
        self.assertFalse(any(len(r["effects"]) > 1 for r in result["components"]))

    def test_real_exact_joint_authorizes_the_two_source_component(self):
        on, off = captured_probes("frost_fever")
        path = (
            Path(__file__).parent
            / "fixtures/simc_passive_frost_fever_exact_joint.json.gz"
        )
        extra = PassiveProbeExport(
            **json.loads(gzip.decompress(path.read_bytes())),
            target_health_percentage=100.0,
        )
        result = verify_passive_applications(on, off + [extra])
        row = next(r for r in result["components"] if r["scenario"] == "baseline")
        self.assertEqual(row["effects"], [[137006, 1], [137006, 12]])
        self.assertAlmostEqual(row["factor"], 1.605888)
        self.assertAlmostEqual(row["on_hit"] / row["factor"], row["off_hit"])

    def test_input_or_binary_identity_mismatch_is_rejected(self):
        for field in ("input_sha256", "binary_sha256"):
            on, off = captured_probes()
            off[0] = replace(off[0], **{field: "0" * 64})
            with self.assertRaisesRegex(ValueError, "identity"):
                verify_passive_applications(on, off)

    def test_amount_tampering_never_produces_authorization(self):
        on, off = captured_probes()
        for p in off:
            if p.payload["parser_counterfactual"]["excluded_effects"] == [
                {"source_spell_id": 137006, "effect_index": 0}
            ]:
                p.payload["actors"][0]["actions"][0]["baseline"]["direct"]["hit"] *= 1.1
        self.assertEqual(verify_passive_applications(on, off)["components"], [])

    def test_duplicates_and_diagnostic_ordinary_input_are_rejected(self):
        on, off = captured_probes()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            verify_passive_applications(on, off + [off[0]])
        with self.assertRaisesRegex(ValueError, "ordinary"):
            verify_passive_applications(off[0], off)
