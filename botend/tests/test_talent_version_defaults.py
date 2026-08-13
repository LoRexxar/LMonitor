from django.test import TestCase

from botend.models import WowTalentVersion
from botend.wow.talents.default_versions import ensure_default_talent_versions
from botend.wow.talents.versioning import TalentVersionResolver


class TalentVersionDefaultsTests(TestCase):
    def test_bootstrap_keeps_121_as_only_active_simulator_version(self):
        ensure_default_talent_versions(WowTalentVersion)
        ensure_default_talent_versions(WowTalentVersion)

        old_version = WowTalentVersion.objects.get(key='retail-12.0.7')
        current_version = WowTalentVersion.objects.get(key='ptr-12.1.0')

        self.assertFalse(old_version.is_active)
        self.assertFalse(old_version.is_default_simulator)
        self.assertEqual(old_version.status, 'inactive')
        self.assertEqual(current_version.label, '正式服 12.1.0')
        self.assertEqual(current_version.branch, 'retail')
        self.assertTrue(current_version.is_active)
        self.assertTrue(current_version.is_default_simulator)
        self.assertEqual(current_version.status, 'active')
        self.assertEqual(
            TalentVersionResolver.get_default(TalentVersionResolver.USAGE_SIMULATOR),
            current_version,
        )
        self.assertEqual(
            list(TalentVersionResolver.list_active()),
            [current_version],
        )
