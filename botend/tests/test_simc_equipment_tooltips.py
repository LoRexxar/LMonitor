from pathlib import Path

from django.test import TestCase, override_settings

from botend.models import WowItemSnapshot
from botend.services.simc_player_config import parse_manual_player_config
from botend.services.simc_result_analysis import parse_simc_html_report


ROOT = Path(__file__).resolve().parents[2]


class SimcEquipmentTooltipContractTests(TestCase):
    @override_settings(OSS_CONFIG={
        'base_url': 'https://oss.wowdaily.cn/',
        'wow_icon_prefix': 'wow_icons_oss',
    })
    def test_profile_result_and_benchmark_equipment_share_item_description_tooltips(self):
        WowItemSnapshot.objects.create(
            item_id=249952,
            name='Night Ender\'s Tusks',
            name_zh='夜幕终结者的獠牙',
            description='Equip: English effect.',
            description_zh='装备：中文装备属性与特效。',
            icon='inv_test_equipment_icon',
        )
        profile = parse_manual_player_config(
            '\n'.join([
                'warrior="TooltipTest"',
                'level=90',
                'spec=fury',
                '# Night Ender\'s Tusks (289)',
                'head=,id=249952,ilevel=289',
            ]),
            'warrior_fury',
        )
        self.assertEqual(profile['equipment'][0]['display_description'], '装备：中文装备属性与特效。')
        expected_icon_url = 'https://oss.wowdaily.cn/wow_icons_oss/small/inv_test_equipment_icon.jpg'
        self.assertEqual(profile['equipment'][0]['icon_url'], expected_icon_url)

        report = parse_simc_html_report('''
            <div class="player">
              <h2>TooltipTest: 100 dps</h2>
              <div class="player-section">
                <h3>Gear</h3>
                <table class="sc">
                  <tr><th>Slot</th><th>Item</th></tr>
                  <tr><td>Head</td><td><a href="https://www.wowhead.com/item=249952?ilvl=289">Night Ender's Tusks</a></td></tr>
                </table>
              </div>
            </div>
        ''')
        gear = next(section for section in report['sections'] if section['key'] == 'gear')
        item_cell = gear['tables'][0]['rows'][1][1]
        self.assertEqual(item_cell['item']['display_description'], '装备：中文装备属性与特效。')
        self.assertEqual(item_cell['item']['icon_url'], expected_icon_url)

        shared_js = (ROOT / 'static/shared/js/wow-item-tooltip.js').read_text(encoding='utf-8')
        shared_css = (ROOT / 'static/shared/css/wow-item-tooltip.css').read_text(encoding='utf-8')
        profile_js = (ROOT / 'static/dashboard/js/main.js').read_text(encoding='utf-8')
        result_js = (ROOT / 'static/dashboard/js/simc-result-report.js').read_text(encoding='utf-8')
        benchmark_js = (ROOT / 'static/portal/js/simc-benchmarks.js').read_text(encoding='utf-8')
        templates = '\n'.join(
            (ROOT / path).read_text(encoding='utf-8')
            for path in (
                'templates/dashboard/index.html',
                'templates/dashboard/simc_detail.html',
                'templates/portal/simc_benchmark_results.html',
            )
        )
        for source in (profile_js, result_js, benchmark_js):
            self.assertIn('data-wow-item-tooltip', source)
            self.assertIn('display_description', source)
            self.assertIn('icon_url', source)
            self.assertIn('wow-item-icon', source)
        for token in ('pointerover', 'focusin', 'click', 'role="tooltip"'):
            self.assertIn(token, shared_js)
        self.assertIn('.wow-item-tooltip', shared_css)
        self.assertGreaterEqual(templates.count('shared/js/wow-item-tooltip.js'), 3)
        self.assertGreaterEqual(templates.count('shared/css/wow-item-tooltip.css'), 3)
