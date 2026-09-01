import json
import tempfile
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from botend.models import (
    SeasonMeta,
    SimcMasteryCoefficient,
    SimcSecondaryStatRule,
    WowItemSnapshot,
    WowItemVariantSnapshot,
)
from botend.management.commands.sync_gear_builder_catalog import Command as SyncGearBuilderCatalogCommand
from botend.services.gear_builder import stats_for_identity
from botend.services.gear_builder_catalog_source import CatalogSourceError, CurrentGearCatalogSource, _tooltip_details
from botend.services.gear_builder_icon_sync import GearBuilderIconSync


class GearBuilderTestDataMixin:
    def setUp(self):
        super().setUp()
        SimcSecondaryStatRule.objects.update_or_create(
            class_name='warrior',
            defaults={
                'crit_per_percent': 46,
                'haste_per_percent': 44,
                'mastery_per_percent': 46,
                'versatility_per_percent': 54,
            },
        )
        SimcMasteryCoefficient.objects.update_or_create(
            spec='fury', defaults={'mastery_coefficient': 1.4},
        )
        self.season = SeasonMeta.objects.create(
            season_key='midnight-s2-test',
            season_name='午夜 S2 测试',
            is_active=True,
            mplus_zone_id=1,
            raid_zone_id=2,
            game_build='12.1.0.99999',
            gear_batch_key='test-batch',
            gear_sync_status='ready',
            gear_sync_report={
                'catalog_rules': {
                    'socket_additions': [
                        {'slot': 'head', 'max_additional': 1, 'source': 'great_vault'},
                        {'slot': 'wrists', 'max_additional': 1, 'source': 'great_vault'},
                        {'slot': 'waist', 'max_additional': 1, 'source': 'great_vault'},
                        {'slot': 'neck', 'max_additional': 1, 'source': 'socket_item'},
                        {'slot': 'finger', 'max_additional': 1, 'source': 'socket_item'},
                    ],
                    'add_socket_item': {'name': '辉耀珠宝钳'},
                },
            },
        )
        self.helm = WowItemSnapshot.objects.create(
            item_id=10001,
            name='Rift Helm',
            name_zh='裂隙头盔',
            description_zh='装备：测试装备特效。',
            icon='inv_helmet_01',
            quality=4,
            catalog_type='equipment',
            slot_key='head',
            item_class_id=4,
            item_subclass_id=4,
            inventory_type=1,
            armor_type='板甲',
            eligible_specs=['Warrior:Fury'],
        )
        self.hero = WowItemVariantSnapshot.objects.create(
            item=self.helm,
            season=self.season,
            batch_key='test-batch',
            game_build='12.1.0.99999',
            variant_key='hero-2',
            variant_type=WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT,
            item_level=710,
            upgrade_track='hero',
            track_rank=2,
            track_max_rank=6,
            compatible_slots=['head'],
            socket_count=1,
            socket_types=['prismatic'],
            stats_json={'strength': 900, 'stamina': 4000, 'crit': 315, 'haste': 315},
            effects_json=[{'description_zh': '装备：攻击有几率开启裂隙。'}],
            source_json=[{'type': 'raid', 'instance_zh': '测试团本', 'difficulty_zh': '英雄'}],
        )
        self.myth = WowItemVariantSnapshot.objects.create(
            item=self.helm,
            season=self.season,
            batch_key='test-batch',
            game_build='12.1.0.99999',
            variant_key='myth-1',
            variant_type=WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT,
            item_level=720,
            upgrade_track='myth',
            track_rank=1,
            track_max_rank=6,
            compatible_slots=['head'],
            stats_json={'strength': 1000, 'stamina': 4500, 'crit': 350, 'haste': 350},
            source_json=[{'type': 'raid', 'instance_zh': '测试团本', 'difficulty_zh': '史诗'}],
        )
        self.crafted_item = WowItemSnapshot.objects.create(
            item_id=10002,
            name='Forged Helm',
            name_zh='锻造头盔',
            catalog_type='crafted_equipment',
            slot_key='head',
            item_class_id=4,
            item_subclass_id=4,
            inventory_type=1,
            armor_type='板甲',
        )
        self.crafted = WowItemVariantSnapshot.objects.create(
            item=self.crafted_item,
            season=self.season,
            batch_key='test-batch',
            game_build='12.1.0.99999',
            variant_key='crafted-q5-720',
            variant_type=WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT,
            item_level=720,
            crafting_quality=5,
            compatible_slots=['head'],
            stats_json={'strength': 1000, 'stamina': 4500, 'secondary_total': 700},
            crafting_options={
                'stat_count': 2,
                'stat_pool': ['crit', 'haste', 'mastery', 'versatility'],
                'secondary_total': 700,
            },
            source_json=[{'type': 'crafted', 'profession_zh': '锻造'}],
        )
        self.embellishment_item = WowItemSnapshot.objects.create(
            item_id=10003,
            name='Rift Lining',
            name_zh='裂隙内衬',
            catalog_type='embellishment',
        )
        self.embellishment = WowItemVariantSnapshot.objects.create(
            item=self.embellishment_item,
            season=self.season,
            batch_key='test-batch',
            variant_key='embellishment',
            variant_type=WowItemVariantSnapshot.TYPE_EMBELLISHMENT,
            compatible_slots=['head', 'chest'],
            effects_json=[{'description_zh': '装备：获得裂隙之力。'}],
            unique_group='embellishment-limit',
            max_equipped=2,
            source_json=[{'type': 'profession', 'profession_zh': '裁缝'}],
        )
        self.gem_item = WowItemSnapshot.objects.create(
            item_id=10004,
            name='Quick Gem',
            name_zh='迅捷宝石',
            catalog_type='gem',
            quality=4,
        )
        self.gem = WowItemVariantSnapshot.objects.create(
            item=self.gem_item,
            season=self.season,
            batch_key='test-batch',
            variant_key='gem-q3',
            variant_type=WowItemVariantSnapshot.TYPE_GEM,
            crafting_quality=2,
            compatible_slots=['head', 'finger'],
            stats_json={'haste': 120},
            metadata={'simc_name': 'quick_gem_2'},
            source_json=[{'type': 'profession', 'profession_zh': '珠宝加工'}],
        )
        self.enchant_item = WowItemSnapshot.objects.create(
            item_id=10005,
            name='Helm Enchant',
            name_zh='头部附魔',
            catalog_type='enchant',
            enchantment_id=8001,
            quality=4,
        )
        self.enchant = WowItemVariantSnapshot.objects.create(
            item=self.enchant_item,
            season=self.season,
            batch_key='test-batch',
            variant_key='enchant-q3',
            variant_type=WowItemVariantSnapshot.TYPE_ENCHANT,
            crafting_quality=2,
            compatible_slots=['head'],
            stats_json={'crit': 100},
            metadata={'simc_name': 'helm_enchant_2'},
            source_json=[{'type': 'profession', 'profession_zh': '附魔'}],
        )


