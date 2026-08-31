import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.services.gear_builder_catalog_source import _tooltip_details


class GearBuilderTestDataMixin:
    def setUp(self):
        super().setUp()
        self.season = SeasonMeta.objects.create(
            season_key='midnight-s2-test',
            season_name='午夜 S2 测试',
            is_active=True,
            mplus_zone_id=1,
            raid_zone_id=2,
            game_build='12.1.0.99999',
            gear_batch_key='test-batch',
            gear_sync_status='ready',
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
        )
        self.gem = WowItemVariantSnapshot.objects.create(
            item=self.gem_item,
            season=self.season,
            batch_key='test-batch',
            variant_key='gem-q3',
            variant_type=WowItemVariantSnapshot.TYPE_GEM,
            compatible_slots=['head', 'finger'],
            stats_json={'haste': 120},
            source_json=[{'type': 'profession', 'profession_zh': '珠宝加工'}],
        )
        self.enchant_item = WowItemSnapshot.objects.create(
            item_id=10005,
            name='Helm Enchant',
            name_zh='头部附魔',
            catalog_type='enchant',
            enchantment_id=8001,
        )
        self.enchant = WowItemVariantSnapshot.objects.create(
            item=self.enchant_item,
            season=self.season,
            batch_key='test-batch',
            variant_key='enchant-q3',
            variant_type=WowItemVariantSnapshot.TYPE_ENCHANT,
            compatible_slots=['head'],
            stats_json={'crit': 100},
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

    def test_dry_run_and_invalid_track_do_not_write(self):
        self.run_import(self.catalog_payload(), '--dry-run')
        self.assertFalse(WowItemSnapshot.objects.exists())
        payload = self.catalog_payload()
        payload['items'][0]['variants'][0]['upgrade_track'] = 'veteran'
        with self.assertRaisesMessage(Exception, '目录审计失败'):
            self.run_import(payload, '--activate')
        self.assertFalse(WowItemSnapshot.objects.exists())

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


class GearBuilderFrontendContractTests(TestCase):
    def test_shared_header_and_frontend_contract(self):
        root = Path(__file__).resolve().parents[2]
        header = (root / 'templates/portal/_header.html').read_text(encoding='utf-8')
        template = (root / 'templates/portal/gear_builder.html').read_text(encoding='utf-8')
        script = (root / 'static/portal/js/gear_builder.js').read_text(encoding='utf-8')
        self.assertIn('/portal/gear-builder/', header)
        self.assertIn('职业配装器', header)
        for value in ('装备', '强化', '美化', '宝石', '永久附魔', '导入 SimC'):
            self.assertIn(value, template)
        for value in ('localStorage', 'CompressionStream', 'parse', 'crafted_stats', 'data-wow-item-tooltip'):
            self.assertIn(value, script)
