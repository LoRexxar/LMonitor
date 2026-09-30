"""A diagnostic parser-disabled export must never be publishable data."""
from django.test import SimpleTestCase
from botend.models import SimcBackendBinary, SimcSkillDamageSnapshot
from botend.services.simc_skill_damage import SimcSkillDamageSnapshotService


class PassiveCounterfactualBoundaryTests(SimpleTestCase):
    def test_counterfactual_marker_is_rejected_before_normal_export_validation(self):
        service = SimcSkillDamageSnapshotService(
            SimcSkillDamageSnapshot(simc_revision='a' * 40, game_build='test'),
            backend=SimcBackendBinary(identifier='local-diagnostic'),
        )
        for marker in (None, {}, {
            'method': 'initialization_parser_only_v1',
            'excluded_effects': [{'source_spell_id': 137006, 'effect_index': 0}],
        }):
            with self.subTest(marker=marker):
                with self.assertRaisesRegex(ValueError, 'parser counterfactual'):
                    service._validate_export({'parser_counterfactual': marker})