class GearBuilderApiTests(GearBuilderTestDataMixin, TestCase):
    def test_page_and_bootstrap_expose_current_catalog(self):
        page = self.client.get('/portal/gear-builder/')
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, '职业配装器')
        self.assertContains(page, 'gear_builder.js')

        response = self.client.get('/portal/api/gear-builder/bootstrap/')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertTrue(payload['catalog']['available'])
        self.assertEqual(payload['catalog']['batch_key'], 'test-batch')
        self.assertEqual(len(payload['slots']), 16)
        self.assertEqual(
            [row['slot'] for row in payload['rules']['socket_additions']],
            ['head', 'wrists', 'waist'],
        )
        conversion = payload['rules']['secondary_stat_conversion']['Warrior:Fury']
        self.assertEqual(conversion['crit_per_percent'], 46)
        self.assertEqual(conversion['haste_per_percent'], 44)
        self.assertEqual(conversion['mastery_per_percent'], 46)
        self.assertEqual(conversion['versatility_per_percent'], 54)
        self.assertEqual(conversion['mastery_coefficient'], 1.4)

    def test_catalog_groups_legal_variants_and_filters_spec_slot_and_source(self):
        response = self.client.get('/portal/api/gear-builder/catalog/', {
            'class': 'Warrior', 'spec': 'Fury', 'slot': 'head', 'source': 'raid',
        })
        self.assertEqual(response.status_code, 200)
        rows = response.json()['items']
        self.assertEqual([row['item_id'] for row in rows], [10001])
        self.assertEqual({row['track'] for row in rows[0]['variants']}, {'hero', 'myth'})

        wrong_spec = self.client.get('/portal/api/gear-builder/catalog/', {
            'class': 'Mage', 'spec': 'Fire', 'slot': 'head',
        }).json()
        self.assertNotIn(10001, [row['item_id'] for row in wrong_spec['items']])

        self.helm.eligible_specs = []
        self.helm.allowable_class_mask = 128
        self.helm.save(update_fields=['eligible_specs', 'allowable_class_mask'])
        wrong_class_mask = self.client.get('/portal/api/gear-builder/catalog/', {
            'class': 'Warrior', 'spec': 'Fury', 'slot': 'head',
        }).json()
        self.assertNotIn(10001, [row['item_id'] for row in wrong_class_mask['items']])

    def test_catalog_normalizes_legacy_jewelry_to_one_or_two_native_sockets(self):
        neck = WowItemSnapshot.objects.create(
            item_id=10012, name='Legacy Neck', catalog_type='equipment', slot_key='neck',
            item_class_id=4, inventory_type=2,
        )
        WowItemVariantSnapshot.objects.create(
            item=neck, season=self.season, batch_key='test-batch', variant_key='neck-legacy',
            variant_type=WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT, item_level=710,
            compatible_slots=['neck'], socket_count=0, socket_types=[],
        )
        special_ring = WowItemSnapshot.objects.create(
            item_id=10013, name='Legacy Special Ring', catalog_type='equipment', slot_key='finger',
            item_class_id=4, inventory_type=11,
        )
        WowItemVariantSnapshot.objects.create(
            item=special_ring, season=self.season, batch_key='test-batch', variant_key='ring-legacy',
            variant_type=WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT, item_level=710,
            compatible_slots=['finger'], socket_count=1, socket_types=['prismatic'],
        )

        neck_rows = self.client.get('/portal/api/gear-builder/catalog/', {
            'class': 'Warrior', 'spec': 'Fury', 'slot': 'neck',
        }).json()['items']
        ring_rows = self.client.get('/portal/api/gear-builder/catalog/', {
            'class': 'Warrior', 'spec': 'Fury', 'slot': 'finger1',
        }).json()['items']
        neck_variant = next(row for row in neck_rows if row['item_id'] == neck.item_id)['variants'][0]
        ring_variant = next(row for row in ring_rows if row['item_id'] == special_ring.item_id)['variants'][0]
        self.assertEqual((neck_variant['socket_count'], neck_variant['socket_types']), (1, ['prismatic']))
        self.assertEqual((ring_variant['socket_count'], ring_variant['socket_types']), (2, ['prismatic', 'prismatic']))
        self.assertTrue(neck_variant['metadata']['jewelry_socket_baseline_applied'])

    def test_catalog_localizes_raw_english_sources(self):
        self.hero.source_json = [{
            'type': 'raid', 'instance': 'The Venomous Abyss', 'encounter': "Ula'tek",
            'difficulty': 'Mythic',
        }]
        self.hero.save(update_fields=['source_json'])
        rows = self.client.get('/portal/api/gear-builder/catalog/', {
            'class': 'Warrior', 'spec': 'Fury', 'slot': 'head', 'source': 'raid',
        }).json()['items']
        source = next(row for row in rows[0]['variants'] if row['id'] == self.hero.id)['sources'][0]
        self.assertEqual(source['type_zh'], '团队副本')
        self.assertEqual(source['instance_zh'], '烈毒之渊')
        self.assertEqual(source['encounter_zh'], '乌拉特克')
        self.assertEqual(source['difficulty_zh'], '史诗')

    def test_catalog_hides_invalid_myth_track_from_legacy_delve_batch(self):
        invalid_delve = WowItemVariantSnapshot.objects.create(
            item=self.helm, season=self.season, batch_key='test-batch',
            game_build='12.1.0.99999', variant_key='delve-myth-invalid',
            variant_type=WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT,
            item_level=720, upgrade_track='myth', track_rank=1, track_max_rank=6,
            compatible_slots=['head'], stats_json={'strength': 1000, 'crit': 350},
            source_json=[{'type': 'delve', 'instance_zh': '地下堡'}],
        )
        rows = self.client.get('/portal/api/gear-builder/catalog/', {
            'class': 'Warrior', 'spec': 'Fury', 'slot': 'head', 'source': 'all',
        }).json()['items']
        variant_ids = [variant['id'] for row in rows for variant in row['variants']]
        self.assertNotIn(invalid_delve.id, variant_ids)

    def test_catalog_strictly_filters_armor_primary_stat_and_weapon_slot(self):
        cloth = WowItemSnapshot.objects.create(
            item_id=10008, name='Intellect Cloth Hood', catalog_type='equipment', slot_key='head',
            item_class_id=4, item_subclass_id=1, inventory_type=1, armor_type='布甲',
            metadata={'raidbots_stats_alloc': [{'id': 5, 'alloc': 5000}]},
        )
        WowItemVariantSnapshot.objects.create(
            item=cloth, season=self.season, batch_key='test-batch', variant_key='cloth-hero',
            variant_type=WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT, item_level=710,
            compatible_slots=['head'], stats_json={'intellect': 900, 'stamina': 4000, 'haste': 300},
        )
        intellect_sword = WowItemSnapshot.objects.create(
            item_id=10009, name='Intellect Sword', catalog_type='equipment', slot_key='weapon',
            item_class_id=2, item_subclass_id=7, inventory_type=13, weapon_type='单手剑',
            metadata={'raidbots_stats_alloc': [{'id': 5, 'alloc': 5000}]},
        )
        WowItemVariantSnapshot.objects.create(
            item=intellect_sword, season=self.season, batch_key='test-batch', variant_key='int-sword',
            variant_type=WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT, item_level=710,
            compatible_slots=['main_hand', 'off_hand'], stats_json={'intellect': 900, 'haste': 300},
        )
        strength_two_hand = WowItemSnapshot.objects.create(
            item_id=10010, name='Strength Greatsword', catalog_type='equipment', slot_key='main_hand',
            item_class_id=2, item_subclass_id=8, inventory_type=17, weapon_type='双手剑',
            metadata={'raidbots_stats_alloc': [{'id': 4, 'alloc': 5000}]},
        )
        WowItemVariantSnapshot.objects.create(
            item=strength_two_hand, season=self.season, batch_key='test-batch', variant_key='strength-2h',
            variant_type=WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT, item_level=710,
            compatible_slots=['main_hand'], stats_json={'strength': 900, 'crit': 300},
        )

        fury_head = self.client.get('/portal/api/gear-builder/catalog/', {
            'class': 'Warrior', 'spec': 'Fury', 'slot': 'head',
        }).json()['items']
        self.assertNotIn(cloth.item_id, [row['item_id'] for row in fury_head])
        fire_head = self.client.get('/portal/api/gear-builder/catalog/', {
            'class': 'Mage', 'spec': 'Fire', 'slot': 'head',
        }).json()['items']
        self.assertIn(cloth.item_id, [row['item_id'] for row in fire_head])
        self.assertNotIn(self.helm.item_id, [row['item_id'] for row in fire_head])

        fury_weapons = self.client.get('/portal/api/gear-builder/catalog/', {
            'class': 'Warrior', 'spec': 'Fury', 'slot': 'main_hand',
        }).json()['items']
        self.assertNotIn(intellect_sword.item_id, [row['item_id'] for row in fury_weapons])
        fire_weapons = self.client.get('/portal/api/gear-builder/catalog/', {
            'class': 'Mage', 'spec': 'Fire', 'slot': 'main_hand',
        }).json()['items']
        self.assertIn(intellect_sword.item_id, [row['item_id'] for row in fire_weapons])
        self.assertEqual(fire_weapons[0]['variants'][0]['stats'], {'intellect': 900, 'haste': 300})
        fury_offhand = self.client.get('/portal/api/gear-builder/catalog/', {
            'class': 'Warrior', 'spec': 'Fury', 'slot': 'off_hand',
        }).json()['items']
        self.assertIn(strength_two_hand.item_id, [row['item_id'] for row in fury_offhand])
        arms_offhand = self.client.get('/portal/api/gear-builder/catalog/', {
            'class': 'Warrior', 'spec': 'Arms', 'slot': 'off_hand',
        }).json()['items']
        self.assertNotIn(strength_two_hand.item_id, [row['item_id'] for row in arms_offhand])

    def test_enhancements_only_offer_embellishment_for_crafted_equipment(self):
        drop = self.client.get('/portal/api/gear-builder/enhancements/', {
            'class': 'Warrior', 'spec': 'Fury', 'slot': 'head', 'variant_id': self.hero.id,
        }).json()['groups']
        self.assertEqual(drop['embellishments'], [])
        self.assertEqual(drop['gems'][0]['item_id'], 10004)
        self.assertEqual(drop['enchants'][0]['item_id'], 10005)

        crafted = self.client.get('/portal/api/gear-builder/enhancements/', {
            'class': 'Warrior', 'spec': 'Fury', 'slot': 'head', 'variant_id': self.crafted.id,
        }).json()['groups']
        self.assertEqual(crafted['embellishments'][0]['item_id'], 10003)

    def test_enhancements_hide_low_quality_and_keep_highest_rank(self):
        low_item = WowItemSnapshot.objects.create(
            item_id=10006, name='Quick Gem Rank 1', catalog_type='gem', quality=2,
        )
        WowItemVariantSnapshot.objects.create(
            item=low_item, season=self.season, batch_key='test-batch',
            variant_key='gem-q1', variant_type=WowItemVariantSnapshot.TYPE_GEM,
            compatible_slots=['head'], crafting_quality=1,
            metadata={'simc_name': 'quick_gem_1'},
        )
        lower_rank_item = WowItemSnapshot.objects.create(
            item_id=10007, name='Quick Gem Lower Rank', catalog_type='gem', quality=4,
        )
        WowItemVariantSnapshot.objects.create(
            item=lower_rank_item, season=self.season, batch_key='test-batch',
            variant_key='gem-q2-low', variant_type=WowItemVariantSnapshot.TYPE_GEM,
            compatible_slots=['head'], crafting_quality=1,
            metadata={'simc_name': 'quick_gem_1'},
        )
        groups = self.client.get('/portal/api/gear-builder/enhancements/', {
            'class': 'Warrior', 'spec': 'Fury', 'slot': 'head', 'variant_id': self.hero.id,
        }).json()['groups']
        self.assertEqual([row['item_id'] for row in groups['gems']], [10004])

    def test_enhancements_keep_only_highest_embellishment_quality(self):
        lower_item = WowItemSnapshot.objects.create(
            item_id=10011, name='Rift Lining', name_zh='裂隙内衬',
            catalog_type='embellishment', quality=3,
        )
        WowItemVariantSnapshot.objects.create(
            item=lower_item, season=self.season, batch_key='test-batch',
            variant_key='embellishment-q1', variant_type=WowItemVariantSnapshot.TYPE_EMBELLISHMENT,
            crafting_quality=1, compatible_slots=['head'], unique_group='embellishment-limit',
            max_equipped=2, effects_json=[{'description_zh': '较低数值效果'}],
        )
        self.embellishment.crafting_quality = 2
        self.embellishment.save(update_fields=['crafting_quality'])
        groups = self.client.get('/portal/api/gear-builder/enhancements/', {
            'class': 'Warrior', 'spec': 'Fury', 'slot': 'head', 'variant_id': self.crafted.id,
        }).json()['groups']
        self.assertEqual([row['item_id'] for row in groups['embellishments']], [10003])

    def test_crafted_resolver_applies_two_stats_and_embellishment_effect(self):
        response = self.client.post(
            '/portal/api/gear-builder/resolve-crafted/',
            data=json.dumps({
                'variant_id': self.crafted.id,
                'selected_stats': ['crit', 'mastery'],
                'embellishment_variant_id': self.embellishment.id,
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['resolved_stats']['crit'], 350)
        self.assertEqual(payload['resolved_stats']['mastery'], 350)
        self.assertEqual(payload['embellishment']['item_id'], 10003)
        self.assertIn('裂隙之力', payload['effects'][-1]['description_zh'])

        invalid = self.client.post(
            '/portal/api/gear-builder/resolve-crafted/',
            data=json.dumps({'variant_id': self.crafted.id, 'selected_stats': ['crit', 'crit']}),
            content_type='application/json',
        )
        self.assertEqual(invalid.status_code, 400)

    def test_simc_import_maps_catalog_and_preserves_external_item(self):
        profile = '\n'.join((
            'warrior="Test"',
            'spec=fury',
            'head=rift_helm,id=10001,ilevel=710,bonus_id=1/2,gem_id=10004,enchant_id=8001',
            'neck=outside_item,id=99999,ilevel=710',
        ))
        response = self.client.post(
            '/portal/api/gear-builder/import-simc/',
            data=json.dumps({'profile': profile}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['identity']['class_name'], 'Warrior')
        by_slot = {row['slot']: row for row in payload['equipment']}
        self.assertFalse(by_slot['head']['external'])
        self.assertEqual(by_slot['head']['variant_id'], self.hero.id)
        self.assertEqual(by_slot['head']['gems'][0]['variant_id'], self.gem.id)
        self.assertEqual(by_slot['head']['enchant']['variant_id'], self.enchant.id)
        self.assertTrue(by_slot['neck']['external'])
        self.assertTrue(payload['warnings'])


class GearBuilderImportCommandTests(TestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(
            season_key='catalog-import-test', season_name='目录导入测试', is_active=True,
            mplus_zone_id=1, raid_zone_id=2,
        )

    def catalog_payload(self):
        return {
            'season_key': self.season.season_key,
            'batch_key': 'batch-20260831',
            'game_build': '12.1.0.99999',
            'provider': {'structure': 'wago_db2', 'display': 'wowhead'},
            'rules': {'socket_additions': [{'slot': 'head', 'max_additional': 1}]},
            'items': [{
                'item_id': 20001,
                'name': 'Imported Helm',
                'name_zh': '导入头盔',
                'slot_key': 'head',
                'catalog_type': 'equipment',
                'variants': [{
                    'key': 'hero-1',
                    'type': 'drop_equipment',
                    'item_level': 700,
                    'upgrade_track': 'hero',
                    'track_rank': 1,
                    'track_max_rank': 6,
                    'compatible_slots': ['head'],
                    'stats': {'strength': 800, 'crit': 300},
                    'sources': [{'type': 'raid', 'instance_zh': '测试团本'}],
                }],
            }],
        }

    def run_import(self, payload, *extra):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / 'catalog.json'
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            output = StringIO()
            call_command('sync_gear_builder_catalog', '--input', str(path), *extra, stdout=output)
            return output.getvalue()

    def test_import_reuses_item_master_is_idempotent_and_activates_batch(self):
        existing = WowItemSnapshot.objects.create(item_id=20001, name_zh='旧名称')
        output = self.run_import(self.catalog_payload(), '--activate')
        self.assertIn('已激活装备目录批次', output)
        self.assertEqual(WowItemSnapshot.objects.filter(item_id=20001).count(), 1)
        existing.refresh_from_db()
        self.assertEqual(existing.name_zh, '导入头盔')
        self.assertEqual(WowItemVariantSnapshot.objects.count(), 1)
        self.run_import(self.catalog_payload(), '--activate')
        self.assertEqual(WowItemVariantSnapshot.objects.count(), 1)
        self.season.refresh_from_db()
        self.assertEqual(self.season.gear_batch_key, 'batch-20260831')
        self.assertEqual(self.season.gear_sync_status, 'ready')
        self.assertEqual(self.season.gear_sync_report['variant_counts']['drop_equipment'], 1)
        self.assertEqual(self.season.gear_sync_report['missing_counts']['compatible_slots'], 0)
        self.assertEqual(self.season.gear_sync_report['catalog_rules']['socket_additions'][0]['slot'], 'head')

    def test_dry_run_and_invalid_track_do_not_write(self):
        self.run_import(self.catalog_payload(), '--dry-run')
        self.assertFalse(WowItemSnapshot.objects.exists())
        payload = self.catalog_payload()
        payload['items'][0]['variants'][0]['upgrade_track'] = 'veteran'
        with self.assertRaisesMessage(Exception, '目录审计失败'):
            self.run_import(payload, '--activate')
        self.assertFalse(WowItemSnapshot.objects.exists())

    def test_audit_reports_tolerated_upstream_build_lag(self):
        payload = self.catalog_payload()
        payload['game_build'] = '12.1.0.69497'
        payload['provider'].update({
            'catalog_build': '12.1.0.69497',
            'wago_build': '12.1.0.69587',
            'raidbots_build': '12.1.0.69497',
            'build_sync_status': 'raidbots_lagging',
        })
        report = SyncGearBuilderCatalogCommand()._audit_payload(payload)
        self.assertTrue(any('上游构建尚未完全同步' in value for value in report['warnings']))

    @patch('botend.management.commands.sync_gear_builder_catalog.CurrentGearCatalogSource.build')
    def test_fetch_current_can_create_and_activate_season_without_input_file(self, build):
        payload = self.catalog_payload()
        payload['season_key'] = 'auto-season-test'
        payload['season_name'] = '自动赛季测试'
        payload['season_info'] = {
            'mplus_zone_id': -1, 'mplus_zone_name': '当前大秘境',
            'raid_zone_id': 1320, 'raid_zone_name': '当前团本',
        }
        build.return_value = payload
        output = StringIO()
        call_command(
            'sync_gear_builder_catalog', '--fetch-current', '--create-season', '--activate',
            '--skip-wowhead', stdout=output,
        )
        season = SeasonMeta.objects.get(season_key='auto-season-test')
        self.assertEqual(season.gear_batch_key, 'batch-20260831')
        self.assertEqual(season.game_build, '12.1.0.99999')
        self.assertIn('已激活装备目录批次', output.getvalue())


class GearBuilderCurrentSourceTests(TestCase):
    def test_identity_stats_remove_other_primary_attributes(self):
        raw = {'strength': 500, 'agility': 500, 'intellect': 500, 'crit': 200}
        self.assertEqual(stats_for_identity(raw, {}, 'Warrior', 'Fury'), {'strength': 500, 'crit': 200})
        self.assertEqual(stats_for_identity(raw, {}, 'Mage', 'Fire'), {'intellect': 500, 'crit': 200})

    def test_same_patch_family_uses_real_raidbots_catalog_build(self):
        build, status = CurrentGearCatalogSource._resolve_catalog_build(
            '12.1.0.69587', '12.1.0.69497',
        )
        self.assertEqual(build, '12.1.0.69497')
        self.assertEqual(status, 'raidbots_lagging')

    def test_cross_patch_build_mismatch_still_blocks_activation(self):
        with self.assertRaisesMessage(CatalogSourceError, '跨版本不一致'):
            CurrentGearCatalogSource._resolve_catalog_build('12.1.5.70000', '12.1.0.69497')

    def test_special_mythic_drop_adds_344_variant_and_native_socket(self):
        item = {
            'inventory_type': 1,
            'metadata': {'native_socket_types': ['prismatic']},
            'variants': [],
        }
        profile = {'tracks': {'champion': [300], 'hero': [315], 'myth': [328]}}
        CurrentGearCatalogSource._add_drop_variants(
            item, profile, 'raid', [{'type': 'raid', 'encounter': '最终首领'}], special_mythic=True,
        )
        special = next(row for row in item['variants'] if row['item_level'] == 344)
        self.assertEqual((special['upgrade_track'], special['track_rank'], special['track_max_rank']), ('myth', 9, 6))
        self.assertEqual(special['socket_count'], 1)
        self.assertTrue(special['metadata']['special_mythic_drop'])

    def test_jewelry_gets_one_base_socket_and_preserves_one_extra_native_socket(self):
        normal_neck = CurrentGearCatalogSource._base_item(
            {'id': 31, 'name': 'Normal Neck', 'inventoryType': 2, 'itemClass': 4},
            'equipment', ('neck',),
        )
        special_ring = CurrentGearCatalogSource._base_item(
            {
                'id': 32, 'name': 'Special Ring', 'inventoryType': 11, 'itemClass': 4,
                'socketInfo': {'sockets': [{'type': 'PRISMATIC'}]},
            },
            'equipment', ('finger',),
        )
        self.assertEqual(normal_neck['metadata']['native_socket_types'], ['prismatic'])
        self.assertEqual(special_ring['metadata']['native_socket_types'], ['prismatic', 'prismatic'])
        self.assertTrue(normal_neck['metadata']['jewelry_socket_baseline_applied'])

    def test_only_head_wrists_and_waist_can_receive_added_sockets(self):
        rules = CurrentGearCatalogSource._socket_addition_rules({
            'sockets': [
                {'slot': 'head', 'vault': 1},
                {'slot': 'wrist', 'vault': 1},
                {'slot': 'waist', 'vault': 1},
                {'slot': 'finger', 'extraSockets': 1},
                {'slot': 'neck', 'extraSockets': 1},
            ],
        })
        self.assertEqual([row['slot'] for row in rules], ['head', 'wrists', 'waist'])

    def test_delve_drop_variants_stop_at_hero_six(self):
        item = {'inventory_type': 1, 'metadata': {}, 'variants': []}
        profile = {
            'tracks': {
                'champion': [292, 295, 298, 302, 305, 308],
                'hero': [305, 308, 311, 315, 318, 321],
                'myth': [318, 321, 324, 328, 331, 334],
            },
        }
        CurrentGearCatalogSource._add_drop_variants(
            item, profile, 'delve', [{'type': 'delve', 'instance': 'Delves Season 2'}],
        )
        self.assertEqual({row['upgrade_track'] for row in item['variants']}, {'champion', 'hero'})
        highest = max(item['variants'], key=lambda row: row['item_level'])
        self.assertEqual((highest['upgrade_track'], highest['track_rank'], highest['track_max_rank']), ('hero', 6, 6))
        self.assertEqual(highest['item_level'], 321)

    def test_enhancement_source_keeps_only_highest_current_quality(self):
        rows = [
            {'itemId': 1, 'expansion': 11, 'quality': 2, 'craftingQuality': 1, 'slot': 'socket', 'tokenizedName': 'gem_1'},
            {'itemId': 2, 'expansion': 11, 'quality': 4, 'craftingQuality': 1, 'slot': 'socket', 'tokenizedName': 'gem_1'},
            {'itemId': 3, 'expansion': 11, 'quality': 4, 'craftingQuality': 2, 'slot': 'socket', 'tokenizedName': 'gem_2'},
        ]
        self.assertEqual(
            [row['itemId'] for row in CurrentGearCatalogSource._highest_quality_enhancements(rows)],
            [3],
        )

    def test_embellishment_source_keeps_only_highest_effect_value(self):
        rows = [
            {
                'id': 21, 'name': 'Sunfire Lining', 'expansion': 11, 'quality': 3,
                'craftingQuality': 1, 'craftingCategoryId': 7,
                'itemLimit': {'category': 512, 'quantity': 2},
            },
            {
                'id': 22, 'name': 'Sunfire Lining', 'expansion': 11, 'quality': 3,
                'craftingQuality': 2, 'craftingCategoryId': 7,
                'itemLimit': {'category': 512, 'quantity': 2},
            },
        ]
        selected = CurrentGearCatalogSource._highest_quality_embellishments(rows)
        self.assertEqual([row['id'] for row in selected], [22])

    def test_current_catalyst_tier_sets_are_added_as_raid_equipment(self):
        by_id = {}
        CurrentGearCatalogSource._add_tier_set_items(
            by_id,
            [{
                'id': 271454, 'name': 'Tier Shoulders', 'expansion': 11,
                'itemSetId': 2067, 'itemClass': 4, 'itemSubClass': 4,
                'inventoryType': 3, 'allowableClasses': [1],
                'sources': [{'instanceId': -100, 'encounterId': -100}],
            }],
            [{
                'id': 2067, 'name': 'Jade Warlord', 'items': [271454],
                'spells': [{'spellId': 1296645, 'reqItems': 2, 'specId': 72}],
            }],
            {'tracks': {'champion': [292], 'hero': [305], 'myth': [318]}},
            {'id': -100, 'type': 'catalyst'},
            [1302],
            {1302: {'id': 1302, 'name': 'The Venomous Abyss'}},
        )
        item = by_id[271454]
        self.assertEqual(item['allowable_class_mask'], 1)
        self.assertTrue(item['metadata']['is_tier_set'])
        self.assertEqual(item['effect_refs'][0]['spec_id'], 72)
        self.assertEqual({row['upgrade_track'] for row in item['variants']}, {'champion', 'hero', 'myth'})
        self.assertEqual(item['variants'][0]['sources'][0]['type'], 'raid')
        self.assertEqual(item['variants'][0]['sources'][0]['encounter_zh'], '职业套装（首领兑换或化生）')

    def test_wowhead_tooltip_parser_extracts_scaled_stats_and_crafted_options(self):
        details = _tooltip_details({
            'name': '破法者的步伐', 'quality': 4, 'icon': 'inv_boot',
            'tooltip': (
                '<span>266护甲</span><br><span>+138 [力量 or 智力]</span>'
                '<br><span>+2,832 耐力</span><br><span>+74 随机属性1</span>'
                '<br><span>+74 随机属性2</span><br>装备：测试效果。'
            ),
        })
        self.assertEqual(details['stats']['armor'], 266)
        self.assertEqual(details['stats']['stamina'], 2832)
        self.assertEqual(details['primary_options'], {'strength': 138, 'intellect': 138})
        self.assertEqual(details['secondary_total'], 148)
        self.assertEqual(details['effects'][0]['description_zh'], '装备：测试效果。')

    def test_wowhead_tooltip_parser_keeps_embellishment_and_tier_descriptions(self):
        details = _tooltip_details({
            'name': '圣佑穿山甲护符', 'quality': 3,
            'tooltip': (
                '<div>提供下列属性：你的增益性技能有几率赋予全能。</div><br>'
                '<span>(2) 组合 狂怒: 怒击的伤害提高15%。</span><br>'
                '<span>(4) 组合 狂怒: 嗜血的伤害提高10%。</span>'
            ),
        })
        self.assertIn('提供下列属性', details['description_zh'])
        self.assertIn('(2) 组合 狂怒', details['description_zh'])
        self.assertEqual(len(details['effects']), 3)


@override_settings(OSS_CONFIG={
    'access_key_id': 'test', 'access_key_secret': 'test', 'region': 'cn-test',
    'bucket_name': 'test', 'base_url': 'https://oss.example.test',
}, PROXY_CONFIG={'http': 'socks5://proxy.test:1080', 'https': 'socks5://proxy.test:1080'})
class GearBuilderIconSyncTests(TestCase):
    @patch('botend.services.gear_builder_icon_sync.ossUploadBytes', return_value='https://oss.example.test/icon.jpg')
    @patch('botend.services.gear_builder_icon_sync.requests.get')
    @patch('botend.services.gear_builder_icon_sync.requests.head')
    def test_streams_missing_icons_directly_and_skips_existing_objects(self, head, get, upload):
        head.side_effect = [SimpleNamespace(status_code=404), SimpleNamespace(status_code=200)]
        get.return_value = SimpleNamespace(status_code=200, content=b'\xff\xd8\xff' + b'x' * 200)
        report = GearBuilderIconSync(workers=1).sync(['inv_missing', 'inv_existing'])
        self.assertEqual(report['uploaded'], 1)
        self.assertEqual(report['skipped'], 1)
        self.assertEqual(report['failed'], 0)
        upload.assert_called_once_with(
            b'\xff\xd8\xff' + b'x' * 200,
            'wow_icons_oss/medium/inv_missing.jpg',
        )
        self.assertEqual(get.call_args.kwargs['proxies'], {
            'http': 'socks5://proxy.test:1080', 'https': 'socks5://proxy.test:1080',
        })
        self.assertEqual(head.call_args.kwargs['proxies'], {
            'http': 'socks5://proxy.test:1080', 'https': 'socks5://proxy.test:1080',
        })


class GearBuilderFrontendContractTests(TestCase):
    def test_shared_header_and_frontend_contract(self):
        root = Path(__file__).resolve().parents[2]
        header = (root / 'templates/portal/_header.html').read_text(encoding='utf-8')
        template = (root / 'templates/portal/gear_builder.html').read_text(encoding='utf-8')
        script = (root / 'static/portal/js/gear_builder.js').read_text(encoding='utf-8')
        self.assertIn('/portal/gear-builder/', header)
        self.assertIn('职业配装器', header)
        for value in ('装备', '强化', '美化', '宝石', '永久附魔', '导入 SimC', '绿字', '游戏预览', '角色装备预览'):
            self.assertIn(value, template)
        self.assertIn('gear-add-socket', template)
        for value in ('localStorage', 'CompressionStream', 'parse', 'crafted_stats', 'data-wow-item-tooltip'):
            self.assertIn(value, script)
        for value in ('socketCapacity', 'addedSocket', 'totalsAndEffects', 'gear-option-check'):
            self.assertIn(value, script)
        for value in ('enhancementSummary', 'gear-slot-enhancements', 'item.description'):
            self.assertIn(value, script)
        self.assertIn('const SUMMARY_STATS = ["crit", "haste", "mastery", "versatility"]', script)
        self.assertIn('secondary_stat_conversion', script)
        self.assertIn('gear-stat-percent', script)
        self.assertIn('const BASE_SECONDARY_PERCENTAGES = Object.freeze({', script)
        self.assertIn('crit: 5', script)
        self.assertIn('mastery: 8', script)
        self.assertIn('return (basePercent + ratingPercent) * coefficient;', script)
        self.assertIn('5% 基础暴击', script)
        self.assertIn('8% 基础精通', script)
        self.assertIn('const ADDITIONAL_SOCKET_SLOTS = new Set(["head", "wrists", "waist"])', script)
        self.assertIn('if (!ADDITIONAL_SOCKET_SLOTS.has(family)) return null;', script)
        self.assertIn('data-select-item', script)
        self.assertNotIn('data-add-item', script)
        self.assertIn('SECONDARY_STATS.has(key)', script)
        for value in ('renderPreview', 'data-preview-slot', 'preview_item_level', 'state.viewMode === "preview"'):
            self.assertIn(value, script)
