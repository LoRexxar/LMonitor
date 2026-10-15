"""Lossless central weapon stat projection regressions."""
from django.test import SimpleTestCase
from botend.services.wow_item_display import _normalize_stats, _format_number, STAT_LABELS, LAYOUT_STAT_ALIASES


class WeaponStatPrecisionTests(SimpleTestCase):
    def test_weapon_speed_survives_flat_and_nested_central_stats(self):
        expected = {'weapon_speed': 3.6, 'weapon_dps': 1585}
        self.assertEqual(_normalize_stats(expected), expected)
        self.assertEqual(_normalize_stats({'weapon': expected}), expected)
        self.assertEqual(STAT_LABELS['weapon_speed'], '武器速度')
        self.assertIn('Speed', LAYOUT_STAT_ALIASES['weapon_speed'])

    def test_weapon_decimal_values_are_not_rounded_for_display(self):
        self.assertEqual(_format_number(4.8125), '4.8125')
        self.assertEqual(_format_number(3.6), '3.6')
        self.assertEqual(_format_number(1585), '1,585')
        self.assertEqual(_format_number(0.3), '0.3')
