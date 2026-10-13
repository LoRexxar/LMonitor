"""装备、宝石、附魔及美化文本分离的回归验证。"""
from copy import deepcopy
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from botend.services.wow_item_text import normalize_catalog_text, separate_item_text
from botend.services.gear_builder_catalog_source import CatalogSourceError, _tooltip_details
from botend.services.gear_builder import serialize_item
from botend.tests.test_gear_builder import GearBuilderTestDataMixin


class ItemTextSeparationTests(SimpleTestCase):
    def test_current_enchants_render_chinese_without_losing_static_or_proc_details(self):
        import json
        from pathlib import Path
        from botend.models import WowItemSnapshot, WowItemVariantSnapshot

        rows = json.loads((Path(__file__).parent / 'fixtures/enchant_text_projection.json').read_text())
        self.assertEqual(len(rows), 43)
        projections = {}
        for row in rows:
            with self.subTest(item_id=row['item_id']):
                item = WowItemSnapshot(**{key: row[key] for key in (
                    'item_id', 'name', 'name_zh', 'description', 'description_zh')}, catalog_type='enchant')
                variant = WowItemVariantSnapshot(item=item, variant_type='enchant',
                    stats_json=deepcopy(row['stats_json']), effects_json=deepcopy(row['effects_json']),
                    metadata=deepcopy(row['metadata']), variant_key=row['variant_key'])
                projection = serialize_item(item, [variant], 'Warrior', 'Fury')
                projections[row['item_id']] = projection
                tooltip = projection['variants'][0]['tooltip']
                self.assertTrue(tooltip)
                self.assertNotRegex(tooltip, r'[A-Za-z]')
                self.assertNotRegex(projection['description'], r'[A-Za-z]')
                for effect in projection['variants'][0]['effects']:
                    self.assertTrue(effect.get('description_zh'))
                    self.assertNotRegex(effect['description_zh'], r'[A-Za-z]')
                self.assertEqual(variant.stats_json, row['stats_json'])
                self.assertEqual(variant.effects_json, row['effects_json'])
                self.assertEqual(item.description_zh, row['description_zh'])
        self.assertIn('+50 力量', projections[243977]['variants'][0]['tooltip'])
        self.assertEqual(projections[243977]['variants'][0]['effects'], [])
        self.assertIn('27点护甲值', projections[244643]['variants'][0]['tooltip'])
        self.assertIn('主属性', projections[244029]['variants'][0]['tooltip'])
        self.assertIn('67', projections[244029]['variants'][0]['tooltip'])
        self.assertIn('15秒', projections[244029]['variants'][0]['tooltip'])
        self.assertIn('最大法力值提高4%', projections[240155]['variants'][0]['tooltip'])
        self.assertIn('总法力值提高5%', projections[244003]['variants'][0]['tooltip'])

    def test_enhancement_without_chinese_effect_source_keeps_english_fallback(self):
        for description_zh in ('', '一块古老的护甲片。'):
            with self.subTest(description_zh=description_zh):
                data = separate_item_text(description='Use: Gain 20 Haste for 10 seconds.',
                    description_zh=description_zh, enhancement=True, recover_description_effects=True)
                self.assertEqual(data['effects'], [{'description': 'Use: Gain 20 Haste for 10 seconds.'}])

    def test_delve_description_discards_other_level_stats_and_tooltip_metadata(self):
        description = '\n'.join([
            '升级：勇士 6/6', '腕部 板甲', '静态属性说明：183护甲',
            '+83 [力量 or 智力]', '静态属性说明：+1629 耐力',
            '静态属性说明：+ 41暴击', '静态属性说明：+ 60急速',
            '耐久: 50', '50 需要等级 90',
        ])
        stats = {'armor': 199, 'strength': 94, 'stamina': 1895, 'crit': 43, 'haste': 64}
        before = deepcopy(stats)
        data = separate_item_text(description_zh=description, stats=stats)
        self.assertEqual(data, {'description': '', 'description_zh': '', 'effects': []})
        self.assertEqual(stats, before)

    def test_crafted_description_keeps_flavor_without_random_stats_or_requirements(self):
        description = '\n'.join([
            '板甲', '静态属性说明：15护甲', '+5 [力量 or 智力]',
            '静态属性说明：+8 耐力', '+ 5 随机属性1', '+ 5 随机属性2', '耐久: 55 / 55',
            '拾取后绑定', '需要等级 90', '掉落于: 测试首领', '掉落几率: 4.13%',
            '“锻造者在护腕内侧刻下了自己的名字。”',
        ])
        effect = {'spell_id': 99, 'description_zh': '装备：攻击有几率使力量提高500，持续10秒。'}
        data = separate_item_text(description_zh=description, effects=[effect], stats={'strength': 103})
        self.assertEqual(data['description_zh'], '“锻造者在护腕内侧刻下了自己的名字。”')
        self.assertEqual(data['effects'], [effect])

    def test_english_tooltip_stats_are_not_equipment_flavor(self):
        data = separate_item_text(description='\n'.join([
            'Epic', 'Upgrade: Champion 6/6', 'Wrist Plate', '183 Armor',
            '+83 [Strength or Intellect]', '+1,629 Stamina', '+41 Critical Strike',
            '+60 Haste', '+5 Random Stat 1', 'Durability: 50 / 50', 'Requires Level 90',
            'Binds when equipped', 'An old promise, forged in steel.',
        ]))
        self.assertEqual(data['description'], 'An old promise, forged in steel.')

    def test_equipment_normalization_is_idempotent_and_preserves_raw_values(self):
        raw = '板甲\n183护甲\n+83 [力量 or 智力]\n+1629 耐力\n耐久: 50 / 50'
        item = {'catalog_type': 'equipment', 'description_zh': raw, 'variants': [
            {'stats': {'armor': 199, 'strength': 94, 'stamina': 1895}, 'effects': []},
            {'stats': {'armor': 183, 'strength': 83, 'stamina': 1629}, 'effects': []},
        ]}
        before = deepcopy(item['variants'])
        normalize_catalog_text(item)
        self.assertEqual(item['description_zh'], '')
        self.assertEqual(item['metadata']['raw_item_descriptions']['description_zh'], raw)
        for variant, original in zip(item['variants'], before):
            self.assertEqual(variant['stats'], original['stats'])
            self.assertEqual(variant['effects'], original['effects'])
        normalized = deepcopy(item)
        normalize_catalog_text(item)
        self.assertEqual(item, normalized)

    def test_live_english_metadata_formats_are_removed_alongside_chinese(self):
        for english in (
            'Upgrade Level: Champion 6/6\nDurability 50\n50 Requires Level 90',
            'Mythic+\nUpgrade Level: Myth 6/6\nBinds when picked up Wrist\nDurability 50\n50',
            'Mythic\nUpgrade Level: Myth 6/6',
            'Binds when picked up Head\nDurability 100 / 100',
            'Binds when equipped Wrist Plate\nDurability: 55 / 55 Requires Level 90',
        ):
            with self.subTest(description=english):
                data = separate_item_text(description=english,
                    description_zh='升级：勇士 6/6\n腕部 板甲\n耐久: 50 / 50 需要等级 90')
                self.assertEqual(data['description_zh'], '')
                self.assertEqual(data['description'], '')

    def test_english_metadata_cleanup_preserves_real_flavor_and_effects(self):
        effect = {'description': 'Equip: Your spells have a chance to increase Haste.',
                  'description_zh': '装备：你的法术有几率提高急速。'}
        data = separate_item_text(
            description='Mythic+\nUpgrade Level: Myth 6/6\nDurability 50 / 50\nAn old promise, forged in steel.',
            description_zh='升级：神话 6/6\n“铭刻于钢铁中的古老誓言。”', effects=[effect])
        self.assertEqual(data['description'], 'An old promise, forged in steel.')
        self.assertEqual(data['description_zh'], '“铭刻于钢铁中的古老誓言。”')
        self.assertEqual(data['effects'], [effect])

    def test_live_socket_weapon_and_combined_stat_rows_preserve_flavor(self):
        data = separate_item_text(description='\n'.join([
            'Prismatic Socket', '-Equipped Two-Hand Axe', 'Plate 199 Armor',
            '100 - 200 Damage Speed 3.60', 'Speed 3.60',
            '(48.5 damage per second) Requires Level 90',
            '-Equipped: Embellished (2) Feet Plate',
            '"The aqir fled before her vicious onslaught."',
        ]), description_zh='棱彩插槽\n双手 斧\n100 - 200点伤害\n（每秒伤害141.8点）\n掉落于\n“亚基虫群败走。”')
        self.assertEqual(data['description'], '"The aqir fled before her vicious onslaught."')
        self.assertEqual(data['description_zh'], '“亚基虫群败走。”')

    def test_set_members_do_not_become_flavor(self):
        data = separate_item_text(description='"A true story."\nWarlord Dominion (0/5)\nWarlord Helm\nWarlord Boots\n(2) Set: More Haste.',
            description_zh='“真正的说明。”\n督军的统御 (0/5)\n督军头盔\n督军战靴\n(2) 套装：提高急速。')
        self.assertEqual(data['description'], '"A true story."')
        self.assertEqual(data['description_zh'], '“真正的说明。”')

    def test_plain_stats_are_not_effects_or_description(self):
        data = separate_item_text(description_zh='无瑕迅捷榄石 榄石 物品等级：295 +17 急速 使用: 最大叠加:200 售价:3 10',
            effects=[{'description': '17 Haste'}], stats={'haste': 17}, names=['无瑕迅捷榄石'], enhancement=True)
        self.assertEqual(data, {'description': '', 'description_zh': '', 'effects': []})

    def test_effect_metadata_and_flavor_stay_separate(self):
        effect = {'spell_id': 99, 'game_build': '12.1.0', 'description_zh': '装备：攻击有几率提高500力量。'}
        data = separate_item_text(description_zh='一颗古老的宝石。\n装备：攻击有几率提高500力量。', effects=[effect], stats={'strength': 23})
        self.assertEqual(data['description_zh'], '一颗古老的宝石。')
        self.assertEqual(data['effects'], [effect])

    def test_old_enhancement_description_can_recover_effect(self):
        data = separate_item_text(description_zh='+23 主属性，每种不同颜色提高0.15%暴击效果。',
            stats={'strength': 23}, enhancement=True, recover_description_effects=True)
        self.assertEqual(data['description_zh'], '')
        self.assertEqual(data['effects'], [{'description_zh': '每种不同颜色提高0.15%暴击效果。'}])

    def test_chinese_effect_is_recovered_with_unique_constraint_kept_separate(self):
        data = separate_item_text(description_zh='坚韧鸡血石 PvP 物品等级：89 装备唯一：萨拉斯钻石（1） +23 主要属性，受到失控效果影响时 +5% 伤害减免 最大叠加:200 售价:7 50',
            names=['坚韧鸡血石'], stats={'strength': 23}, enhancement=True, recover_description_effects=True,
            effects=[{'spell_id': 99, 'description': '23 Primary / +5% Damage Reduction when affected by Crowd Control'}])
        self.assertEqual(data['description_zh'], '装备唯一：萨拉斯钻石（1）')
        self.assertEqual(data['effects'][0]['description_zh'], '受到失控效果影响时 +5% 伤害减免')
        self.assertEqual(data['effects'][0]['spell_id'], 99)

    def test_equipment_does_not_reuse_other_level_effect(self):
        data = separate_item_text(description='Equip: old 334 effect.',
            effects=[{'description_zh': '装备：321装等效果。'}], recover_description_effects=False)
        self.assertEqual(data['description'], '')
        self.assertEqual(data['effects'], [{'description_zh': '装备：321装等效果。'}])

    def test_enchant_static_stats_usage_and_proc_are_separate(self):
        text = '使用: 永久性地为头盔附魔强化加速祝福，使加速提高22。在户外击败敌人时获得1层充能。不能对物品等级低于120的物品使用。'
        data = separate_item_text(description_zh=text + '\n强化体系说明', effects=[{'description_zh': text}],
            stats={'speed': 22}, enhancement=True, recover_description_effects=True)
        self.assertEqual(data['effects'], [{'description_zh': '在户外击败敌人时获得1层充能。'}])
        self.assertEqual(data['description_zh'], '强化体系说明\n不能对物品等级低于120的物品使用。')

    def test_empty_effect_and_unmapped_static_value_are_not_effects(self):
        self.assertEqual(separate_item_text(effects=[{'description_zh': '提供下列属性：'}], enhancement=True)['effects'], [])
        data = separate_item_text(effects=[{'description': '41 Agi/Str & 27 Armor'}],
            stats={'strength': 41}, enhancement=True)
        self.assertEqual(data['effects'], [])
        self.assertEqual(data['description'], '静态属性说明：27 Armor')

    def test_normalization_preserves_original_and_is_idempotent(self):
        item = {'name_zh': '附魔护腕', 'catalog_type': 'enchant', 'description_zh': '+100 暴击',
            'variants': [{'stats': {'crit': 100}, 'effects': [{'description': '100 Crit'}]}]}
        normalize_catalog_text(item)
        self.assertEqual(item['description_zh'], '')
        self.assertEqual(item['variants'][0]['effects'], [])
        self.assertEqual(item['metadata']['raw_item_descriptions']['description_zh'], '+100 暴击')
        normalized = deepcopy(item)
        normalize_catalog_text(item)
        self.assertEqual(item, normalized)

    def test_parser_does_not_copy_effects_into_flavor(self):
        parsed = _tooltip_details({'name': '测试装备', 'tooltip': '<span class="q">一段风味描述。</span><br><span>装备：攻击获得500力量。</span>'})
        self.assertEqual(parsed['description_zh'], '一段风味描述。')
        self.assertEqual(parsed['effects'], [{'description_zh': '装备：攻击获得500力量。'}])


