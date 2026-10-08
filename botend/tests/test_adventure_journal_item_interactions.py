"""装备名称/图标只触发站内详情，不提供外站导航。"""
from bs4 import BeautifulSoup
from django.template.loader import render_to_string
from django.test import SimpleTestCase


class JournalItemInteractionTests(SimpleTestCase):
    def test_item_identity_is_keyboard_accessible_local_tooltip_trigger(self):
        for details in (None, {
            'complete': True, 'item_level': 100,
            'lines': ['物品等级 100', '+12 耐力', '装备：测试特效'],
            'stats': ['+12 耐力'], 'effects': ['装备：测试特效'],
        }):
            with self.subTest(details_loaded=bool(details)):
                html = render_to_string('portal/adventure_journal_detail.html', {
                    'tab': 'loot', 'instance': {'id': 10, 'name': '测试副本'},
                    'source': {'key': 'current', 'build': '12.1.0.69587'},
                    'loot': [{
                        'item_id': 60, 'name': '测试饰品', 'quality': 4,
                        'url': 'https://www.wowhead.com/cn/item=60',
                        'icon_url': '/test-icon.jpg', 'details': details,
                    }],
                })
                dom = BeautifulSoup(html, 'html.parser')
                trigger = dom.select_one('.journal-loot-link')
                self.assertEqual(trigger.name, 'button')
                self.assertEqual(trigger.get('type'), 'button')
                self.assertFalse(trigger.has_attr('disabled'))
                self.assertFalse(trigger.has_attr('href'))
                self.assertFalse(trigger.has_attr('target'))
                self.assertEqual(trigger['data-tooltip-kind'], 'item')
                self.assertEqual(trigger['data-tooltip-id'], '60')
                self.assertEqual(trigger['aria-haspopup'], 'dialog')
                self.assertEqual(trigger.select_one('strong').text, '测试饰品')
                self.assertEqual(trigger.select_one('img')['src'], '/test-icon.jpg')
                self.assertEqual(trigger['data-wow-item-tooltip-name'], '测试饰品')
                self.assertIn('+12 耐力' if details else '正在加载', trigger['data-wow-item-tooltip'])
                self.assertFalse(dom.select('a[href*="wowhead.com"]'))
                self.assertTrue(dom.select('script[src*="shared/js/wow-item-tooltip.js"]'))
                self.assertTrue(dom.select('link[href*="shared/css/wow-item-tooltip.css"]'))
