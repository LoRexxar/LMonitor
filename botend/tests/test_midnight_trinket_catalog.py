import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from botend.services.midnight_trinket_catalog import build_mid1_panel_payload, parse_mid1_catalog
from botend.services.simc_benchmark_config import (
    _AGILITY_TRINKET_SPECS, _INTELLECT_TRINKET_SPECS, _STRENGTH_TRINKET_SPECS,
)


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

    def test_payload_keeps_distinct_labels_for_stat_bonus_variants(self):
        catalog = parse_mid1_catalog(json.loads(FIXTURE.read_text()))
        with patch(
            'botend.services.simc_benchmark_config.resolve_default_benchmark_resources',
            return_value={spec_key: {'apl': type('R', (), {'pk': 1})(),
                                    'template': type('R', (), {'pk': 2})(),
                                    'backend': type('R', (), {'pk': 3})(),
                                    'profile': type('R', (), {'pk': 4, 'name': 'Profile'})()}
                          for spec_key in catalog.spec_keys},
        ):
            payload = build_mid1_panel_payload(catalog, user_id=1)

        drum_labels = {
            candidate['label'] for candidate in payload['candidates']
            if candidate['params']['raw_value'].startswith('id=248583,')
        }
        self.assertEqual(len(drum_labels), 4)
        self.assertEqual({label.rpartition(' · ')[2] for label in drum_labels}, {
            '暴击', '急速', '精通', '全能',
        })

    def test_payload_freezes_standard_trinket_reference_strategy(self):
        catalog = parse_mid1_catalog(json.loads(FIXTURE.read_text()))
        with patch(
            'botend.services.simc_benchmark_config.resolve_default_benchmark_resources',
            return_value={spec_key: {'apl': type('R', (), {'pk': 1})(),
                                    'template': type('R', (), {'pk': 2})(),
                                    'backend': type('R', (), {'pk': 3})(),
                                    'profile': type('R', (), {'pk': 4, 'name': 'Profile'})()}
                          for spec_key in catalog.spec_keys},
        ):
            payload = build_mid1_panel_payload(catalog, user_id=1)

        self.assertTrue(payload['candidates'])
        self.assertTrue(all(candidate['params']['benchmark_profile'] == {
            'kind': 'trinket_standard_reference',
            'item_level': 240,
        } for candidate in payload['candidates']))
        mapped_specs = (
            _AGILITY_TRINKET_SPECS
            | _INTELLECT_TRINKET_SPECS
            | _STRENGTH_TRINKET_SPECS
        )
        self.assertEqual(mapped_specs, set(catalog.spec_keys))
        self.assertEqual(
            sum(map(len, (
                _AGILITY_TRINKET_SPECS,
                _INTELLECT_TRINKET_SPECS,
                _STRENGTH_TRINKET_SPECS,
            ))),
            len(mapped_specs),
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
