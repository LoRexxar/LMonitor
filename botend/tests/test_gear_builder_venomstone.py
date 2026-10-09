"""毒液石部位、装等、数值与增量导入回归。"""
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, SimpleTestCase

from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.services.gear_builder import serialize_variant
from botend.services.gear_builder_catalog_source import CurrentGearCatalogSource, SEASON_LEVEL_PROFILES, _tooltip_details
from botend.services.gear_builder_venomstone import upgraded_variant, apply_tooltip, preserve_localized_effects, tooltip_branch


class VenomstoneRulesTests(SimpleTestCase):
    def test_crafted_explicit_levels_keep_crafting_and_refresh_secondary_pool(self):
        for inventory_type in (1, 2, 11, 12, 13, 14, 17, 23):
            item = {'inventory_type': inventory_type, 'metadata': {}, 'variants': []}
            CurrentGearCatalogSource._add_crafted_variants(item, SEASON_LEVEL_PROFILES['mid2'], [{'type': 'crafted'}])
            upgrades = [v for v in item['variants'] if v.get('metadata', {}).get('venomstone')]
            self.assertEqual([v['item_level'] for v in upgrades], [] if inventory_type in (1, 11) else [328, 340])
            self.assertTrue(all(v['type'] == 'crafted_equipment' and v['crafting_quality'] == 5 for v in upgrades))
        base = {'key': 'crafted-myth-q5-331', 'type': 'crafted_equipment', 'item_level': 331,
                'crafting_quality': 5, 'crafting_options': {'stat_count': 2, 'stat_pool': ['crit', 'haste'], 'secondary_total': 198},
                'bonus_ids': [1808], 'socket_count': 1, 'is_intrinsic_embellishment': True}
        upgrade = upgraded_variant(17, base)
        self.assertEqual(upgrade['item_level'], 340)
        self.assertEqual(upgrade['bonus_ids'], [1808], '制造档位不套用掉落轨道bonus')
        self.assertEqual(upgrade['crafting_options']['stat_count'], 2)
        self.assertNotIn('secondary_total', upgrade['crafting_options'])
        self.assertTrue(upgrade['is_intrinsic_embellishment'])
        self.assertEqual(upgrade['socket_count'], 1)
        with self.assertRaisesMessage(ValueError, '副属性'):
            apply_tooltip(upgrade, {'item_level': 340, 'stats': {'strength': 199}})
        apply_tooltip(upgrade, {'item_level': 340, 'stats': {'strength': 199}, 'secondary_total': 216})
        self.assertEqual(upgrade['crafting_options']['secondary_total'], 216)
        self.assertEqual(base['crafting_options']['secondary_total'], 198)
        self.assertIsNone(upgraded_variant(17, dict(base, crafting_quality=4)))

    def test_automatic_branch_follows_item_origin(self):
        self.assertEqual(tooltip_branch({}), 'live')
        self.assertEqual(tooltip_branch({'ptr_preview': True}), 'ptr-2')
        self.assertEqual(tooltip_branch({'ptr_preview': True}, 'live'), 'live')
        self.assertEqual(tooltip_branch({}, 'ptr'), 'ptr')

    @patch.object(CurrentGearCatalogSource, 'venomstone_tooltip')
    def test_full_catalog_uses_live_for_regular_upgrades(self, fetch):
        fetch.return_value = {'item_level': 340, 'stats': {'strength': 120}, 'effects': [{'description_zh': '装备：新数值。'}]}
        source = CurrentGearCatalogSource(cache_dir='.cache/venomstone-fix-tests')
        variant = {'item_level': 340, 'type': 'drop_equipment', 'metadata': {'venomstone': {'item_id': 280562}}}
        source._enrich_wowhead([{'item_id': 270175, 'inventory_type': 12, 'variants': [variant]}], '测试构建')
        self.assertEqual(fetch.call_args.args[-1], 'live')
        self.assertEqual(variant['effects'][0]['description_zh'], '装备：新数值。')
        variant['metadata']['ptr_preview'] = True
        source._enrich_wowhead([{'item_id': 270175, 'inventory_type': 12, 'variants': [variant]}], '测试构建')
        self.assertEqual(fetch.call_args.args[-1], 'ptr-2')

    def test_only_eligible_drop_slots_receive_two_extra_variants(self):
        for inventory_type in (1, 2, 11, 12, 13, 14, 17, 23):
            with self.subTest(inventory_type=inventory_type):
                item = {'inventory_type': inventory_type, 'metadata': {}, 'variants': []}
                CurrentGearCatalogSource._add_drop_variants(item, SEASON_LEVEL_PROFILES['mid2'], 'raid', [])
                extra = [row for row in item['variants'] if row['track_rank'] > 6]
                self.assertEqual([(v['upgrade_track'], v['track_rank'], v['track_max_rank'], v['item_level'], v['bonus_ids']) for v in extra],
                                 [] if inventory_type in (1, 11) else [('hero', 8, 6, 328, [12848]), ('myth', 8, 6, 340, [12856])])

    def test_delve_myth_unlock_and_special_mythic_limits(self):
        item = {'inventory_type': 12, 'metadata': {}, 'variants': []}
        sources = [{'type': 'delve'}]
        CurrentGearCatalogSource._add_drop_variants(item, SEASON_LEVEL_PROFILES['mid2'], 'delve', sources)
        myth = [row for row in item['variants'] if row['upgrade_track'] == 'myth']
        self.assertEqual([(row['track_rank'], row['item_level']) for row in myth],
                         [(1, 318), (2, 321), (3, 324), (4, 328), (5, 331), (6, 334), (8, 340)])
        self.assertTrue(all(row['metadata']['delve_myth']['patch'] == '12.1.5' for row in myth))
        self.assertTrue(all(row['sources'] == sources for row in myth))
        item['variants'] = []
        profile = {key: value for key, value in SEASON_LEVEL_PROFILES['mid2'].items() if key != 'delve_myth'}
        CurrentGearCatalogSource._add_drop_variants(item, profile, 'delve', sources)
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
    def test_real_retail_chinese_without_templates_imports_new_values(self, fetch):
        fixture = json.loads((Path(__file__).parent / 'fixtures/venomstone_retail_270175.json').read_text(encoding='utf8'))
        self.item.item_id = fixture['item_id']
        self.item.name_zh = '乌拉特克贪婪之心'
        self.item.inventory_type = 12
        self.item.save()
        self.base.effects_json = fixture['base_effects']
        self.base.save(update_fields=['effects_json'])
        # 旧默认 PTR 会得到英文；旧目录只有中文文本，确实没有可绑定的新数值模板。
        ptr_variant = {'effects': _tooltip_details(fixture['ptr-2'])['effects']}
        with self.assertRaisesMessage(ValueError, '中文模板'):
            preserve_localized_effects(ptr_variant, fixture['base_effects'])
        fetch.side_effect = lambda _iid, _level, _cache, branch: _tooltip_details(fixture[branch])
        self.execute(apply=True)
        self.assertEqual(fetch.call_args.args[-1], 'live')
        upgraded = WowItemVariantSnapshot.objects.get(track_rank=8)
        effect = upgraded.effects_json[0]['description_zh']
        self.assertIn('462力量或敏捷', effect)
        self.assertIn('15131点物理伤害', effect)
        self.assertNotIn('13921', effect)

    @patch.object(CurrentGearCatalogSource, 'venomstone_tooltip')
    def test_ptr_upgrade_retains_template_translation(self, fetch):
        self.base.metadata['ptr_preview'] = True
        self.base.effects_json = [{'spell_id': 1, 'trigger_type': 1, 'template': 'Gain $s1 Haste.',
                                  'template_zh': '获得$s1急速。', 'description_zh': '装备：获得100急速。'}]
        self.base.save(update_fields=['metadata', 'effects_json'])
        fetch.side_effect = lambda _iid, level, *_args: dict(self.details, item_level=level, effects=[{'description': 'Equip: Gain 150 Haste.'}])
        self.execute(apply=True)
        self.assertEqual(fetch.call_args.args[-1], 'ptr-2')
        self.assertEqual(WowItemVariantSnapshot.objects.get(track_rank=8).effects_json[0]['description_zh'], '装备：获得150急速。')

    @patch.object(CurrentGearCatalogSource, 'venomstone_tooltip')
    def test_real_ptr_tuning_refreshes_base_and_upgrade_together(self, fetch):
        fixture = json.loads((Path(__file__).parent / 'fixtures/venomstone_ptr_280799.json').read_text(encoding='utf8'))
        self.item.metadata = {'ptr_preview': True}
        self.item.save(update_fields=['metadata'])
        self.base.effects_json = fixture['base_effects']
        self.base.metadata.update(game_build='12.1.5.69594', simc_revision='旧版本',
                                  effects_status='exact_build_db2_simc', primary_stat_amount=189)
        self.base.save()
        original_id, original_bonus = self.base.pk, deepcopy(self.base.bonus_ids)
        def details(_iid, level, _cache, branch):
            self.assertEqual(branch, 'ptr-2')
            value = _tooltip_details(fixture[str(level)])
            value['source'] = {'provider': 'wowhead', 'branch': branch}
            return value
        fetch.side_effect = details
        self.assertIn('1,127', self.base.effects_json[0]['description_zh'])
        self.execute()
        fetch.assert_not_called()
        for _ in range(2):
            self.execute(apply=True)
            self.base.refresh_from_db()
            upgraded = WowItemVariantSnapshot.objects.get(track_rank=8)
            self.assertIn('756急速', self.base.effects_json[0]['description_zh'])
            self.assertIn('9972点光辉伤害', self.base.effects_json[0]['description_zh'])
            self.assertIn('774急速', upgraded.effects_json[0]['description_zh'])
            self.assertEqual(self.base.pk, original_id)
            self.assertEqual(self.base.bonus_ids, original_bonus)
            self.assertEqual(self.base.game_build, '')
            for key in ('game_build', 'simc_revision', 'effects_status', 'primary_stat_amount'):
                self.assertNotIn(key, self.base.metadata)
            self.assertEqual(self.base.metadata['tooltip_source']['branch'], 'ptr-2')
            self.assertEqual(WowItemVariantSnapshot.objects.count(), 2)

    @patch.object(CurrentGearCatalogSource, 'venomstone_tooltip')
    def test_refresh_covers_all_preview_ranks_and_is_atomic(self, fetch):
        self.base.metadata['ptr_preview'] = True
        self.base.save()
        champion = deepcopy(self.base)
        champion.pk = None
        champion.variant_key = 'raid-champion-1-292'
        champion.item_level, champion.upgrade_track, champion.track_rank = 292, 'champion', 1
        champion.save()
        special = deepcopy(self.base)
        special.pk = None
        special.variant_key = 'raid-myth-9-344'
        special.item_level, special.track_rank = 344, 9
        special.save()
        fetch.side_effect = lambda _iid, level, *_args: dict(self.details, item_level=level)
        self.execute(apply=True)
        self.assertEqual({call.args[1] for call in fetch.call_args_list}, {292, 334, 340, 344})
        for row in (champion, special):
            row.refresh_from_db()
            self.assertEqual(row.effects_json, self.details['effects'])
        fetch.side_effect = lambda _iid, level, *_args: dict(self.details, item_level=level,
            effects=[] if level == 292 else [{'description_zh': '装备：提高200急速。'}])
        with self.assertRaisesMessage(CommandError, 'champion 1/6'):
            self.execute(apply=True)
        for row in WowItemVariantSnapshot.objects.all():
            self.assertEqual(row.effects_json, self.details['effects'])

    @patch.object(CurrentGearCatalogSource, 'venomstone_tooltip')
    def test_localization_error_names_the_item_and_preserves_atomicity(self, fetch):
        fetch.return_value = dict(self.details, effects=[{'description': 'Equip: Changed mechanics.'}])
        with self.assertRaises(CommandError) as error:
            self.execute(apply=True)
        message = str(error.exception)
        for expected in ('数据库未修改', '测试武器', '物品 280799', '装等 340', '来源 live', 'myth 8/6'):
            self.assertIn(expected, message)
        self.assertEqual(WowItemVariantSnapshot.objects.count(), 1)

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
