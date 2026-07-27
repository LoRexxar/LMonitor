import json
from pathlib import Path
from unittest import TestCase

from botend.services.midnight_trinket_catalog import parse_mid1_catalog


FIXTURE = Path(__file__).parent / 'fixtures' / 'mid1_trinkets.json'


class MidnightTrinketCatalogTests(TestCase):
    def test_fixture_is_audited_mid1_and_covers_all_specs_and_items(self):
        catalog = parse_mid1_catalog(json.loads(FIXTURE.read_text()))
        self.assertEqual(catalog.tier, 'MID1')
        self.assertEqual(len(catalog.spec_keys), 32)
        self.assertNotIn('evoker_augmentation', catalog.spec_keys)
        self.assertEqual(len({item.item_id for item in catalog.items}), 57)
        self.assertEqual(len(catalog.items), 66)
        self.assertGreater(len(catalog.variants), 57)

    def test_special_variants_are_controlled_and_deterministic(self):
        raw = json.loads(FIXTURE.read_text())
        catalog = parse_mid1_catalog(raw)
        crucible = [v for v in catalog.variants if v.item_id == 264507]
        self.assertEqual({v.option_key for v in crucible}, {
            'predation', 'sustenance', 'violence', 'predation+sustenance+violence+'
        })
        self.assertTrue(all(v.simc_options for v in crucible))
        self.assertEqual(
            [v.candidate_key for v in catalog.variants],
            [v.candidate_key for v in parse_mid1_catalog(raw).variants],
        )

    def test_rejects_wrong_tier_missing_spec_or_untrusted_directive(self):
        raw = json.loads(FIXTURE.read_text())
        raw['documents']['mage_fire']['simc_settings']['tier'] = 'S3'
        with self.assertRaises(ValueError):
            parse_mid1_catalog(raw)
        raw = json.loads(FIXTURE.read_text())
        del raw['documents']['mage_fire']
        with self.assertRaises(ValueError):
            parse_mid1_catalog(raw)
        raw = json.loads(FIXTURE.read_text())
        raw['documents']['mage_fire']['data']['Evil'] = {'298': 1}
        raw['documents']['mage_fire']['item_ids']['Evil'] = 250144
        raw['documents']['mage_fire']['data_sources']['Evil'] = 'x'
        with self.assertRaises(ValueError):
            parse_mid1_catalog(raw)
