"""装备、宝石、附魔及美化文本分离的回归验证。"""
from copy import deepcopy
from io import StringIO

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from botend.services.wow_item_text import normalize_catalog_text, separate_item_text
from botend.services.gear_builder_catalog_source import _tooltip_details
from botend.services.gear_builder import serialize_item
from botend.tests.test_gear_builder import GearBuilderTestDataMixin


class ItemTextSeparationTests(SimpleTestCase):
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
