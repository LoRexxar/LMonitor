"""模拟场景在前后台使用相同的展示规则。"""

from django import template

from botend.services.simc_scenario_display import scenario_label

register = template.Library()
register.simple_tag(scenario_label, name='simc_scenario_label')
