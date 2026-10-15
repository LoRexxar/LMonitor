"""Native magical wand damage wording must retain damage/DPS/speed."""
import json
from pathlib import Path
from django.test import SimpleTestCase
from botend.services.gear_builder_catalog_source import _tooltip_details
from botend.services.wow_item_difficulty_import import _lossless_attributes

class WandAttributeTests(SimpleTestCase):
    def test_real_arcane_wand_payload_keeps_all_weapon_attributes(self):
        p = json.loads((Path(__file__).parent / 'fixtures/wow_item_wand_tooltip_65172.json').read_text())['payload']
        stats, primary = _lossless_attributes(p, _tooltip_details(p))
        self.assertEqual((stats['min_damage'], stats['max_damage'], stats['weapon_dps'], stats['weapon_speed']), (3, 6, 2.3, 2))
        self.assertEqual(stats['intellect'], 24)
