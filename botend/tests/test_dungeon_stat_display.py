"""WCL rating-only observations retain the secondary-stat visual range."""
from bs4 import BeautifulSoup
from django.template.loader import render_to_string
from django.test import SimpleTestCase


class DungeonStatDisplayTests(SimpleTestCase):
    def test_rating_only_range_keeps_chart_without_inventing_percentages(self):
        for dungeon_id in ('all', 1):
            with self.subTest(dungeon_id=dungeon_id):
                html = render_to_string('portal/spec_detail/dungeon_stats.html', {
                    'class_name': 'Warrior', 'spec_name': 'Arms',
                    'selected_dungeon_id': dungeon_id,
                    'dungeon_detail': {
                        'dungeon_id': dungeon_id, 'dungeon_name': 'WCL',
                        'secondary_stats': [{
                            'key': 'crit', 'label': '暴击', 'sample_size': 3,
                            'pct': None,
                            'rating': {'p25': -114, 'p75': 1410,
                                       'median': 520, 'avg': 605},
                        }],
                    },
                })
                panel = BeautifulSoup(html, 'html.parser').select_one('.secondary-stats-section')
                row = panel.select_one('.secondary-stat-row')
                self.assertIsNotNone(row.select_one('.secondary-stat-bar'))
                self.assertIsNotNone(row.select_one('.secondary-stat-fill'))
                self.assertIsNotNone(row.select_one('.secondary-stat-marker'))
                bounds = [s.get_text(strip=True) for s in row.select('.secondary-stat-range > span')]
                self.assertEqual(bounds, ['-114', '1410'])
                self.assertNotIn('%', row.get_text())
                self.assertIn('中位 520', row.get_text())
                self.assertIn('平均 605', row.get_text())
                self.assertIn('P25–P75', panel.get_text())
