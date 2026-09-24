"""Full Hotfix reader must describe frozen new values, not imply field deltas."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.test import SimpleTestCase, override_settings

from botend.controller.plugins.wow.WagoSkillDiffMonitor import WagoSkillDiffMonitor


class FullHotfixNewValueReaderTests(SimpleTestCase):
    def test_same_spell_effect_change_groups_effect_positions_without_losing_sources(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        sources = [{'id': rid, 'push_id': 112233, 'table_name': 'SpellEffect',
                    'record_id': rid, 'region_id': 3, 'locale': 'enUS', 'status': 1}
                   for rid in (1357276, 1357280, 1357281)]
        facts = [
            {'source': source,
             'after': {'ID': str(source['record_id']), 'SpellID': '1222923',
                       'EffectIndex': str(index), 'EffectAura': '649'},
             'before': {'ID': str(source['record_id']), 'SpellID': '1222923',
                        'EffectIndex': str(index), 'EffectAura': '218'},
             'after_verified': True, 'before_verified': True,
             'changes': [{'field': 'EffectAura', 'before': '218', 'after': '649'}]}
            for source, index in zip(sources, (7, 8, 9))
        ]
        with TemporaryDirectory() as root, override_settings(BASE_DIR=root):
            path, _ = monitor._write_hotfix_full_html(
                branch='wow', locale='enUS', region_id=3,
                from_push=112232, to_push=112233,
                summary_title='效果合并测试', wago_url='https://wago.tools/hotfixes',
                build_num='69933', db2_build='12.1.0.69933',
                table_stats=[('SpellEffect', 3)], by_table={'SpellEffect': sources},
                sample_per_table=3, enrich_max=0, facts=facts,
            )
            doc = BeautifulSoup(Path(path).read_text(encoding='utf-8'), 'html.parser')
        confirmed = doc.select_one('.reader-confirmed')
        self.assertEqual(len(confirmed.select('.reader-confirmed-card')), 1)
        self.assertIn('3 条来源', confirmed.get_text(' ', strip=True))
        card = confirmed.select_one('.reader-confirmed-card')
        self.assertEqual(card.get_text(' ', strip=True).count('218 → 649'), 1)
        for effect in ('#8', '#9', '#10'):
            self.assertIn(effect, card.get_text(' ', strip=True))
        for source in sources:
            self.assertIn(str(source['record_id']), card['data-search'])
        self.assertEqual(len(doc.select('.technical-report article.record')), 3)

    def test_verified_field_change_is_a_primary_compact_fact(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        source = {'id': 1, 'push_id': 112055, 'table_name': 'SpellEffect',
                  'record_id': 1354561, 'region_id': 3, 'locale': 'enUS', 'status': 1}
        fact = {'source': source,
                'after': {'ID': '1354561', 'SpellID': '880001', 'EffectIndex': '0',
                          'EffectBasePointsF': '10'},
                'before': {'ID': '1354561', 'SpellID': '880001', 'EffectIndex': '0',
                           'EffectBasePointsF': '30'},
                'after_verified': True, 'before_verified': True,
                'changes': [{'field': 'EffectBasePointsF', 'before': '30', 'after': '10'}]}
        with TemporaryDirectory() as root, override_settings(BASE_DIR=root):
            path, _ = monitor._write_hotfix_full_html(
                branch='wow', locale='enUS', region_id=3, from_push=112054,
                to_push=112055, summary_title='改动直读测试',
                wago_url='https://wago.tools/hotfixes', build_num='69933',
                db2_build='12.1.0.69933', table_stats=[('SpellEffect', 1)],
                by_table={'SpellEffect': [source]}, sample_per_table=1,
                enrich_max=0, facts=[fact],
            )
            doc = BeautifulSoup(Path(path).read_text(encoding='utf-8'), 'html.parser')
        reader = doc.select_one('.reader-digest')
        confirmed = reader.select_one('.reader-confirmed')
        self.assertIsNotNone(confirmed)
        self.assertEqual(len(confirmed.select('.reader-confirmed-card')), 1)
        self.assertIn('EffectBasePointsF', confirmed.get_text(' ', strip=True))
        self.assertIn('30 → 10', confirmed.get_text(' ', strip=True))
        self.assertLess(str(doc).index('class="reader-confirmed"'), str(doc).index('id="hotfixFilter"'))
        self.assertNotIn('这次究竟知道什么', reader.get_text(' ', strip=True))

    def test_internal_spell_and_world_object_status_are_not_equipment_change_claims(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        sources = [
            {'id': 1, 'push_id': 112236, 'table_name': table, 'record_id': rid,
             'region_id': 3, 'locale': 'enUS', 'build': 69933, 'status': status, 'data': None}
            for table, rid, status in (
                ('GameObjects', 653515, 3), ('SpellName', 1322323, 1),
                ('SpellCooldowns', 101894, 1),
            )
        ]
        facts = [
            {'source': sources[0], 'after': None, 'before': None,
             'after_verified': False, 'before_verified': False, 'changes': []},
            {'source': sources[1], 'after': {'ID': '1322323', 'Name_lang': "[DNT] Head Mason's Tablet"},
             'before': None, 'after_verified': True, 'before_verified': False, 'changes': []},
            {'source': sources[2], 'after': {'ID': '101894', 'SpellID': '1322323',
                                            'RecoveryTime': '20000'},
             'before': None, 'after_verified': True, 'before_verified': False, 'changes': []},
        ]
        with TemporaryDirectory() as root, override_settings(BASE_DIR=root):
            path, _ = monitor._write_hotfix_full_html(
                branch='wow', locale='enUS', region_id=3,
                from_push=112235, to_push=112236,
                summary_title='单次热修阅读检验', wago_url='https://wago.tools/hotfixes',
                build_num='69933', db2_build='12.1.0.69933',
                table_stats=[(s['table_name'], 1) for s in sources],
                by_table={s['table_name']: [s] for s in sources},
                sample_per_table=1, enrich_max=0, facts=facts,
            )
            doc = BeautifulSoup(Path(path).read_text(encoding='utf-8'), 'html.parser')
        reader = doc.select_one('.reader-digest')
        self.assertIn('已核实改动：无', reader.get_text(' ', strip=True))
        verdict = reader.select_one('.reader-verdict')
        self.assertIsNotNone(verdict)
        self.assertIn('世界交互物', verdict.get_text(' ', strip=True))
        self.assertIn('20 秒', verdict.get_text(' ', strip=True))
        self.assertIn('关联、旧值未核实', verdict.get_text(' ', strip=True))
        self.assertIn('具体改动未知', verdict.get_text(' ', strip=True))
        self.assertNotIn('这次究竟知道什么', reader.get_text(' ', strip=True))
        self.assertIn('状态：失效', reader.select_one('.reader-world-status').get_text(' ', strip=True))
        self.assertEqual(len(reader.select('.reader-readable .reader-card')), 0)
        status = reader.select_one('.reader-world-status')
        self.assertIsNotNone(status)
        self.assertIn('世界交互物 #653515', status.get_text(' ', strip=True))
        self.assertIn('来源状态：失效', status.get_text(' ', strip=True))
        self.assertIn('具体改动未知', status.get_text(' ', strip=True))
        self.assertIn('https://www.wowhead.com/object=653515', status.select_one('a')['href'])
        raw = reader.select_one('.reader-raw-only')
        self.assertIn('内部技能 #1322323', raw.get_text(' ', strip=True))
        self.assertIn('冷却记录 20 秒', raw.get_text(' ', strip=True))
        self.assertNotIn('旧值未核实', raw.select_one('.reader-summary').get_text(' ', strip=True))
        self.assertNotIn('装备', status.get_text(' ', strip=True))
        self.assertEqual(len(reader.select('.reader-evidence')), 2)
        self.assertEqual(len(doc.select('.technical-report article.record')), 3)

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
        self.assertIn('Hotfix 改动', reader.get_text(' ', strip=True))
        self.assertIn('已核实改动 1', reader.get_text(' ', strip=True))
        self.assertEqual(len(reader.select('.reader-evidence')), 3)
        self.assertIn('1 项已核实改动', doc.select_one('.quick-facts').get_text(' ', strip=True))
        self.assertIn('1 条仅状态/来源', doc.select_one('.quick-facts').get_text(' ', strip=True))
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
        self.assertIn('无法辨认具体改动', raw_only.select_one('summary').get_text(' ', strip=True))
        self.assertIn('显示名 / Display_lang：勇士徽记包', reader.get_text(' ', strip=True))
        self.assertIn('描述 / Description_lang：内含十枚徽记', reader.get_text(' ', strip=True))
        self.assertNotIn('$@spelldesc1291728', reader.get_text(' ', strip=True))
        self.assertIn('物品等级 / ItemLevel：681', reader.get_text(' ', strip=True))
        self.assertIn('排序 / OrderIndex：0', reader.get_text(' ', strip=True))
        self.assertIn('UniqueBitFlag', reader.get_text(' ', strip=True))
        self.assertIn('59 → 0', reader.select_one('.reader-confirmed').get_text(' ', strip=True))
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
