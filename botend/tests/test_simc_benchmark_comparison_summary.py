import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_JS = ROOT / 'static/dashboard/js/simc-benchmark-dashboard.js'


class SimcBenchmarkComparisonSummaryTests(unittest.TestCase):
    def test_current_comparison_overrides_are_projected_for_editor_summary(self):
        script = f"""
const {{ comparisonSummaryData }} = require({json.dumps(str(DASHBOARD_JS))});
const summary = comparisonSummaryData({{
  label: '牧师能量灌注',
  simulation_params: {{
    desired_targets: 5,
    max_time: 40,
    fight_style: 'CastingPatchwerk',
    use_class_raid_buff: false,
    raid_buffs: [],
    extra_options: ['power_infusion'],
    profile_overrides: {{flask: 'disabled'}},
    additional_simc_input: 'foo=1\\nbar=2',
  }},
}}, {{
  fight_styles: [{{value: 'CastingPatchwerk', label: '施法木桩'}}],
  raid_buffs: [{{value: 'arcane_intellect', label: '奥术智慧'}}],
  extra_options: [{{value: 'power_infusion', label: '能量灌注'}}],
  consumables: {{flask: [{{value: 'disabled', label: '禁用'}}]}},
}});
process.stdout.write(JSON.stringify(summary));
"""
        result = subprocess.run(
            ['node', '-e', script], capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            'label': '牧师能量灌注',
            'rows': [
                {'label': '目标数', 'value': '5'},
                {'label': '战斗时间', 'value': '40 秒'},
                {'label': '战斗类型', 'value': '施法木桩'},
                {'label': '职业自身团队增益', 'value': '关闭'},
                {'label': '额外 Raid Buffs', 'value': '清空'},
                {'label': '额外选项', 'value': '能量灌注'},
                {'label': '合剂', 'value': '禁用'},
                {'label': '附加 SimC 输入', 'value': '已设置 2 行'},
            ],
            'inheritance': '其余配置继承各基准场景',
        })


if __name__ == '__main__':
    unittest.main()
