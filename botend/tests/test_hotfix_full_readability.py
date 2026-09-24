"""Full Hotfix reader must describe frozen new values, not imply field deltas."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.test import SimpleTestCase, override_settings

from botend.controller.plugins.wow.WagoSkillDiffMonitor import WagoSkillDiffMonitor


class FullHotfixNewValueReaderTests(SimpleTestCase):
    def test_later_named_itemsparse_upgrades_earlier_generic_item_group_title(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        sources = [
            {'push_id': 112236, 'table_name': table, 'record_id': 265790,
             'region_id': 3, 'locale': 'enUS', 'status': 1}
            for table in ('Item', 'ItemSparse')
        ]
        facts = [
            {'source': sources[0], 'after': {'ID': '265790', 'InventoryType': '0', 'IconFileDataID': '6011923'},
             'before': None, 'after_verified': True, 'before_verified': False, 'changes': []},
            {'source': sources[1], 'after': {'ID': '265790', 'Display_lang': 'Cache of Mistcrests',
                                          'Description_lang': 'A satchel containing 10 Mistcrests.'},
             'before': None, 'after_verified': True, 'before_verified': False, 'changes': []},
        ]
        graph_object = SimpleNamespace(
            kind='item', category='物品/装备', title='Item 265790', object_id=265790,
            tags=[], summary_fields=[], source_records=[
                SimpleNamespace(table='Item', record_id=265790),
                SimpleNamespace(table='ItemSparse', record_id=265790),
            ],
        )
        with TemporaryDirectory() as root, override_settings(BASE_DIR=root), \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WagoDB2GraphService') as service:
            service.return_value.resolve_hotfix_rows.return_value = SimpleNamespace(
                objects=[graph_object], unresolved_records=[],
            )
            path, _ = monitor._write_hotfix_full_html(
                branch='wow', locale='enUS', region_id=3, from_push=112235, to_push=112236,
                summary_title='物品分组测试', wago_url='https://wago.tools/hotfixes',
                build_num='70000', db2_build='12.1.0.70000',
                table_stats=[('Item', 1), ('ItemSparse', 1)],
                by_table={'Item': [sources[0]], 'ItemSparse': [sources[1]]},
                sample_per_table=1, enrich_max=0, facts=facts,
            )
            doc = BeautifulSoup(Path(path).read_text(encoding='utf-8'), 'html.parser')
        self.assertEqual(len(doc.select('.reader-card')), 1)
        self.assertEqual(doc.select_one('.reader-card h3').get_text(strip=True), 'Cache of Mistcrests')
        self.assertEqual(len(doc.select('.reader-evidence')), 2)
        summary = doc.select_one('.reader-readable .reader-card .reader-summary')
        self.assertIsNotNone(summary)
        self.assertIn('A satchel containing 10 Mistcrests.', summary.get_text(' ', strip=True))
        self.assertNotIn('Sound_override_subclassID', summary.get_text(' ', strip=True))
        self.assertIsNone(doc.select_one('.reader-card details.reader-sources').get('open'))

    def test_source_table_category_stays_filterable_when_graph_category_differs(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        source = {'push_id': 112236, 'table_name': 'TraitNodeEntry', 'record_id': 136970,
                  'region_id': 3, 'locale': 'enUS', 'status': 1}
        fact = {'source': source, 'after': {'ID': '136970', 'TraitDefinitionID': '4242'},
                'before': None, 'after_verified': True, 'before_verified': False, 'changes': []}
        graph_object = SimpleNamespace(
            kind='talent', category='天赋', title='天赋对象 136970', object_id=136970,
            tags=[], summary_fields=[],
            source_records=[SimpleNamespace(table='TraitNodeEntry', record_id=136970)],
        )
        graph = SimpleNamespace(objects=[graph_object], unresolved_records=[])
        with TemporaryDirectory() as root, override_settings(BASE_DIR=root), \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WagoDB2GraphService') as service:
            service.return_value.resolve_hotfix_rows.return_value = graph
            path, _ = monitor._write_hotfix_full_html(
                branch='wow', locale='enUS', region_id=3, from_push=112235, to_push=112236,
                summary_title='分类测试', wago_url='https://wago.tools/hotfixes',
                build_num='70000', db2_build='12.1.0.70000',
                table_stats=[('TraitNodeEntry', 1)], by_table={'TraitNodeEntry': [source]},
                sample_per_table=1, enrich_max=0, facts=[fact],
            )
            doc = BeautifulSoup(Path(path).read_text(encoding='utf-8'), 'html.parser')
        card = doc.select_one('.reader-card')
        self.assertEqual(card['data-category'], '天赋树')
        self.assertIn('天赋对象 136970', card.get_text(' ', strip=True))

    def test_full_reader_shows_object_payload_without_before_after_comparison(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        monitor.locale = 'enUS'
        sources = [
            {'push_id': 112236, 'table_name': table, 'record_id': rid,
             'region_id': 3, 'locale': 'enUS', 'status': status, 'data': None}
            for table, rid, status in (
                ('ItemSparse', 265790, 1), ('CriteriaTree', 234844, 1),
                ('QuestV2', 99114, 1), ('Mount', 3107, 4),
            )
        ]
        facts = [
            {'source': sources[0], 'after': {'ID': '265790', 'Display_lang': '勇士徽记包',
              'Description_lang': '内含十枚徽记；$@spelldesc1291728', 'ItemLevel': '681', 'Flags': '0'},
             'before': None, 'changes': [], 'after_verified': True, 'before_verified': False},
            {'source': sources[1], 'after': {'ID': '234844', 'Description_lang': '神秘收藏品',
              'CriteriaID': '118033', 'Parent': '234784', 'OrderIndex': '0'},
             'before': {'ID': '234844', 'Description_lang': '神秘收藏品',
               'CriteriaID': '118033', 'Parent': '234784', 'OrderIndex': '59'},
             'changes': [{'field': 'OrderIndex', 'before': '59', 'after': '0'}],
             'after_verified': True, 'before_verified': True},
            {'source': sources[2], 'after': {'ID': '99114', 'UniqueBitFlag': '72202'},
             'before': None, 'changes': [], 'after_verified': True, 'before_verified': False},
            {'source': sources[3], 'after': None, 'before': None, 'changes': [],
             'after_verified': False, 'before_verified': False},
        ]
        by_table = {source['table_name']: [source] for source in sources}
        monitor._fetch_db2_row_by_id = lambda *args: self.fail('frozen render must not fetch DB2 rows')
        with TemporaryDirectory() as root, override_settings(BASE_DIR=root):
            path, _ = monitor._write_hotfix_full_html(
                branch='wow', locale='enUS', to_push=112236,
                summary_title='样例正常 Hotfix 报告',
                wago_url='https://wago.tools/hotfixes', build_num='70000',
                from_push=112235, table_stats=[(t, 1) for t in by_table],
                by_table=by_table, facts=facts, sample_per_table=1,
                enrich_max=0, region_id=3, db2_build='12.1.0.70000',
            )
            doc = BeautifulSoup(Path(path).read_text(encoding='utf-8'), 'html.parser')
        reader = doc.select_one('.reader-digest')
        self.assertIsNotNone(reader)
        self.assertIn('本次热修涉及的对象与新值', reader.get_text(' ', strip=True))
        self.assertIn('仅有来源或状态', reader.get_text(' ', strip=True))
        self.assertEqual(len(reader.select('.reader-evidence')), 3)
        self.assertIn('1 个内容摘要', doc.select_one('.quick-facts').get_text(' ', strip=True))
        self.assertIn('2 个仅底层对象', doc.select_one('.quick-facts').get_text(' ', strip=True))
        self.assertIn('勇士徽记包', [heading.get_text(strip=True) for heading in reader.select('.reader-card h3')])
        primary = reader.select_one('.reader-readable')
        self.assertIsNotNone(primary)
        self.assertIn('勇士徽记包', primary.get_text(' ', strip=True))
        self.assertIn('内含十枚徽记', primary.select_one('.reader-summary').get_text(' ', strip=True))
        self.assertIn('物品等级 681', primary.select_one('.reader-summary').get_text(' ', strip=True))
        self.assertNotIn('UniqueBitFlag', primary.get_text(' ', strip=True))
        raw_only = reader.select_one('details.reader-raw-only')
        self.assertIsNotNone(raw_only)
        self.assertIsNone(raw_only.get('open'))
        self.assertIn('Quest 99114', raw_only.get_text(' ', strip=True))
        self.assertIn('无法判断具体游戏表现', raw_only.select_one('summary').get_text(' ', strip=True))
        self.assertIn('显示名 / Display_lang：勇士徽记包', reader.get_text(' ', strip=True))
        self.assertIn('描述 / Description_lang：内含十枚徽记', reader.get_text(' ', strip=True))
        self.assertNotIn('$@spelldesc1291728', reader.get_text(' ', strip=True))
        self.assertIn('物品等级 / ItemLevel：681', reader.get_text(' ', strip=True))
        self.assertIn('排序 / OrderIndex：0', reader.get_text(' ', strip=True))
        self.assertIn('UniqueBitFlag', reader.get_text(' ', strip=True))
        self.assertNotIn('59 → 0', reader.get_text(' ', strip=True))
        self.assertNotIn('旧值未知', reader.get_text(' ', strip=True))
        self.assertNotIn('坐骑 #3107', reader.get_text(' ', strip=True))
        self.assertIsNotNone(reader.select_one('#hotfixFilter'))
        self.assertIsNotNone(reader.select_one('details.reader-filter-drawer'))
        self.assertIsNone(reader.select_one('details.reader-filter-drawer').get('open'))
        self.assertEqual(len(doc.select('.technical-report article.record')), 4)
        self.assertIn('3107', doc.select_one('.technical-report').get_text(' ', strip=True))

    def test_legacy_full_report_remains_readable_without_frozen_payload(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        monitor.locale = 'enUS'
        monitor._fetch_db2_row_by_id = lambda table, build, rid: {
            'ID': rid, 'Display_lang': '旧版物品对象', 'VerifiedBuild': build,
        }
        with TemporaryDirectory() as root, override_settings(BASE_DIR=root):
            path, _ = monitor._write_hotfix_full_html(
                branch='wow', locale='enUS', to_push=109505,
                summary_title='旧版报告', wago_url='https://wago.tools/hotfixes',
                build_num='68367', from_push=109504, table_stats=[('ItemSparse', 1)],
                by_table={'ItemSparse': [{'push_id': 109505, 'table_name': 'ItemSparse',
                                          'record_id': 19019}]},
                sample_per_table=1, enrich_max=3,
            )
            doc = BeautifulSoup(Path(path).read_text(encoding='utf-8'), 'html.parser')
        self.assertIn('旧版物品对象', doc.select_one('.reader-digest').get_text(' ', strip=True))
        self.assertEqual(len(doc.select('.technical-report article.record')), 1)
