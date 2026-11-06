"""Database-free DOM and presentation contracts for the RIO observer."""
from collections import Counter
from pathlib import Path
import re

from bs4 import BeautifulSoup
from django.conf import settings
from django.test import SimpleTestCase

# Frozen from the pre-redesign template; intentionally independent of Git at runtime.
LEGACY_CONTRACT = {'rio-statistics': {'tag': 'div',
                    'attrs': {},
                    'options': [{'value': ''},
                                {'value': 'cn'},
                                {'value': '30'},
                                {'value': '14'},
                                {'value': '0'},
                                {'value': '1'},
                                {'value': '0.1'},
                                {'value': '5'},
                                {'value': '10'},
                                {'value': '25'},
                                {'value': '40'},
                                {'value': 'mythic'},
                                {'value': 'heroic'},
                                {'value': 'normal'}]},
 'rio-version': {'tag': 'p', 'attrs': {'role': 'status'}, 'options': []},
 'rio-tab-overview': {'tag': 'button',
                      'attrs': {'role': 'tab',
                                'aria-selected': 'true',
                                'aria-controls': 'rio-overview',
                                'data-tab': 'overview'},
                      'options': []},
 'rio-tab-runs': {'tag': 'button',
                  'attrs': {'role': 'tab',
                            'aria-selected': 'false',
                            'aria-controls': 'rio-runs',
                            'data-tab': 'runs',
                            'tabindex': '-1'},
                  'options': []},
 'rio-tab-heatmaps': {'tag': 'button',
                      'attrs': {'role': 'tab',
                                'aria-selected': 'false',
                                'aria-controls': 'rio-heatmaps',
                                'data-tab': 'heatmaps',
                                'tabindex': '-1'},
                      'options': []},
 'rio-tab-raids': {'tag': 'button',
                   'attrs': {'role': 'tab',
                             'aria-selected': 'false',
                             'aria-controls': 'rio-raids',
                             'data-tab': 'raids',
                             'tabindex': '-1'},
                   'options': []},
 'rio-filters': {'tag': 'form',
                 'attrs': {'aria-label': '统计筛选'},
                 'options': [{'value': ''},
                             {'value': 'cn'},
                             {'value': '30'},
                             {'value': '14'},
                             {'value': '0'},
                             {'value': '1'},
                             {'value': '0.1'},
                             {'value': '5'},
                             {'value': '10'},
                             {'value': '25'},
                             {'value': '40'},
                             {'value': 'mythic'},
                             {'value': 'heroic'},
                             {'value': 'normal'}]},
 'rio-season': {'tag': 'select', 'attrs': {'name': 'season'}, 'options': [{'value': ''}]},
 'rio-region': {'tag': 'select', 'attrs': {'name': 'region'}, 'options': [{'value': 'cn'}]},
 'rio-overview-filters': {'tag': 'div',
                          'attrs': {},
                          'options': [{'value': '30'},
                                      {'value': '14'},
                                      {'value': '0'},
                                      {'value': '1'},
                                      {'value': '0.1'},
                                      {'value': '5'},
                                      {'value': '10'},
                                      {'value': '25'},
                                      {'value': '40'}]},
 'rio-days': {'tag': 'select',
              'attrs': {'name': 'days'},
              'options': [{'value': '30'}, {'value': '14'}, {'value': '0'}]},
 'rio-percentile': {'tag': 'select',
                    'attrs': {'name': 'percentile'},
                    'options': [{'value': '1'},
                                {'value': '0.1'},
                                {'value': '5'},
                                {'value': '10'},
                                {'value': '25'},
                                {'value': '40'}]},
 'rio-heatmap-filters': {'tag': 'div', 'attrs': {'hidden': ''}, 'options': []},
 'rio-dungeon-filter': {'tag': 'label', 'attrs': {}, 'options': []},
 'rio-dungeon': {'tag': 'select', 'attrs': {'name': 'dungeon'}, 'options': []},
 'rio-level': {'tag': 'select', 'attrs': {'name': 'level'}, 'options': []},
 'rio-period': {'tag': 'select', 'attrs': {'name': 'period'}, 'options': []},
 'rio-level-range': {'tag': 'div', 'attrs': {'hidden': ''}, 'options': []},
 'rio-min_level': {'tag': 'input',
                   'attrs': {'type': 'number', 'min': '2', 'max': '99', 'value': '10'},
                   'options': []},
 'rio-max_level': {'tag': 'input',
                   'attrs': {'type': 'number', 'min': '2', 'max': '99', 'value': '20'},
                   'options': []},
 'rio-apply-level-range': {'tag': 'button', 'attrs': {'type': 'button'}, 'options': []},
 'rio-week-range': {'tag': 'div', 'attrs': {'hidden': ''}, 'options': []},
 'rio-week_start': {'tag': 'select', 'attrs': {}, 'options': []},
 'rio-week_end': {'tag': 'select', 'attrs': {}, 'options': []},
 'rio-raid-filters': {'tag': 'div',
                      'attrs': {'hidden': ''},
                      'options': [{'value': 'mythic'}, {'value': 'heroic'}, {'value': 'normal'}]},
 'rio-raid': {'tag': 'select', 'attrs': {'name': 'raid'}, 'options': []},
 'rio-difficulty': {'tag': 'select',
                    'attrs': {'name': 'difficulty'},
                    'options': [{'value': 'mythic'}, {'value': 'heroic'}, {'value': 'normal'}]},
 'rio-share': {'tag': 'button', 'attrs': {'type': 'button'}, 'options': []},
 'rio-advanced': {'tag': 'details', 'attrs': {'hidden': ''}, 'options': []},
 'rio-filter-summary': {'tag': 'span', 'attrs': {}, 'options': []},
 'rio-include_classes': {'tag': 'select', 'attrs': {'multiple': '', 'size': '3'}, 'options': []},
 'rio-exclude_classes': {'tag': 'select', 'attrs': {'multiple': '', 'size': '3'}, 'options': []},
 'rio-include_specs': {'tag': 'select', 'attrs': {'multiple': '', 'size': '3'}, 'options': []},
 'rio-exclude_specs': {'tag': 'select', 'attrs': {'multiple': '', 'size': '3'}, 'options': []},
 'rio-include_affixes': {'tag': 'select', 'attrs': {'multiple': '', 'size': '3'}, 'options': []},
 'rio-exclude_affixes': {'tag': 'select', 'attrs': {'multiple': '', 'size': '3'}, 'options': []},
 'rio-death_classes': {'tag': 'select',
                       'attrs': {'multiple': '', 'size': '3', 'data-death-filter': ''},
                       'options': []},
 'rio-exclude_death_classes': {'tag': 'select',
                               'attrs': {'multiple': '', 'size': '3', 'data-death-filter': ''},
                               'options': []},
 'rio-death_specs': {'tag': 'select',
                     'attrs': {'multiple': '', 'size': '3', 'data-death-filter': ''},
                     'options': []},
 'rio-exclude_death_specs': {'tag': 'select',
                             'attrs': {'multiple': '', 'size': '3', 'data-death-filter': ''},
                             'options': []},
 'rio-item_min': {'tag': 'input',
                  'attrs': {'type': 'number',
                            'min': '0',
                            'max': '5000',
                            'step': '0.1',
                            'placeholder': '不限'},
                  'options': []},
 'rio-item_max': {'tag': 'input',
                  'attrs': {'type': 'number',
                            'min': '0',
                            'max': '5000',
                            'step': '0.1',
                            'placeholder': '不限'},
                  'options': []},
 'rio-deaths_min': {'tag': 'input',
                    'attrs': {'type': 'number', 'min': '0', 'max': '5000', 'placeholder': '不限'},
                    'options': []},
 'rio-deaths_max': {'tag': 'input',
                    'attrs': {'type': 'number', 'min': '0', 'max': '5000', 'placeholder': '不限'},
                    'options': []},
 'rio-timer_min': {'tag': 'input',
                   'attrs': {'type': 'number',
                             'min': '0',
                             'max': '10',
                             'step': '0.01',
                             'placeholder': '0'},
                   'options': []},
 'rio-timer_max': {'tag': 'input',
                   'attrs': {'type': 'number',
                             'min': '0',
                             'max': '10',
                             'step': '0.01',
                             'placeholder': '1'},
                   'options': []},
 'rio-apply': {'tag': 'button', 'attrs': {'type': 'button'}, 'options': []},
 'rio-clear-advanced': {'tag': 'button', 'attrs': {'type': 'button'}, 'options': []},
 'rio-status': {'tag': 'p', 'attrs': {'role': 'status', 'aria-live': 'polite'}, 'options': []},
 'rio-error': {'tag': 'div', 'attrs': {'hidden': ''}, 'options': []},
 'rio-error-title': {'tag': 'h2', 'attrs': {}, 'options': []},
 'rio-error-message': {'tag': 'p', 'attrs': {}, 'options': []},
 'rio-retry': {'tag': 'button', 'attrs': {'type': 'button'}, 'options': []},
 'rio-prepare': {'tag': 'button', 'attrs': {'type': 'button', 'hidden': ''}, 'options': []},
 'rio-overview': {'tag': 'section',
                  'attrs': {'role': 'tabpanel', 'aria-labelledby': 'rio-tab-overview'},
                  'options': []},
 'rio-population-chart': {'tag': 'div', 'attrs': {}, 'options': []},
 'rio-population': {'tag': 'dd', 'attrs': {}, 'options': []},
 'rio-score-label': {'tag': 'dt', 'attrs': {}, 'options': []},
 'rio-score': {'tag': 'dd', 'attrs': {}, 'options': []},
 'rio-date': {'tag': 'dd', 'attrs': {}, 'options': []},
 'rio-change-label': {'tag': 'dt', 'attrs': {}, 'options': []},
 'rio-change': {'tag': 'dd', 'attrs': {}, 'options': []},
 'rio-score-chart': {'tag': 'div', 'attrs': {}, 'options': []},
 'rio-percentiles': {'tag': 'tbody', 'attrs': {}, 'options': []},
 'rio-history': {'tag': 'tbody', 'attrs': {}, 'options': []},
 'rio-runs': {'tag': 'section',
              'attrs': {'role': 'tabpanel', 'aria-labelledby': 'rio-tab-runs', 'hidden': ''},
              'options': []},
 'rio-runs-chart': {'tag': 'div', 'attrs': {}, 'options': []},
 'rio-runs-total': {'tag': 'dd', 'attrs': {}, 'options': []},
 'rio-runs-timed': {'tag': 'dd', 'attrs': {}, 'options': []},
 'rio-runs-rate': {'tag': 'dd', 'attrs': {}, 'options': []},
 'rio-runs-note': {'tag': 'p', 'attrs': {}, 'options': []},
 'rio-runs-rows': {'tag': 'tbody', 'attrs': {}, 'options': []},
 'rio-heatmaps': {'tag': 'section',
                  'attrs': {'role': 'tabpanel',
                            'aria-labelledby': 'rio-tab-heatmaps',
                            'hidden': ''},
                  'options': []},
 'rio-zero': {'tag': 'input', 'attrs': {'type': 'checkbox'}, 'options': []},
 'rio-opacity': {'tag': 'input',
                 'attrs': {'type': 'range', 'min': '20', 'max': '100', 'value': '80'},
                 'options': []},
 'rio-floor': {'tag': 'select', 'attrs': {}, 'options': []},
 'rio-source-map': {'tag': 'a',
                    'attrs': {'target': '_blank', 'rel': ['noopener', 'noreferrer']},
                    'options': []},
 'rio-map-zoom-in': {'tag': 'button',
                     'attrs': {'type': 'button', 'aria-label': '放大地图'},
                     'options': []},
 'rio-map-zoom-out': {'tag': 'button',
                      'attrs': {'type': 'button', 'aria-label': '缩小地图'},
                      'options': []},
 'rio-map-reset': {'tag': 'button', 'attrs': {'type': 'button'}, 'options': []},
 'rio-map-scale': {'tag': 'span', 'attrs': {'aria-label': '缩放比例'}, 'options': []},
 'rio-map-message': {'tag': 'p', 'attrs': {'role': 'status'}, 'options': []},
 'rio-heatmap': {'tag': 'canvas', 'attrs': {'aria-label': '副本热力图', 'role': 'img'}, 'options': []},
 'rio-floor-empty': {'tag': 'p', 'attrs': {'hidden': ''}, 'options': []},
 'rio-map-stats': {'tag': 'dl', 'attrs': {}, 'options': []},
 'rio-raids': {'tag': 'section',
               'attrs': {'role': 'tabpanel', 'aria-labelledby': 'rio-tab-raids', 'hidden': ''},
               'options': []},
 'rio-raid-heading': {'tag': 'h2', 'attrs': {}, 'options': []},
 'rio-raid-chart': {'tag': 'div', 'attrs': {}, 'options': []},
 'rio-raid-rows': {'tag': 'tbody', 'attrs': {}, 'options': []},
 'rio-methodology': {'tag': 'details', 'attrs': {}, 'options': []}}
