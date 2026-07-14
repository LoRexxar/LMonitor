from types import SimpleNamespace
from unittest.mock import Mock, patch

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


class SpecDetailPlayerMonitorDifferentialUpdateTests(SimpleTestCase):
    def test_save_changed_profile_skips_unchanged_payload_and_timestamp(self):
        profile = SimpleNamespace(
            pk=1,
            class_name='Warrior',
            score=3000,
            gear_json=[{'item_id': 1}],
            last_updated='old-time',
            save=Mock(),
        )

        changed = SpecDetailPlayerMonitor._save_changed_profile(profile, {
            'class_name': 'Warrior',
            'score': 3000,
            'gear_json': [{'item_id': 1}],
            'last_updated': 'new-time',
        })

        self.assertFalse(changed)
        self.assertEqual(profile.last_updated, 'old-time')
        profile.save.assert_not_called()

    def test_save_changed_profile_updates_only_changed_fields_and_timestamp(self):
        profile = SimpleNamespace(
            pk=1,
            class_name='Warrior',
            score=3000,
            gear_json=[{'item_id': 1}],
            last_updated='old-time',
            save=Mock(),
        )

        changed = SpecDetailPlayerMonitor._save_changed_profile(profile, {
            'class_name': 'Warrior',
            'score': 3100,
            'gear_json': [{'item_id': 1}],
            'last_updated': 'new-time',
        })

        self.assertTrue(changed)
        self.assertEqual(profile.score, 3100)
        self.assertEqual(profile.last_updated, 'new-time')
        profile.save.assert_called_once_with(update_fields=['score', 'last_updated'])


class SpecDetailPlayerMonitorTalentPreserveTests(SimpleTestCase):
    @patch('botend.controller.plugins.portal.SpecDetailPlayerMonitor.TalentBuildCodeService.build_full_payload')
    def test_preserves_existing_apex_talents_when_refresh_payload_loses_apex(self, mock_build_payload):
        old_talents = [{'node_id': 137002, 'talent_id': 110412, 'points': 4}]
        new_talents = [{'node_id': 112292, 'talent_id': 112292, 'points': 2}]
        existing = SimpleNamespace(talents_json=old_talents)
        defaults = {
            'talents_json': new_talents,
            'talent_build_code': 'new-build-code',
        }

        def fake_payload(class_name, spec_name, talent_build_code, talents_json):
            if talents_json is old_talents:
                return [{'is_apex_talent': True, 'points': 4}]
            if talents_json is new_talents:
                return [{'is_apex_talent': True, 'points': 0}]
            return []

        mock_build_payload.side_effect = fake_payload

        preserved = SpecDetailPlayerMonitor._preserve_complete_talents_when_new_payload_is_downgrade(
            existing,
            defaults,
            'Warrior',
            'Fury',
        )

        self.assertTrue(preserved)
        self.assertIs(defaults['talents_json'], old_talents)
        self.assertEqual(defaults['talent_build_code'], 'new-build-code')

    @patch('botend.controller.plugins.portal.SpecDetailPlayerMonitor.TalentBuildCodeService.build_full_payload')
    def test_keeps_refresh_payload_when_it_already_has_apex_points(self, mock_build_payload):
        old_talents = [{'node_id': 137002, 'talent_id': 110412, 'points': 4}]
        new_talents = [{'node_id': 137002, 'talent_id': 110412, 'points': 4}]
        existing = SimpleNamespace(talents_json=old_talents)
        defaults = {'talents_json': new_talents}
        mock_build_payload.return_value = [{'is_apex_talent': True, 'points': 4}]

        preserved = SpecDetailPlayerMonitor._preserve_complete_talents_when_new_payload_is_downgrade(
            existing,
            defaults,
            'Warrior',
            'Fury',
        )

        self.assertFalse(preserved)
        self.assertIs(defaults['talents_json'], new_talents)
