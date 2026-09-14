from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.services.journal_tooltip import cached_tooltip, tooltip


class JournalLocalTooltipTests(TestCase):
    def setUp(self):
        cache.clear()
        season = SeasonMeta.objects.create(
            season_key='ptr-journal-tooltip-test',
            season_name='PTR Journal Tooltip Test',
            is_active=True,
            mplus_zone_id=1,
            raid_zone_id=2,
            game_build='12.1.0.69814',
            gear_batch_key='ptr-journal-tooltip-batch',
            gear_sync_status='ready',
        )
        self.season = season
        item = WowItemSnapshot.objects.create(
            item_id=281235,
            name="Voidweaver's Vestments",
            icon='inv_chest_cloth_raidpriestethereal_d_01',
            quality=4,
            catalog_type='equipment',
            inventory_type=20,
            slot_key='chest',
            item_class_id=4,
            item_subclass_id=1,
            armor_type='布甲',
            allowable_class_mask=-1,
            metadata={'ptr_preview': True},
        )
        WowItemVariantSnapshot.objects.create(
            season=season,
            batch_key=season.gear_batch_key,
            game_build='12.1.5.69594',
            item=item,
            variant_key='ptr-292',
            variant_type='drop_equipment',
            item_level=292,
            compatible_slots=['chest'],
            stats_json={
                'stragiint': 128,
                'stamina': 2406,
                'crit': 51,
                'mastery': 116,
            },
            effects_json=[{
                'description_zh': '装备：攻击有几率爆发虚空能量。',
                'game_build': '12.1.5.69594',
            }],
            source_json=[{'type': 'raid'}],
            metadata={
                'ptr_preview': True,
                'stats_status': 'exact_build_simc',
                'effects_status': 'exact_build_db2_simc',
                'game_build': '12.1.5.69594',
            },
        )
        complete_metadata = {
            'ptr_preview': True,
            'stats_status': 'exact_build_simc',
            'effects_status': 'exact_build_db2_simc',
            'game_build': '12.1.5.69594',
        }
        complete_effects = [{
            'description_zh': '装备：不应选择旧批次。',
            'game_build': '12.1.5.69594',
            'unresolved_tokens': [],
        }]
        WowItemVariantSnapshot.objects.create(
            season=season,
            batch_key='stale-ptr-batch',
            game_build='12.1.5.69594',
            item=item,
            variant_key='ptr-stale-290',
            variant_type='drop_equipment',
            item_level=290,
            stats_json={'intellect': 100},
            effects_json=complete_effects,
            source_json=[{'type': 'raid'}],
            metadata=complete_metadata,
        )
        WowItemVariantSnapshot.objects.create(
            season=season,
            batch_key=season.gear_batch_key,
            game_build='12.1.5.69594',
            item=item,
            variant_key='ptr-incomplete-291',
            variant_type='drop_equipment',
            item_level=291,
            stats_json={'intellect': 110},
            effects_json=[],
            source_json=[{'type': 'raid'}],
            metadata=complete_metadata,
        )

    def test_exact_build_ptr_item_uses_local_reference_variant(self):
        result = cached_tooltip('item', 281235, 14, '12.1.5.69594')

        self.assertEqual(result['name'], "Voidweaver's Vestments")
        self.assertEqual(result['item_level'], 292)
        self.assertEqual(result['source'], 'LMonitor PTR DB2 + SimulationCraft')
        self.assertIn('+128 力量／敏捷／智力', result['stats'])
        self.assertIn('+2,406 耐力', result['stats'])
        self.assertEqual(result['effects'], ['装备：攻击有几率爆发虚空能量。'])
        self.assertTrue(result['complete'])

    def test_live_item_uses_current_exact_build_catalog_variant(self):
        item = WowItemSnapshot.objects.create(
            item_id=251127,
            name_zh='啃咬之护臂',
            icon='inv_bracer_cloth_raidmageethereal_d_01',
            catalog_type='equipment',
            metadata={},
        )
        WowItemVariantSnapshot.objects.create(
            season=self.season,
            batch_key=self.season.gear_batch_key,
            game_build='12.1.0.69587',
            item=item,
            variant_key='live-292',
            variant_type='drop_equipment',
            item_level=292,
            stats_json={
                'intellect': 72,
                'stamina': 1353,
                'crit': 37,
                'haste': 57,
                'armor': 43,
            },
            effects_json=[],
            metadata={},
        )

        result = cached_tooltip('item', 251127, 1, '12.1.0.69587')

        self.assertEqual(result['source'], 'LMonitor 装备目录')
        self.assertEqual(result['item_level'], 292)
        self.assertIn('+72 智力', result['stats'])
        self.assertIn('+1,353 耐力', result['stats'])
        self.assertEqual(result['effects'], [])

    def test_live_effect_only_item_uses_current_exact_build_catalog_variant(self):
        item = WowItemSnapshot.objects.create(
            item_id=250243,
            name_zh='魔力之心的联结烈焰',
            catalog_type='equipment',
            metadata={},
        )
        WowItemVariantSnapshot.objects.create(
            season=self.season,
            batch_key=self.season.gear_batch_key,
            game_build='12.1.0.69587',
            item=item,
            variant_key='live-effect-292',
            variant_type='drop_equipment',
            item_level=292,
            stats_json={},
            effects_json=[{'description_zh': '装备：攻击有几率获得力量。'}],
            metadata={},
        )

        result = cached_tooltip('item', 250243, 1, '12.1.0.69587')

        self.assertEqual(result['source'], 'LMonitor 装备目录')
        self.assertEqual(result['stats'], [])
        self.assertEqual(result['effects'], ['装备：攻击有几率获得力量。'])

    def test_uses_authoritative_active_season_when_multiple_rows_are_active(self):
        item = WowItemSnapshot.objects.create(
            item_id=251500,
            name_zh='当前赛季装备',
            catalog_type='equipment',
        )
        self.season.gear_synced_at = timezone.now()
        self.season.save(update_fields=['gear_synced_at'])
        stale = SeasonMeta.objects.create(
            season_key='stale-active-tooltip-test',
            season_name='Stale Active Tooltip Test',
            is_active=True,
            mplus_zone_id=99,
            raid_zone_id=100,
            game_build='12.1.0.69587',
            gear_batch_key='stale-live-batch',
            gear_sync_status='ready',
            gear_synced_at=timezone.now() - timedelta(days=1),
        )
        WowItemVariantSnapshot.objects.create(
            season=stale,
            batch_key=stale.gear_batch_key,
            game_build='12.1.0.69587',
            item=item,
            variant_key='stale-live-280',
            variant_type='drop_equipment',
            item_level=280,
            stats_json={'intellect': 1},
            effects_json=[],
        )
        WowItemVariantSnapshot.objects.create(
            season=self.season,
            batch_key=self.season.gear_batch_key,
            game_build='12.1.0.69587',
            item=item,
            variant_key='current-live-292',
            variant_type='drop_equipment',
            item_level=292,
            stats_json={'intellect': 72},
            effects_json=[],
        )

        result = cached_tooltip('item', 251500, 1, '12.1.0.69587')

        self.assertEqual(result['item_level'], 292)
        self.assertIn('+72 智力', result['stats'])

    def test_global_ptr_item_marker_does_not_taint_live_variant(self):
        item = WowItemSnapshot.objects.get(item_id=281235)
        WowItemVariantSnapshot.objects.create(
            season=self.season,
            batch_key=self.season.gear_batch_key,
            game_build='12.1.0.69587',
            item=item,
            variant_key='live-same-item-292',
            variant_type='drop_equipment',
            item_level=292,
            stats_json={'intellect': 72},
            effects_json=[],
            metadata={},
        )

        result = cached_tooltip('item', 281235, 1, '12.1.0.69587')

        self.assertEqual(result['source'], 'LMonitor 装备目录')
        self.assertIn('+72 智力', result['stats'])

    def test_different_build_does_not_reuse_ptr_snapshot(self):
        self.assertIsNone(cached_tooltip('item', 281235, 14, '12.1.0.69814'))

    @patch('botend.services.journal_tooltip.requests.Session')
    def test_missing_item_variant_never_falls_back_to_external_fetch(self, session):
        with self.assertRaisesRegex(ValueError, '中央装备目录'):
            tooltip('item', 999999, 14, '12.1.5.69594')

        session.assert_not_called()
