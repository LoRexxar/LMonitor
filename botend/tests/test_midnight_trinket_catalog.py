import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from botend.services.midnight_trinket_catalog import (
    build_ptr_12_1_panel_payload,
    parse_ptr_12_1_catalog,
)
from botend.services.simc_benchmark_config import (
    _AGILITY_TRINKET_SPECS,
    _INTELLECT_TRINKET_SPECS,
    _STRENGTH_TRINKET_SPECS,
)


FIXTURE = Path(__file__).parent / 'fixtures' / 'ptr_12_1_trinkets.json'
EXPECTED_UNRESOLVED_IDS = {267631, 270172, 274370, 274371, 277735, 279190}
TARGET_LEVELS_BY_SOURCE = {
    'delve_or_open_world': (308, 321),
    'mythic_dungeon': (321, 334),
    'raid': (321, 334),
    'pvp': (321,),
}


class MidnightTrinketCatalogTests(TestCase):
    def load_catalog(self):
        return parse_ptr_12_1_catalog(json.loads(FIXTURE.read_text()))

    def test_fixture_freezes_exact_wowt_build_diff_and_all_new_trinket_ids(self):
        catalog = self.load_catalog()
        self.assertEqual(catalog.product, 'wowt')
        self.assertEqual(catalog.baseline_build, '12.0.5.67602')
        self.assertEqual(catalog.target_build, '12.1.0.69111')
        self.assertEqual(len(catalog.items), 49)
        self.assertEqual(len(catalog.unresolved_item_ids), 6)
        self.assertEqual(set(catalog.unresolved_item_ids), EXPECTED_UNRESOLVED_IDS)
        self.assertEqual(
            len({item.item_id for item in catalog.items} | set(catalog.unresolved_item_ids)),
            55,
        )
        self.assertTrue(all(item.inventory_type == 12 for item in catalog.items))

    def test_source_specific_target_levels_expand_each_complete_item(self):
        catalog = self.load_catalog()
        expected = {
            (item.item_id, item_level)
            for item in catalog.items
            for item_level in TARGET_LEVELS_BY_SOURCE[item.source_category]
        }
        self.assertEqual(
            {(variant.item_id, variant.item_level) for variant in catalog.variants},
            expected,
        )
        self.assertEqual(len(catalog.variants), len(expected))
        self.assertFalse(set(catalog.unresolved_item_ids) & {
            variant.item_id for variant in catalog.variants
        })
        self.assertEqual(
            {variant.item_level for variant in catalog.variants},
            {308, 321, 334},
        )

    def test_special_raid_item_gets_only_the_explicit_extra_344_variant(self):
        raw = json.loads(FIXTURE.read_text())
        raw['items'][0]['source_category'] = 'raid'
        raw['items'][0]['is_special_raid_item'] = True
        catalog = parse_ptr_12_1_catalog(raw)
        self.assertEqual(
            {variant.item_level for variant in catalog.variants if variant.item_id == 268292},
            {321, 334, 344},
        )
        self.assertFalse(any(
            variant.item_level == 344 and variant.item_id != 268292
            for variant in catalog.variants
        ))

    def test_payload_applies_every_candidate_to_all_enabled_damage_and_tank_specs(self):
        catalog = self.load_catalog()
        resources = {
            spec_key: {
                'apl': type('R', (), {'pk': 1})(),
                'template': type('R', (), {'pk': 2})(),
                'backend': type('R', (), {'pk': 3})(),
                'profile': type('R', (), {'pk': 4, 'name': 'Profile'})(),
            }
            for spec_key in catalog.spec_keys
        }
        with patch(
            'botend.services.simc_benchmark_config.resolve_default_benchmark_resources',
            return_value=resources,
        ):
            payload = build_ptr_12_1_panel_payload(catalog, user_id=1)

        self.assertEqual(len(payload['candidates']), len(catalog.variants))
        self.assertTrue(all(
            candidate['spec_keys'] == list(catalog.spec_keys)
            for candidate in payload['candidates']
        ))
        self.assertEqual(
            {candidate['params']['raw_value'] for candidate in payload['candidates']},
            {f'id={variant.item_id},ilevel={variant.item_level}' for variant in catalog.variants},
        )
        self.assertTrue(all(candidate['params']['benchmark_profile'] == {
            'kind': 'trinket_standard_reference',
            'item_level': 240,
        } for candidate in payload['candidates']))
        self.assertIn('source-specific target ilevels', payload['description'])
        mapped_specs = (
            _AGILITY_TRINKET_SPECS
            | _INTELLECT_TRINKET_SPECS
            | _STRENGTH_TRINKET_SPECS
        )
        self.assertEqual(mapped_specs, set(catalog.spec_keys))

    def test_rejects_wrong_build_inventory_type_duplicate_or_unresolved_overlap(self):
        raw = json.loads(FIXTURE.read_text())
        raw['source']['product'] = 'wow'
        with self.assertRaises(ValueError):
            parse_ptr_12_1_catalog(raw)

        raw = json.loads(FIXTURE.read_text())
        raw['items'][0]['inventory_type'] = 11
        with self.assertRaises(ValueError):
            parse_ptr_12_1_catalog(raw)

        raw = json.loads(FIXTURE.read_text())
        raw['items'].append(dict(raw['items'][0]))
        with self.assertRaises(ValueError):
            parse_ptr_12_1_catalog(raw)

        raw = json.loads(FIXTURE.read_text())
        raw['unresolved'][0]['item_id'] = raw['items'][0]['item_id']
        with self.assertRaises(ValueError):
            parse_ptr_12_1_catalog(raw)

    def test_rejects_invalid_source_category_or_non_raid_special_flag(self):
        raw = json.loads(FIXTURE.read_text())
        raw['items'][0]['source_category'] = 'made_up'
        with self.assertRaises(ValueError):
            parse_ptr_12_1_catalog(raw)

        raw = json.loads(FIXTURE.read_text())
        raw['items'][0]['source_category'] = 'pvp'
        raw['items'][0]['is_special_raid_item'] = True
        with self.assertRaises(ValueError):
            parse_ptr_12_1_catalog(raw)