class ItemTextStorageTests(GearBuilderTestDataMixin, TestCase):
    def test_sidebar_uses_database_chinese_description_before_english(self):
        self.helm.description = 'An English item description.'
        self.helm.description_zh = '数据库已有的中文装备说明。'
        self.helm.save(update_fields=['description', 'description_zh'])
        data = serialize_item(self.helm, [self.hero], 'Warrior', 'Fury')
        self.assertEqual(data['description'], '数据库已有的中文装备说明。')

    def test_sidebar_does_not_switch_language_after_chinese_cleanup(self):
        self.helm.description = 'An English item description.'
        self.helm.description_zh = '升级：勇士 6/6\n腕部 板甲\n耐久: 50 / 50'
        self.helm.save(update_fields=['description', 'description_zh'])
        data = serialize_item(self.helm, [self.hero], 'Warrior', 'Fury')
        self.assertEqual(data['description'], '')
        self.helm.refresh_from_db()
        self.assertIn('腕部 板甲', self.helm.description_zh)

    def test_sidebar_reuses_preserved_database_chinese_without_fetching(self):
        self.helm.description = 'An English item description.'
        self.helm.description_zh = ''
        self.helm.metadata = {'raw_item_descriptions': {'description_zh':
            '物品等级：344\n数据库保留的中文装备说明。\n装备：旧装等的特效。'}}
        self.helm.save(update_fields=['description', 'description_zh', 'metadata'])
        before = type(self.helm).objects.filter(pk=self.helm.pk).values().get()
        data = serialize_item(self.helm, [self.hero], 'Warrior', 'Fury')
        self.assertEqual(data['description'], '数据库保留的中文装备说明。')
        self.assertEqual(data['variants'][0]['effects'], self.hero.effects_json)
        self.assertEqual(before, type(self.helm).objects.filter(pk=self.helm.pk).values().get())

    def test_sidebar_uses_english_only_when_chinese_source_is_absent(self):
        self.helm.description = 'An English item description.'
        self.helm.description_zh = ''
        self.assertEqual(serialize_item(self.helm, [self.hero], 'Warrior', 'Fury')['description'],
            'An English item description.')

    def _prepare_missing_chinese_flavor(self):
        self.helm.description = 'Prismatic Socket\n"The aqir fled before her vicious onslaught."'
        self.helm.description_zh = '物品等级：344\n棱彩插槽\n耐久: 50 / 50'
        self.helm.save(update_fields=['description', 'description_zh'])

    @patch('botend.management.commands.repair_gear_builder_descriptions.CurrentGearCatalogSource')
    def test_description_repair_audit_is_read_only_without_network(self, source):
        self._prepare_missing_chinese_flavor()
        before = type(self.helm).objects.get(pk=self.helm.pk).description_zh
        output = StringIO()
        call_command('repair_gear_builder_descriptions', stdout=output)
        source.assert_not_called()
        self.helm.refresh_from_db()
        self.assertEqual(self.helm.description_zh, before)
        self.assertIn('10001', output.getvalue())

    @patch('botend.management.commands.repair_gear_builder_descriptions.CurrentGearCatalogSource')
    def test_description_repair_only_updates_chinese_flavor_and_provenance(self, source):
        self._prepare_missing_chinese_flavor()
        before_item = type(self.helm).objects.filter(pk=self.helm.pk).values().get()
        before_variants = list(type(self.hero).objects.filter(item=self.helm).values())
        source.return_value._wowhead_tooltip.return_value = _tooltip_details({'tooltip':
            '<span class="q">物品等级：344</span><span class="q">“亚基虫群败走。”</span>'
            '<span>装备：其他装等的特效。</span>'})
        call_command('repair_gear_builder_descriptions', apply=True, stdout=StringIO())
        self.helm.refresh_from_db()
        self.assertEqual(self.helm.description_zh, '“亚基虫群败走。”')
        self.assertEqual(self.helm.metadata['raw_item_descriptions']['description'], before_item['description'])
        self.assertIn('locale=zhcn', self.helm.metadata['description_zh_source']['url'])
        after_item = type(self.helm).objects.filter(pk=self.helm.pk).values().get()
        for key in before_item.keys() - {'description_zh', 'metadata'}:
            self.assertEqual(before_item[key], after_item[key], key)
        self.assertEqual(before_variants, list(type(self.hero).objects.filter(item=self.helm).values()))
        self.assertEqual(serialize_item(self.helm, [self.hero], 'Warrior', 'Fury')['description'], '“亚基虫群败走。”')
        source.return_value._wowhead_tooltip.reset_mock()
        call_command('repair_gear_builder_descriptions', apply=True, stdout=StringIO())
        source.return_value._wowhead_tooltip.assert_not_called()

    @patch('botend.management.commands.repair_gear_builder_descriptions.CurrentGearCatalogSource')
    def test_description_repair_missing_source_preserves_original(self, source):
        self._prepare_missing_chinese_flavor()
        before = type(self.helm).objects.filter(pk=self.helm.pk).values().get()
        source.return_value._wowhead_tooltip.return_value = {'description_zh': 'Item Level: 344'}
        output = StringIO()
        call_command('repair_gear_builder_descriptions', apply=True, stdout=output)
        self.assertEqual(before, type(self.helm).objects.filter(pk=self.helm.pk).values().get())
        self.assertIn('"源站无中文说明": [10001]', output.getvalue())

    @patch('botend.management.commands.repair_gear_builder_descriptions.CurrentGearCatalogSource')
    def test_description_repair_respects_item_filter(self, source):
        self._prepare_missing_chinese_flavor()
        call_command('repair_gear_builder_descriptions', apply=True, item_id=[99999], stdout=StringIO())
        source.assert_not_called()

    @patch('botend.management.commands.repair_gear_builder_descriptions.CurrentGearCatalogSource')
    def test_description_repair_failed_request_keeps_original_and_reports_item(self, source):
        self._prepare_missing_chinese_flavor()
        before = type(self.helm).objects.filter(pk=self.helm.pk).values().get()
        source.return_value._wowhead_tooltip.side_effect = CatalogSourceError('中文源返回 404')
        output = StringIO()
        call_command('repair_gear_builder_descriptions', apply=True, stdout=output)
        self.assertEqual(before, type(self.helm).objects.filter(pk=self.helm.pk).values().get())
        self.assertIn('"请求失败": [{"物品ID": 10001', output.getvalue())
        self.assertIn('中文源返回 404', output.getvalue())

    def test_catalog_does_not_fall_back_to_english_tooltip_metadata(self):
        self.helm.description_zh = '升级：勇士 6/6\n腕部 板甲\n183护甲\n耐久: 50 / 50 需要等级 90'
        self.helm.description = 'Mythic+\nUpgrade Level: Champion 6/6\nBinds when picked up Wrist\nDurability 50 / 50 Requires Level 90'
        self.helm.save(update_fields=['description', 'description_zh'])
        before = deepcopy(self.hero.stats_json)
        data = serialize_item(self.helm, [self.hero], 'Warrior', 'Fury')
        self.assertEqual(data['description'], '')
        self.assertEqual(data['variants'][0]['stats'], before)
        self.helm.description_zh += '\n“铭刻于钢铁中的古老誓言。”'
        self.assertEqual(serialize_item(self.helm, [self.hero], 'Warrior', 'Fury')['description'],
            '“铭刻于钢铁中的古老誓言。”')

    def test_catalog_projects_clean_description_without_changing_stored_stats(self):
        raw = '升级：勇士 6/6\n腕部 板甲\n183护甲\n+83 [力量 or 智力]\n+1629 耐力\n耐久: 50 / 50 需要等级 90'
        self.helm.description_zh = raw
        self.helm.save(update_fields=['description_zh'])
        before = deepcopy(self.hero.stats_json)
        data = serialize_item(self.helm, [self.hero], 'Warrior', 'Fury')
        self.assertEqual(data['description'], '')
        self.assertIn('升级：勇士 6/6', data['variants'][0]['tooltip'])
        self.assertNotIn('+83 [力量 or 智力]', data['variants'][0]['tooltip'])
        self.helm.refresh_from_db()
        self.hero.refresh_from_db()
        self.assertEqual(self.helm.description_zh, raw)
        self.assertEqual(self.hero.stats_json, before)

    def test_shared_api_keeps_flavor_and_effect_fields_distinct(self):
        self.helm.description_zh = '古老的头盔。\n装备：攻击有几率开启裂隙。'
        self.helm.save(update_fields=['description_zh'])
        data = serialize_item(self.helm, [self.hero], 'Warrior', 'Fury')
        self.assertEqual(data['description'], '古老的头盔。')
        self.assertNotIn('物品等级', data['description'])
        self.assertEqual(data['variants'][0]['effects'], [{'description_zh': '装备：攻击有几率开启裂隙。'}])
        self.assertEqual(data['text_schema_version'], 2)

    def test_audit_is_read_only_and_apply_retains_stats(self):
        self.gem_item.description_zh = '迅捷宝石\n+120 急速\n使用: 最大叠加:200 售价:3 10'
        self.gem_item.save(update_fields=['description_zh'])
        before = deepcopy(self.gem.stats_json)
        call_command('normalize_gear_builder_text', stdout=StringIO())
        self.gem_item.refresh_from_db()
        self.assertIn('最大叠加', self.gem_item.description_zh)
        call_command('normalize_gear_builder_text', apply=True, stdout=StringIO())
        self.gem_item.refresh_from_db()
        self.gem.refresh_from_db()
        self.assertEqual(self.gem_item.description_zh, '')
        self.assertIn('最大叠加', self.gem_item.metadata['raw_item_descriptions']['description_zh'])
        self.assertEqual(self.gem.stats_json, before)
        output = StringIO()
        call_command('normalize_gear_builder_text', stdout=output)
        self.assertIn('"物品数": 0', output.getvalue())
        self.assertIn('"变体数": 0', output.getvalue())
