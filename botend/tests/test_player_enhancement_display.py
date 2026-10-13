from copy import deepcopy

from django.test import TestCase

from botend.models import WowItemSnapshot, WowSpellSnapshot, WowSpellEffectSnapshot
from botend.services.spec_stats_service import (
    _aggregate_enchants, _aggregate_gems, _normalize_gear_items,
)


class PlayerEnhancementDisplayTests(TestCase):
    def test_id_only_player_gear_uses_central_names_without_crossing_id_namespaces(self):
        # Production player 107211: WCL-shaped details have IDs, not RIO names.
        WowItemSnapshot.objects.create(
            item_id=243981, enchantment_id=7991, catalog_type='enchant',
            name='Empowered Blessing of Speed', name_zh='附魔头盔 - 强化加速祝福',
        )
        WowItemSnapshot.objects.create(
            item_id=240914, catalog_type='gem', name_zh='无瑕致命青金石',
        )
        WowItemSnapshot.objects.create(item_id=7991, name_zh='无关同号物品')
        WowItemSnapshot.objects.create(item_id=999001, enchantment_id=999003, name_zh='歧义卷轴甲')
        WowItemSnapshot.objects.create(item_id=999002, enchantment_id=999003, name_zh='歧义卷轴乙')
        for sid, eid, effect, name in (
            (326805, 6241, 53, '鲜红符文'),
            (1237006, 8052, 54, '萨拉斯凤凰之油'),
        ):
            WowSpellSnapshot.objects.create(branch='wow', locale='zhCN', spell_id=sid, name_zh=name)
            WowSpellEffectSnapshot.objects.create(branch='wow', locale='zhCN', spell_id=sid,
                                                  effect_index=0, effect=effect, misc_value=eid)
        raw = [
            {'id': 271474, 'slot': 'head', 'itemLevel': '334',
             'enchants_detail': [{'enchantment_id': 7991, 'kind': 'permanent'}]},
            {'id': 271445, 'slot': 'waist', 'gems_detail': [{'id': '240914'}]},
            {'id': 237834, 'slot': 'wrist', 'gems_detail': [{'id': '240914'}]},
            {'id': 237846, 'slot': 'main_hand', 'enchants_detail': [
                {'enchantment_id': 999004, 'kind': 'temporary'},
                {'enchantment_id': 999003, 'kind': 'permanent'},
                {'id': 12345, 'name': '已提供的旧格式附魔'},
                {'enchantment_id': 6241, 'kind': 'permanent'},
                {'enchantment_id': 8052, 'kind': 'temporary'},
            ]},
        ]
        original = deepcopy(raw)
        gear = _normalize_gear_items(raw)
        gems = _aggregate_gems(gear)
        enchants = _aggregate_enchants(gear)
        self.assertEqual([(g['name'], g['count']) for g in gems], [('无瑕致命青金石', 2)])
        self.assertEqual(enchants[0]['name'], '附魔头盔 - 强化加速祝福')
        self.assertEqual(enchants[0]['id'], 243981)
        self.assertEqual(enchants[0]['enchantment_id'], 7991)
        self.assertEqual(enchants[1]['name'], '附魔 #999004（名称待补全）')
        self.assertIsNone(enchants[1]['id'])
        self.assertEqual(enchants[2]['name'], '附魔 #999003（名称待补全）')
        self.assertIsNone(enchants[2]['id'])
        self.assertEqual(enchants[3]['name'], '已提供的旧格式附魔')
        self.assertEqual([e['name'] for e in enchants[4:]], ['鲜红符文', '萨拉斯凤凰之油'])
        self.assertTrue(all(e['id'] is None for e in enchants[4:]))
        self.assertEqual(raw, original)
