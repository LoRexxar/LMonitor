from django.test import SimpleTestCase

from botend.controller.plugins.portal.SpecDetailPlayerMonitor import SpecDetailPlayerMonitor


class SpecDetailPlayerMonitorIdentityTests(SimpleTestCase):
    def test_profile_identity_key_ignores_case_and_accents(self):
        self.assertEqual(
            SpecDetailPlayerMonitor._profile_identity_key('EU', 'Kazzak', 'Bloodmäster'),
            SpecDetailPlayerMonitor._profile_identity_key('eu', 'kazzak', 'Bloodmastêr'),
        )

    def test_profile_identity_key_rejects_incomplete_identity(self):
        self.assertIsNone(SpecDetailPlayerMonitor._profile_identity_key('eu', '', 'Bloodmäster'))
