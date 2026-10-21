from types import SimpleNamespace
from unittest.mock import Mock

from django.test import SimpleTestCase

from botend.management.commands.backfill_simc_benchmark_display_metadata import _display_metadata
from botend.management.commands.fetch_item_metadata import (
    Command,
    WOWHEAD_PTR_TOOLTIP_API,
    _choose_icon,
)


class FetchItemMetadataCommandTests(SimpleTestCase):
    def test_ptr_tooltip_uses_ptr_endpoint_and_preserves_attributes_and_effect(self):
        session = Mock()
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            'name': 'PTR Trinket',
            'quality': 4,
            'icon': 'inv_trinket_01',
            'tooltip': (
                '<table><tr><td><b>PTR Trinket</b><br />'
                '物品等级 300<br />+321 敏捷<br />+123 暴击<br />'
                '装备：你的法术有几率触发海潮。<br />售价</td></tr></table>'
            ),
        }
        session.get.return_value = response

        data = Command()._fetch_wowhead_tooltip_api(session, 270160, 'zhCN', ptr=True)

        self.assertEqual(
            session.get.call_args.args[0],
            WOWHEAD_PTR_TOOLTIP_API.format(item_id=270160, locale='zhCN'),
        )
        self.assertEqual(data['name'], 'PTR Trinket')
        self.assertEqual(data['icon'], 'inv_trinket_01')
        self.assertIn('+321 敏捷', data['description'])
        self.assertIn('+123 暴击', data['description'])
        self.assertIn('装备：你的法术有几率触发海潮。', data['description'])
        self.assertNotIn('物品等级 300', data['description'])

    def test_real_tooltip_icon_replaces_questionmark(self):
        self.assertEqual(
            _choose_icon('', 'inv_misc_questionmark', 'inv_gear_real', ''),
            'inv_gear_real',
        )

    def test_benchmark_metadata_preserves_available_chinese_tooltip(self):
        item = SimpleNamespace(
            name_zh='测试饰品', name='Test Trinket', icon='inv_trinket_01',
            description_zh='物品等级 219\n装备 Trinket',
            description='Mythic\nItem Level 334\n+179 Agility\nEquip: Deal damage.',
        )

        label, effect, icon_url = _display_metadata({270160: item}, {'gear_swap': {'item_id': 270160}})

        self.assertEqual(label, '测试饰品')
        self.assertEqual(effect, item.description_zh)
        self.assertEqual(icon_url, '/static/wow_icons/small/inv_trinket_01.jpg')

    def test_benchmark_metadata_falls_back_to_english_without_chinese_tooltip(self):
        item = SimpleNamespace(
            name_zh='', name='Test Trinket', icon='inv_trinket_01',
            description_zh=' ', description='Equip: Deal damage.',
        )

        label, effect, icon_url = _display_metadata({270160: item}, {'gear_swap': {'item_id': 270160}})

        self.assertEqual(label, item.name)
        self.assertEqual(effect, item.description)
        self.assertEqual(icon_url, '/static/wow_icons/small/inv_trinket_01.jpg')
