import importlib

from django.test import TestCase

from botend.models import SimcProfile


class _Apps:
    @staticmethod
    def get_model(app_label, model_name):
        assert (app_label, model_name) == ('botend', 'SimcProfile')
        return SimcProfile


class SimcProfileAttributeOverrideMigrationTests(TestCase):
    def _profile(self, name, **overrides):
        values = {
            'user_id': 1001,
            'name': name,
            'spec': 'warrior_fury',
            'player_config_mode': 'manual_equipment',
            'gear_strength': 5000,
            'gear_crit': 1000,
            'gear_haste': 2000,
            'gear_mastery': 3000,
            'gear_versatility': 4000,
        }
        values.update(overrides)
        return SimcProfile.objects.create(**values)

    def test_legacy_manual_values_are_cleared_but_other_modes_are_preserved(self):
        battlenet = self._profile('Battle.net', player_config_mode='battlenet')
        battlenet_legacy_tuple = self._profile(
            'Battle.net legacy tuple', player_config_mode='battlenet',
            gear_strength=93330, gear_crit=0, gear_haste=0,
            gear_mastery=0, gear_versatility=0,
        )
        old_manual_value = self._profile('Old manual value')
        system = self._profile(
            'System', user_id=None, source='simc_upstream',
            system_key='simc_upstream:warrior_fury',
        )
        legacy = self._profile(
            'Legacy', gear_strength=93330, gear_crit=0, gear_haste=0,
            gear_mastery=0, gear_versatility=0,
        )
        attribute_only = self._profile(
            'Attribute-only', player_config_mode='attribute_only',
            gear_strength=0, gear_crit=0, gear_haste=0,
            gear_mastery=0, gear_versatility=0,
        )

        migration = importlib.import_module(
            'botend.migrations.0148_clear_implicit_profile_attribute_overrides'
        )
        migration.clear_implicit_attribute_overrides(_Apps(), None)

        for profile in (system, legacy, old_manual_value):
            profile.refresh_from_db()
            self.assertIsNone(profile.gear_strength)
            self.assertIsNone(profile.gear_crit)
            self.assertIsNone(profile.gear_haste)
            self.assertIsNone(profile.gear_mastery)
            self.assertIsNone(profile.gear_versatility)

        battlenet.refresh_from_db()
        self.assertEqual(battlenet.gear_strength, 5000)
        self.assertEqual(battlenet.gear_versatility, 4000)
        battlenet_legacy_tuple.refresh_from_db()
        self.assertEqual(battlenet_legacy_tuple.gear_strength, 93330)
        self.assertEqual(battlenet_legacy_tuple.gear_crit, 0)

        attribute_only.refresh_from_db()
        self.assertEqual(attribute_only.gear_strength, 0)
        self.assertEqual(attribute_only.gear_crit, 0)
