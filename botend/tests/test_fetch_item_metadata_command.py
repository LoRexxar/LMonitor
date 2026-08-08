from unittest.mock import Mock

from django.test import SimpleTestCase

from botend.management.commands.fetch_item_metadata import (
    Command,
    WOWHEAD_PTR_TOOLTIP_API,
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
