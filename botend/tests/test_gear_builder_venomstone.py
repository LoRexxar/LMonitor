"""毒液石部位、装等、数值与增量导入回归。"""
from copy import deepcopy
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, SimpleTestCase

from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.services.gear_builder import serialize_variant
from botend.services.gear_builder_catalog_source import CurrentGearCatalogSource, SEASON_LEVEL_PROFILES, _tooltip_details
from botend.services.gear_builder_venomstone import upgraded_variant, apply_tooltip, preserve_localized_effects


class VenomstoneRulesTests(SimpleTestCase):
    def test_only_eligible_drop_slots_receive_two_extra_variants(self):
        for inventory_type in (1, 2, 11, 12, 13, 14, 17, 23):
            with self.subTest(inventory_type=inventory_type):
                item = {'inventory_type': inventory_type, 'metadata': {}, 'variants': []}
                CurrentGearCatalogSource._add_drop_variants(item, SEASON_LEVEL_PROFILES['mid2'], 'raid', [])
                extra = [row for row in item['variants'] if row['track_rank'] > 6]
                self.assertEqual([(v['upgrade_track'], v['track_rank'], v['track_max_rank'], v['item_level'], v['bonus_ids']) for v in extra],
                                 [] if inventory_type in (1, 11) else [('hero', 8, 6, 328, [12848]), ('myth', 8, 6, 340, [12856])])

    def test_delve_and_special_mythic_limits_remain(self):
        item = {'inventory_type': 12, 'metadata': {}, 'variants': []}
        CurrentGearCatalogSource._add_drop_variants(item, SEASON_LEVEL_PROFILES['mid2'], 'delve', [])
        self.assertNotIn('myth', {row['upgrade_track'] for row in item['variants']})
        self.assertEqual(max(row['item_level'] for row in item['variants']), 328)
        item['variants'] = []
        CurrentGearCatalogSource._add_drop_variants(item, SEASON_LEVEL_PROFILES['mid2'], 'raid', [], special_mythic=True)
        self.assertEqual(max(row['item_level'] for row in item['variants']), 344)
        self.assertEqual(sum(row['track_rank'] == 9 for row in item['variants']), 1)

    def test_incomplete_or_crafted_track_is_not_upgraded(self):
        base = {'key': '英雄', 'type': 'drop_equipment', 'upgrade_track': 'hero', 'track_rank': 6,
                'track_max_rank': 6, 'item_level': 321, 'bonus_ids': [12846, 1808],
                'stats': {'strength': 100}, 'effects': [{'description_zh': '旧数值'}],
                'metadata': {'primary_stat_amount': 100, 'primary_stat_values': {'strength': 100}, 'two_handed': True}}
        result = upgraded_variant(17, base)
        self.assertEqual(result['bonus_ids'], [1808, 12848])
        self.assertEqual((result['stats'], result['effects']), ({}, []))
        self.assertNotIn('primary_stat_amount', result['metadata'])
        self.assertTrue(result['metadata']['two_handed'])
        self.assertEqual(base['stats'], {'strength': 100})
        for changes in ({'type': 'crafted_equipment'}, {'track_rank': 5}, {'item_level': 318}, {'track_max_rank': 8},
                        {'upgrade_track': 'myth', 'item_level': 334, 'sources': [{'type': 'delve'}]}):
            self.assertIsNone(upgraded_variant(17, dict(base, **changes)))

    def test_tooltip_must_match_level_and_include_required_effects(self):
        variant = {'item_level': 340}
        with self.assertRaisesMessage(ValueError, '目标装等'):
            apply_tooltip(variant, {'item_level': 334, 'stats': {'haste': 100}})
        with self.assertRaisesMessage(ValueError, '装备特效'):
            apply_tooltip(variant, {'item_level': 340, 'stats': {'haste': 100}}, requires_effect=True)
        details = _tooltip_details({'tooltip': '<span>Item Level <!--ilvl-->340</span><!--rf--><br>+190 Intellect<!--nameDescStats-->'})
        apply_tooltip(variant, details)
        self.assertEqual(variant['stats'], {'intellect': 190})

    def test_chinese_effect_binds_new_values_and_rejects_changed_template(self):
        old = [{'spell_id': 1, 'trigger_type': 1, 'template': 'Gain $s1 Haste for $d.',
                'template_zh': '获得$s1急速，持续$d。', 'description_zh': '装备：获得100急速，持续10秒。'}]
        variant = {'effects': [{'description': 'Equip: Gain 150 Haste for 10 sec.'}]}
        preserve_localized_effects(variant, old)
        self.assertEqual(variant['effects'][0]['description_zh'], '装备：获得150急速，持续10秒。')
        with self.assertRaisesMessage(ValueError, '中文模板'):
            preserve_localized_effects({'effects': [{'description': 'Equip: Changed mechanics.'}]}, old)


