"""场景名去重、自定义名称保留及模板转义回归。"""

from django.template import Context, Template
from django.test import SimpleTestCase

from botend.services.simc_scenario_display import scenario_label


class ScenarioLabelTests(SimpleTestCase):
    def test_generated_names_show_each_condition_once(self):
        for name in ('1 目标 · 300 秒', '1目标/300秒', '1 目标 300 秒', '单体', '单目标 · 300秒', ''):
            with self.subTest(name=name):
                self.assertEqual(scenario_label(name, 1, 300), '1 目标 · 300 秒')

    def test_custom_names_and_distinct_durations_are_preserved(self):
        self.assertEqual(scenario_label('爆发窗口 · 20目标 · 40秒', 20, 40), '爆发窗口 · 20 目标 · 40 秒')
        self.assertEqual(scenario_label('持续战斗', 20, 300), '持续战斗 · 20 目标 · 300 秒')
        self.assertEqual(scenario_label('每300秒增援', 5, 600), '每300秒增援 · 5 目标 · 600 秒')

    def test_saved_parameters_override_old_generated_names(self):
        self.assertEqual(scenario_label('5目标 · 300秒', 1, 40), '1 目标 · 40 秒')
        self.assertEqual(scenario_label('单体', 5, 300), '5 目标 · 300 秒')

    def test_numeric_normalization_and_missing_historical_params(self):
        self.assertEqual(scenario_label('5.0目标 / 300.00秒', 5, 300.0), '5 目标 · 300 秒')
        self.assertEqual(scenario_label('历史场景', None, None), '历史场景')
        self.assertEqual(scenario_label('5目标', None, 300), '5目标 · 300 秒')

    def test_tag_escapes_custom_name(self):
        rendered = Template('{% load simc_display %}{% simc_scenario_label name targets duration %}').render(
            Context({'name': '<script>坏名称</script>', 'targets': 5, 'duration': 300}))
        self.assertIn('&lt;script&gt;', rendered)
        self.assertNotIn('<script>', rendered)
