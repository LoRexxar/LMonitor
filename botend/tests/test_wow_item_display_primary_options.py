"""Regression for journal tooltips omitting variant primary-stat alternatives."""
from copy import deepcopy

from django.test import TestCase

from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.services.journal_tooltip import cached_tooltip
from botend.services.wow_item_display import item_display_metadata, load_item_tooltip_metadata


class JournalPrimaryOptionsTests(TestCase):
    def test_journal_projects_primary_alternatives_without_adding_them(self):
        season = SeasonMeta.objects.create(
            season_key='journal-primary-options', season_name='Journal primary options',
            is_active=True, gear_batch_key='journal-primary-options',
            mplus_zone_id=1, raid_zone_id=2,
        )
        # Captured production variants: empty stats_json, alternatives in metadata.
        cases = (
            (280617, '圣光使者之盾碎片', {'agility': 121, 'strength': 121},
             'stragi', '+121 力量／敏捷'),
            (281215, '扭曲恐兽触须', {'agility': 121, 'strength': 121, 'intellect': 121},
             'stragiint', '+121 力量／敏捷／智力'),
        )
        for item_id, name, values, stat_key, line in cases:
            with self.subTest(item_id=item_id):
                item = WowItemSnapshot.objects.create(
                    item_id=item_id, name_zh=name, catalog_type='equipment',
                    slot_key='trinket', inventory_type=12,
                )
                variant = WowItemVariantSnapshot.objects.create(
                    item=item, season=season, batch_key=season.gear_batch_key,
                    data_branch='ptr', variant_key='ptr-kithix-champion-1',
                    variant_type='drop_equipment', item_level=292,
                    stats_json={}, effects_json=[{'description_zh': '装备：测试特效。'}],
                    metadata={'ptr_preview': True, 'primary_stat_values': values},
                )
                original = deepcopy((variant.stats_json, variant.metadata))
                request = {'item_id': item_id, 'allow_default_variant': True,
                           'default_variant_order': 'lowest'}
                display = load_item_tooltip_metadata([request])[0]
                journal = cached_tooltip(
                    'item', item_id, 14, '12.1.5.69594', use_current_catalog=True,
                )
                self.assertEqual(display['stats'], {stat_key: 121})
                self.assertEqual(display['stat_lines'], [line])
                self.assertIn(line, display['tooltip'])
                self.assertEqual(journal['stats'], [line])
                self.assertTrue(journal['complete'])
                self.assertEqual(display['variant_id'], variant.pk)
                self.assertEqual(display['effects'], ['装备：测试特效。'])
                for primary in values:
                    selected = load_item_tooltip_metadata([{**request, 'primary_stat': primary}])[0]
                    self.assertEqual(selected['stats'], {primary: 121})
                variant.refresh_from_db()
                self.assertEqual((variant.stats_json, variant.metadata), original)

    def test_generic_primary_projection_preserves_known_stats_and_unknown_amounts(self):
        cases = (
            ({'agility': 121, 'intellect': 121}, {}, {'agiint': 121}),
            ({'strength': 121, 'intellect': 121}, {}, {'strint': 121}),
            ({'intellect': 121}, {'stamina': 2406}, {'intellect': 121, 'stamina': 2406}),
            ({'strength': 121, 'agility': 121}, {'stragi': 128}, {'stragi': 128}),
            ({'strength': 121, 'agility': 121}, {'strength': 128}, {'strength': 128}),
            ({'strength': 121, 'agility': 122}, {}, {}),
            ({'strength': 0, 'agility': 'unknown'}, {}, {}),
        )
        for values, stats, expected in cases:
            with self.subTest(values=values, stats=stats):
                item = WowItemSnapshot(item_id=1, catalog_type='equipment')
                variant = WowItemVariantSnapshot(
                    item=item, item_level=292, stats_json=deepcopy(stats),
                    metadata={'primary_stat_values': deepcopy(values)},
                )
                original = deepcopy((variant.stats_json, variant.metadata))
                self.assertEqual(item_display_metadata(1, item, variant=variant)['stats'], expected)
                self.assertEqual((variant.stats_json, variant.metadata), original)
        # An untyped amount alone does not prove which primary options exist.
        variant.metadata = {'primary_stat_amount': 121}
        variant.stats_json = {}
        self.assertEqual(item_display_metadata(1, item, variant=variant)['stats'], {})