NEW_IDS = set("""rio-context rio-module-title rio-module-description rio-filter-disclosure
rio-heatmap-title rio-heatmap-description rio-opacity-value
rio-population-range rio-score-range rio-runs-availability""".split())


class RioStatisticsPresentationTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        root = Path(settings.BASE_DIR)
        cls.source = (root / 'templates/portal/rio_statistics.html').read_text()
        cls.dom = BeautifulSoup(cls.source, 'html.parser')
        cls.css = (root / 'static/portal/css/rio-statistics.css').read_text()

    def test_legacy_ids_and_control_contracts_are_preserved(self):
        counts = Counter(n['id'] for n in self.dom.select('[id]'))
        self.assertEqual(set(counts), set(LEGACY_CONTRACT) | NEW_IDS)
        self.assertTrue(all(count == 1 for count in counts.values()))
        for node_id, contract in LEGACY_CONTRACT.items():
            with self.subTest(node_id=node_id):
                node = self.dom.find(id=node_id)
                self.assertEqual(node.name, contract['tag'])
                expected_attrs = dict(contract['attrs'])
                # The moved controls keep their original form owner explicitly.
                if node_id in ('rio-dungeon', 'rio-level', 'rio-period', 'rio-min_level',
                               'rio-max_level', 'rio-week_start', 'rio-week_end', 'rio-apply-level-range'):
                    expected_attrs['form'] = 'rio-filters'
                self.assertEqual({k: v for k, v in node.attrs.items()
                                  if k not in ('id', 'class')}, expected_attrs)
                self.assertEqual([dict(o.attrs) for o in node.select('option')], contract['options'])
        self.assertIn('{% csrf_token %}', self.source)

    def test_module_tabs_are_descriptive_and_keep_dataset_navigation(self):
        for module in ('overview', 'runs', 'heatmaps', 'raids'):
            button = self.dom.find(id=f'rio-tab-{module}')
            self.assertEqual(button['data-tab'], module)
            self.assertTrue(button.select_one('.rio-tab-title'))
            self.assertTrue(button.select_one('.rio-tab-description'))
        badge = self.dom.find(id='rio-runs-availability')
        self.assertIsNotNone(badge)
        self.assertTrue(badge.has_attr('hidden'))
        self.assertIsNotNone(self.dom.find(id='rio-tab-runs').find(id=badge['id']))
        for node_id in ('rio-context', 'rio-module-title', 'rio-module-description'):
            self.assertIsNotNone(self.dom.find(id=node_id))

    def test_overview_metrics_precede_both_visible_charts(self):
        overview = self.dom.find(id='rio-overview')
        metrics = overview.select_one('.rio-metrics')
        self.assertIsNotNone(metrics)
        for node_id in ('rio-population', 'rio-score', 'rio-date', 'rio-change'):
            self.assertIsNotNone(metrics.find(id=node_id))
        self.assertEqual(len(metrics.select('.rio-metric')), 4)
        nodes = list(overview.descendants)
        self.assertLess(nodes.index(metrics), nodes.index(overview.find(id='rio-population-chart')))
        for name in ('population', 'score'):
            chart = overview.find(id=f'rio-{name}-chart')
            self.assertFalse(any(n.has_attr('hidden') for n in chart.parents if n.name))
            self.assertIsNotNone(overview.find(id=f'rio-{name}-range'))
        self.assertIn('非独立玩家人数', overview.get_text())

    def test_filters_have_native_mobile_disclosure_with_context_outside(self):
        disclosure = self.dom.find(id='rio-filter-disclosure')
        self.assertIsNotNone(disclosure)
        self.assertEqual(disclosure.name, 'details')
        self.assertTrue(disclosure.has_attr('open'), 'desktop/no-JS fallback keeps filters reachable')
        self.assertIsNotNone(disclosure.find('summary', recursive=False))
        self.assertIsNotNone(disclosure.find(id='rio-filters'))
        self.assertIsNone(self.dom.find(id='rio-context').find_parent(id='rio-filter-disclosure'))

    def test_dungeon_and_range_controls_are_visible_outside_general_disclosure(self):
        scope = self.dom.select_one('.rio-map-scope')
        self.assertIsNotNone(scope)
        self.assertIsNone(scope.find_parent('details'))
        self.assertIsNone(scope.find_parent(attrs={'role': 'tabpanel'}), 'selection remains reachable with an empty result')
        for node_id in ('rio-dungeon', 'rio-level', 'rio-period', 'rio-min_level', 'rio-max_level', 'rio-week_start', 'rio-week_end'):
            control = scope.find(id=node_id)
            self.assertIsNotNone(control)
            self.assertEqual(control.get('form'), 'rio-filters')
        self.assertIsNotNone(scope.find(id='rio-dungeon-filter'))
        self.assertIsNotNone(scope.find(id='rio-level-range'))
        self.assertIsNotNone(scope.find(id='rio-week-range'))

    def test_filter_groups_and_current_context_are_separate(self):
        filters = self.dom.find(id='rio-filters')
        self.assertTrue(filters.select_one('.rio-filter-primary'))
        share = self.dom.find(id='rio-share')
        self.assertEqual(share['type'], 'button')
        self.assertIsNotNone(share.find_parent(class_='rio-filter-actions'))
        self.assertIsNotNone(self.dom.find(id='rio-context').find_parent(class_='rio-context-bar'))

    def test_heatmap_controls_legend_and_sampling_context(self):
        heatmaps = self.dom.find(id='rio-heatmaps')
        for node_id in ('rio-heatmap-title', 'rio-heatmap-description'):
            self.assertIsNotNone(heatmaps.find(id=node_id))
        output = self.dom.find(id='rio-opacity-value')
        self.assertIsNotNone(output)
        self.assertEqual(output.name, 'output')
        self.assertEqual(output['for'], ['rio-opacity'])
        self.assertEqual(output.get_text(strip=True), '80%')
        self.assertEqual([(n['data-layer'], n['aria-pressed']) for n in heatmaps.select('[data-layer]')],
                         [('player_death', 'true'), ('player_spell', 'false'), ('npc_death', 'false')])
        self.assertTrue(heatmaps.select_one('.rio-map-legend'))
        self.assertTrue(heatmaps.find(id='rio-map-stats'))
        self.assertIn('拖动', heatmaps.get_text())
        self.assertIn('样本', heatmaps.get_text())
        self.assertFalse(self.dom.find(id='rio-runs-rows').get_text(strip=True))
        self.assertIn('不累加', self.dom.find(id='rio-raids').get_text())

    def test_responsive_theme_and_accessibility_rules_are_scoped(self):
        # Parse rule preludes, ignoring comments and enclosing media rules.
        clean = re.sub(r'/\*.*?\*/', '', self.css, flags=re.S)
        for prelude in re.findall(r'(?:^|[{}])([^{}]+)\{', clean):
            prelude = prelude.strip()
            if prelude.startswith('@'):
                continue
            for selector in prelude.split(','):
                self.assertIn('.rio-observer', selector, selector)
        self.assertIn('html.portal-theme-dark .rio-observer', self.css)
        self.assertRegex(self.css, r'@media\s*\(max-width:\s*640px\)')
        self.assertIn('prefers-reduced-motion', self.css)
        self.assertIn(':focus-visible', self.css)
        self.assertRegex(self.css, r'min-height:\s*40px')
        self.assertRegex(self.css, r'grid-template-columns:\s*repeat\(4,')
        self.assertRegex(self.css, r'grid-template-columns:\s*repeat\(2,')
        self.assertNotRegex(self.css, r'transform\s*:\s*scale')
        for direction in ('positive', 'negative', 'neutral', 'unknown'):
            self.assertIn(f'[data-direction="{direction}"]', self.css)

    def test_map_is_width_constrained_without_canvas_coordinate_scaling(self):
        self.assertRegex(self.css, r'\.rio-observer \.rio-map-frame\s*\{[^}]*max-width:\s*900px')
        self.assertRegex(self.css, r'\.rio-observer \.rio-map-frame canvas\s*\{[^}]*width:\s*100%')
        self.assertRegex(self.css, r'\.rio-observer \.rio-map-frame canvas\s*\{[^}]*height:\s*auto')
        self.assertRegex(self.css, r'\.rio-observer \.rio-map-frame\s*\{[^}]*aspect-ratio:\s*3\s*/\s*2')

    def test_sources_and_statistical_boundaries_remain_explicit(self):
        text = self.dom.find(id='rio-methodology').get_text()
        for boundary in ('不是实时全服统计', '不对现实玩家去重', '空缺的历史值保留为空',
                         '热度表示事件密度，不等于死亡概率', '不推算周增量'):
            self.assertIn(boundary, text)
        for href in ('https://raider.io', 'https://keystone.guru', 'https://raider.io/api'):
            self.assertIsNotNone(self.dom.find('a', href=href))
