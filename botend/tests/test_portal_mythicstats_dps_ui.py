from pathlib import Path
from importlib import import_module
from unittest.mock import patch
from django.apps import apps
from django.test import SimpleTestCase, TestCase
from botend.models import PortalMythicstatsDpsRow, PortalNavigationGroup, PortalNavigationItem
from botend.portal.mythicstats import mythicstats_spec_identity


class PortalMythicstatsDpsUiContractsTests(SimpleTestCase):
    def test_home_no_longer_renders_or_fetches_mythicstats(self):
        root = Path(__file__).resolve().parents[2]
        source = (root / 'static/portal/js/main.js').read_text(encoding='utf-8')
        template = (root / 'templates/portal/index.html').read_text(encoding='utf-8')
        self.assertNotIn('id="section-mythicstats"', template)
        self.assertNotIn('/portal/api/mythicstats/dps/', source)
        self.assertNotIn('renderMythicstatsTable', source)
        self.assertIn('/portal/mplus/dps-rankings/?source=mythicstats', source)

    def test_new_page_uses_one_renderer_for_both_sources(self):
        root = Path(__file__).resolve().parents[2]
        source = (root / 'static/portal/js/mplus-dps-rankings.js').read_text(encoding='utf-8')
        template = (root / 'templates/portal/mplus_dps_rankings.html').read_text(encoding='utf-8')
        self.assertIn('本站统计数据', template)
        self.assertIn('Mythicstats统计数据', template)
        self.assertEqual(source.count('function renderRankings()'), 1)
        self.assertEqual(source.count('function renderTierBoard('), 1)
        self.assertIn('row.runs', source)
        self.assertIn('row.diff_raw', source)

    def test_mythicstats_identity_keeps_ambiguous_specs_distinct(self):
        mage = mythicstats_spec_identity('frost-mage')
        knight = mythicstats_spec_identity('frost-death-knight')
        self.assertEqual(mage['class_name'], 'Mage')
        self.assertEqual(knight['class_name'], 'DeathKnight')
        self.assertNotEqual(mage['icon_url'], knight['icon_url'])
        self.assertEqual(mythicstats_spec_identity('beast-mastery-hunter')['spec_name_cn'], '野兽控制')
        self.assertIn('/large/', mage['icon_url'])
        self.assertEqual(mythicstats_spec_identity('unknown'), {})

    def test_landing_page_cache_busts_portal_stylesheet(self):
        template = (Path(__file__).resolve().parents[2] / 'templates/portal/index.html').read_text(encoding='utf-8')

        self.assertRegex(template, r"portal/css/portal\.css' %}\?v=[^\"']+")


class PortalMythicstatsMigrationTests(TestCase):
    def test_api_preserves_source_fields_and_adds_shared_identity(self):
        for role, slug in [('damage', 'frost-mage'), ('tank', 'blood-death-knight'), ('healer', 'holy-priest')]:
            PortalMythicstatsDpsRow.objects.create(
                season='test-season', period_id=100, period_label='第 3 周', week=3,
                dungeon_id=0, role=role, spec_slug=slug, spec_name=slug,
                rank=1, tier='A', avg_value=287000, avg_text='287K', top_value=367000,
                top_text='367K', runs_text='16K', diff_raw='+2', diff_value=2,
                spec_url=f'/spec/{slug}',
            )
        with patch('botend.portal.api.fetch_mythicstats_dps') as fetch:
            response = self.client.get('/portal/api/mythicstats/dps/', {'season': 'test-season', 'period': 100})
        fetch.assert_not_called()
        self.assertEqual(response.status_code, 200)
        data = response.json()['data']
        self.assertEqual(set(data['roles']), {'damage', 'tank', 'healer'})
        for rows in data['roles'].values():
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual((row['avg_value'], row['top_value'], row['runs'], row['diff_raw']), (287000, 367000, '16K', '+2'))
            self.assertEqual(row['tier'], 'A')
            self.assertTrue(row['updated_at'])
            self.assertIn('/large/', row['icon_url'])
            self.assertTrue(row['spec_url'].startswith('https://mythicstats.com/spec/'))

    def test_navigation_migration_preserves_item_and_is_reversible(self):
        migration = import_module('botend.migrations.0204_move_mythicstats_navigation')
        group = PortalNavigationGroup.objects.create(key='migration-test', name='测试')
        item = PortalNavigationItem.objects.create(group=group, name='DPS 排行榜', url=migration.OLD_URL, is_active=False)
        migration.move_navigation(apps, None)
        migration.move_navigation(apps, None)
        item.refresh_from_db()
        self.assertEqual(item.url, migration.NEW_URL)
        self.assertFalse(item.is_active)
        migration.restore_navigation(apps, None)
        item.refresh_from_db()
        self.assertEqual(item.url, migration.OLD_URL)