class VenomstoneImportTests(TestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(season_key='mn-s2', season_name='至暗之夜第二赛季',
            is_active=True, mplus_zone_id=1, raid_zone_id=2, gear_batch_key='毒液石测试',
            gear_sync_report={'catalog_rules': {'socket_additions': [{'slot': 'head'}]}})
        self.item = WowItemSnapshot.objects.create(item_id=280799, name_zh='测试武器', inventory_type=17, slot_key='main_hand')
        self.base = WowItemVariantSnapshot.objects.create(item=self.item, season=self.season, batch_key=self.season.gear_batch_key,
            variant_key='raid-myth-6-334', variant_type='drop_equipment', item_level=334, upgrade_track='myth',
            track_rank=6, track_max_rank=6, bonus_ids=[12854, 1808], compatible_slots=['main_hand'],
            socket_count=1, socket_types=['prismatic'], stats_json={'strength': 100},
            effects_json=[{'description_zh': '装备：旧数值。'}], source_json=[{'type': 'raid'}],
            metadata={'primary_stat_values': {'strength': 100}, 'two_handed': True})
        self.details = {'item_level': 340, 'stats': {'strength': 120}, 'effects': [{'description_zh': '装备：提高150急速。'}]}

    def execute(self, **options):
        call_command('update_gear_builder_venomstone', stdout=StringIO(), **options)

    @patch.object(CurrentGearCatalogSource, 'venomstone_tooltip')
    def test_preview_neither_fetches_nor_writes(self, fetch):
        self.execute()
        fetch.assert_not_called()
        self.assertEqual(WowItemVariantSnapshot.objects.count(), 1)

    @patch.object(CurrentGearCatalogSource, 'venomstone_tooltip')
    def test_apply_is_idempotent_and_preserves_original_and_rules(self, fetch):
        fetch.return_value = self.details
        original = deepcopy(self.base.stats_json)
        self.execute(apply=True)
        row = WowItemVariantSnapshot.objects.get(track_rank=8)
        serialized = serialize_variant(row, 'Warrior', 'Fury')
        self.assertEqual(serialized['stats']['strength'], 120)
        self.assertEqual(serialized['bonus_ids'], [1808, 12856])
        self.assertEqual(serialized['effects'][0]['description_zh'], '装备：提高150急速。')
        self.assertEqual((row.socket_count, row.socket_types, row.source_json), (1, ['prismatic'], [{'type': 'raid'}]))
        self.execute(apply=True)
        self.assertEqual(WowItemVariantSnapshot.objects.get(track_rank=8).pk, row.pk)
        self.assertEqual(WowItemVariantSnapshot.objects.count(), 2)
        self.base.refresh_from_db()
        self.season.refresh_from_db()
        self.assertEqual(self.base.stats_json, original)
        self.assertEqual(self.season.gear_sync_report['catalog_rules'], {'socket_additions': [{'slot': 'head'}]})

    @patch.object(CurrentGearCatalogSource, 'venomstone_tooltip')
    def test_incorrect_remote_level_does_not_write(self, fetch):
        fetch.return_value = dict(self.details, item_level=334)
        with self.assertRaisesMessage(CommandError, '数据库未修改'):
            self.execute(apply=True)
        self.assertEqual(WowItemVariantSnapshot.objects.count(), 1)

    def test_other_season_is_rejected(self):
        self.season.season_key = 'mn-s1'
        self.season.save(update_fields=['season_key'])
        with self.assertRaisesMessage(CommandError, '第二赛季'):
            self.execute(apply=True)
