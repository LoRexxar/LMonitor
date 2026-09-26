"""Full Hotfix reader must describe frozen new values, not imply field deltas."""
import html
import json
import requests
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.test import SimpleTestCase, override_settings

from botend.controller.plugins.wow.WagoSkillDiffMonitor import WagoSkillDiffMonitor
from botend.services.wago_hotfix_reader_fields import display_hotfix_value, project_hotfix_columns


class FullHotfixNewValueReaderTests(SimpleTestCase):
    def test_item_price_changes_show_currency_without_discarding_copper_facts(self):
        after = {'ID': '171692', 'SellPrice': '125023', 'BuyPrice': '625117'}
        base = {'ID': '171692', 'SellPrice': '562279', 'BuyPrice': '2811399'}
        fields = {field['field']: field for field in
                  project_hotfix_columns('ItemSparse', after, base)}
        self.assertEqual(fields['SellPrice']['text'], '56金22银79铜 → 12金50银23铜')
        self.assertEqual(fields['BuyPrice']['text'], '281金13银99铜 → 62金51银17铜')
        self.assertTrue(fields['SellPrice']['base_changed'])
        self.assertEqual(after['SellPrice'], '125023')
        self.assertEqual(display_hotfix_value('SellPrice', '10000'), '1金')
        self.assertEqual(display_hotfix_value('SellPrice', '0'), '0铜')
        self.assertEqual(display_hotfix_value('SellPrice', 'invalid'), 'invalid')

    def test_spell_misc_removed_cast_permissions_need_verified_same_build_base(self):
        after = {'ID': '842998', 'SpellID': '1295610',
                 'Attributes_0': '539230208', 'Attributes_1': '1160',
                 'Attributes_2': '272629764', 'Attributes_4': '8388608',
                 'Attributes_5': '540928', 'Attributes_6': '0', 'Attributes_9': '0'}
        base = {'ID': '842998', 'SpellID': '1295610',
                'Attributes_0': '690225152', 'Attributes_1': '1192',
                'Attributes_2': '273170436', 'Attributes_4': '8388736',
                'Attributes_5': '934152', 'Attributes_6': '4096',
                'Attributes_9': '1048576'}
        fields = project_hotfix_columns('SpellMisc', after, base)
        text = ' '.join(field['text'] for field in fields)
        self.assertIn('移除', text)
        self.assertIn('引导中施放', text)
        self.assertIn('骑乘时施放', text)
        self.assertIn('乘坐载具时施放', text)
        self.assertIn('昏迷时施放', text)
        self.assertTrue(any(field['base_changed'] for field in fields))
        self.assertFalse(any(field['field'] == 'Attributes' for field in
                             project_hotfix_columns('SpellMisc', after, None)))
        self.assertEqual(after['Attributes_9'], '0')

    def test_spell_misc_cast_permission_changes_explain_the_named_spell(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        misc = {'id': 1, 'push_id': 112218, 'table_name': 'SpellMisc',
                'record_id': 842998, 'region_id': 3, 'locale': 'enUS',
                'build': 69875, 'status': 1}
        name = {'id': 2, 'push_id': 112218, 'table_name': 'SpellName',
                'record_id': 1295610, 'region_id': 3, 'locale': 'enUS',
                'build': 69875, 'status': 1}
        after = {'ID': '842998', 'SpellID': '1295610',
                 'Attributes_0': '539230208', 'Attributes_1': '1160',
                 'Attributes_2': '272629764', 'Attributes_4': '8388608',
                 'Attributes_5': '540928', 'Attributes_6': '0', 'Attributes_9': '0'}
        base = {'ID': '842998', 'SpellID': '1295610',
                'Attributes_0': '690225152', 'Attributes_1': '1192',
                'Attributes_2': '273170436', 'Attributes_4': '8388736',
                'Attributes_5': '934152', 'Attributes_6': '4096',
                'Attributes_9': '1048576'}
        facts = [
            {'source': misc, 'source_build': '12.1.0.69875', 'after': after,
             'before': None, 'after_verified': True, 'before_verified': False, 'changes': []},
            {'source': name, 'source_build': '12.1.0.69875',
             'after': {'ID': '1295610', 'Name_lang': '蜿蜒打击'},
             'before': None, 'after_verified': True, 'before_verified': False, 'changes': []},
        ]
        monitor._fetch_hotfix_db2_baseline_row = (
            lambda table, build, rid, locale: base if table == 'SpellMisc' else {})
        with TemporaryDirectory() as root, override_settings(BASE_DIR=root,
                     WAGO_HOTFIX_FIELD_BASELINE_LOOKUPS=2):
            path, _ = monitor._write_hotfix_full_html(
                branch='wow', locale='enUS', region_id=3,
                from_push=112217, to_push=112218, summary_title='施放条件变化',
                wago_url='https://wago.tools/hotfixes', build_num='69875',
                db2_build='12.1.0.69875', table_stats=[('SpellMisc', 1), ('SpellName', 1)],
                by_table={'SpellMisc': [misc], 'SpellName': [name]},
                sample_per_table=1, enrich_max=0, facts=facts,
            )
            doc = BeautifulSoup(Path(path).read_text(encoding='utf-8'), 'html.parser')
        story = doc.select_one('.reader-impact-summary')
        self.assertIsNotNone(story)
        self.assertIn('蜿蜒打击', story.get_text(' ', strip=True))
        self.assertIn('移除', story.get_text(' ', strip=True))
        self.assertIn('施法/引导中', story.get_text(' ', strip=True))
        self.assertIn('逐位见下方', story.get_text(' ', strip=True))
        self.assertNotIn('不能施放', story.get_text(' ', strip=True))
        card = doc.select_one('.reader-impact-card[data-baseline-key*="spellmisc"]')
        self.assertIsNotNone(card)
        self.assertIn('Attributes', card.get_text(' ', strip=True))
        self.assertIn('引导中施放', card.get_text(' ', strip=True))
        self.assertEqual(len(doc.select('.technical-report article.record')), 2)

    def test_display_rounds_float_noise_without_changing_comparison_or_raw_value(self):
        after = {'EffectBasePointsF': '2.549999952316284',
                 'PvpMultiplier': '0.69999998807907',
                 'BonusCoefficientFromAP': '0.52135998010635'}
        baseline = {'EffectBasePointsF': '4.25'}
        fields = project_hotfix_columns('SpellEffect', after, baseline)
        text = ' '.join(field['text'] for field in fields)
        self.assertIn('4.25 → 2.55', text)
        self.assertIn('70%', text)
        self.assertIn('52.14%', text)
        self.assertEqual(display_hotfix_value('EffectBasePointsF', '2.549999952316284'), '2.55')
        self.assertEqual(after['EffectBasePointsF'], '2.549999952316284')

    def test_identical_client_row_is_not_a_change_and_request_failure_is_not_new(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        source = {'id': 99, 'push_id': 112236, 'table_name': 'SpellEffect',
                  'record_id': 1358044, 'region_id': 3, 'locale': 'enUS',
                  'build': 69933, 'status': 1}
        row = {'ID': '1358044', 'SpellID': '1322323', 'Effect': '189',
               'ImplicitTarget_0': '1', 'EffectMiscValue_0': '142879'}
        fact = {'source': source, 'source_build': '12.1.0.69933',
                'after': row, 'before': None, 'after_verified': True,
                'before_verified': False, 'changes': []}
        with TemporaryDirectory() as root, override_settings(BASE_DIR=root,
                     WAGO_HOTFIX_FIELD_BASELINE_LOOKUPS=1):
            monitor._fetch_hotfix_db2_baseline_row = lambda *args: row
            path, _ = monitor._write_hotfix_full_html(
                branch='wow', locale='enUS', region_id=3,
                from_push=112235, to_push=112236, summary_title='基表同值',
                wago_url='https://wago.tools/hotfixes', build_num='69933',
                db2_build='12.1.0.69933', table_stats=[('SpellEffect', 1)],
                by_table={'SpellEffect': [source]}, sample_per_table=1,
                enrich_max=0, facts=[fact],
            )
            unchanged = BeautifulSoup(Path(path).read_text(encoding='utf-8'), 'html.parser')
            monitor._fetch_hotfix_db2_baseline_row = lambda *args: None
            path, _ = monitor._write_hotfix_full_html(
                branch='wow', locale='enUS', region_id=3,
                from_push=112235, to_push=112236, summary_title='基表不可用',
                wago_url='https://wago.tools/hotfixes', build_num='69933',
                db2_build='12.1.0.69933', table_stats=[('SpellEffect', 1)],
                by_table={'SpellEffect': [source]}, sample_per_table=1,
                enrich_max=0, facts=[fact],
            )
            unavailable = BeautifulSoup(Path(path).read_text(encoding='utf-8'), 'html.parser')
        self.assertEqual(len(unchanged.select('.reader-impact-card')), 0)
        self.assertEqual(len(unchanged.select('.technical-report article.record')), 1)
        self.assertEqual(len(unavailable.select('.reader-impact-card')), 1)
        self.assertIn('本次配置', unavailable.select_one('.reader-impact-card header small').get_text(' ', strip=True))
        self.assertNotIn('新配置', unavailable.select_one('.reader-impact-card header small').get_text(' ', strip=True))

    def test_common_spell_enums_are_readable_without_inventing_other_values(self):
        fields = project_hotfix_columns('SpellEffect', {
            'Effect': '6', 'ImplicitTarget_0': '25', 'EffectAura': '430',
            'EffectMiscValue_0': '3081',
        })
        text = ' '.join(item['text'] for item in fields)
        self.assertIn('施加光环', text)
        self.assertIn('任意目标', text)
        self.assertIn('播放场景', text)
        self.assertIn('3081', text)

    def test_single_push_lists_meaningful_fields_and_client_base_differences(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        rows = [
            ('SpellName', 1271622, {'ID': '1271622', 'Name_lang': '[DNT] Tainted Page'}),
            ('SpellEffect', 1284426, {'ID': '1284426', 'SpellID': '1271622',
                                       'EffectIndex': '0', 'Effect': '189',
                                       'ImplicitTarget_0': '1', 'EffectMiscValue_0': '139621'}),
            ('SpellName', 1322323, {'ID': '1322323', 'Name_lang': "[DNT] Head Mason's Tablet"}),
            ('SpellEffect', 1358044, {'ID': '1358044', 'SpellID': '1322323',
                                       'EffectIndex': '0', 'Effect': '189',
                                       'ImplicitTarget_0': '1', 'EffectMiscValue_0': '142879'}),
            ('SpellCooldowns', 101894, {'ID': '101894', 'SpellID': '1322323',
                                         'RecoveryTime': '20000'}),
            ('SpellMisc', 867977, {'ID': '867977', 'SpellID': '1322323',
                                   'RangeIndex': '1', 'CastingTimeIndex': '1'}),
        ]
        sources = [{'id': i, 'push_id': 112236, 'table_name': table,
                    'record_id': rid, 'build': 69933, 'region_id': 3,
                    'locale': 'enUS', 'status': 1}
                   for i, (table, rid, _row) in enumerate(rows, 1)]
        facts = [{'source': source, 'source_build': '12.1.0.69933',
                  'after': row, 'before': None, 'after_verified': True,
                  'before_verified': False, 'changes': []}
                 for source, (_table, _rid, row) in zip(sources, rows)]
        base = {('SpellName', 1271622): {'ID': '1271622',
                                         'Name_lang': "[DNT] Head Mason's Tablet"},
                ('SpellEffect', 1284426): {'ID': '1284426', 'SpellID': '1271622',
                                            'EffectIndex': '0', 'Effect': '189',
                                            'ImplicitTarget_0': '1', 'EffectMiscValue_0': '142879'},
                ('SpellRange', 1): {'ID': '1', 'RangeMin_0': '0', 'RangeMax_0': '0',
                                    'RangeMin_1': '0', 'RangeMax_1': '0'},
                ('SpellCastTimes', 1): {'ID': '1', 'Base': '0', 'Minimum': '0'}}
        monitor._fetch_hotfix_db2_baseline_row = lambda table, build, rid, locale: base.get((table, rid), {})
        with TemporaryDirectory() as root, override_settings(BASE_DIR=root,
                     WAGO_HOTFIX_FIELD_BASELINE_LOOKUPS=8,
                     WAGO_HOTFIX_READER_CONTEXT_LOOKUPS=4,
                     WAGO_HOTFIX_READER_IMPACT_MAX=2):
            path, _ = monitor._write_hotfix_full_html(
                branch='wow', locale='enUS', region_id=3, from_push=112235,
                to_push=112236, summary_title='单条字段说明测试',
                wago_url='https://wago.tools/hotfixes', build_num='69933',
                db2_build='12.1.0.69933',
                table_stats=[(table, 1 if table not in ('SpellName', 'SpellEffect') else 2)
                             for table in ('SpellName', 'SpellEffect', 'SpellCooldowns', 'SpellMisc')],
                by_table={table: [s for s in sources if s['table_name'] == table]
                          for table in ('SpellName', 'SpellEffect', 'SpellCooldowns', 'SpellMisc')},
                sample_per_table=2, enrich_max=0, facts=facts,
            )
            doc = BeautifulSoup(Path(path).read_text(encoding='utf-8'), 'html.parser')
        cards = doc.select('.reader-impact-card')
        self.assertEqual(len(cards), 6)
        self.assertIn('spelleffect:12.1.0.69933:enUS:1284426:112236',
                      {card.get('data-baseline-key') for card in cards})
        self.assertIn('spellname:12.1.0.69933:enUS:1271622:112236',
                      {card.get('data-baseline-key') for card in cards})
        self.assertEqual(len(doc.select('.reader-impact-more .reader-impact-card')), 4)
        self.assertEqual(len(doc.select('.reader-impacts > .reader-impact-card')), 2)
        self.assertEqual(len(doc.select('.reader-configs .reader-impact-more .reader-impact-card')), 4)
        self.assertIn('非改动数', doc.select_one('.reader-configs summary').get_text(' ', strip=True))
        text = ' '.join(card.get_text(' ', strip=True) for card in cards)
        self.assertIn('拾取', text)
        self.assertIn('施法者', text)
        self.assertIn('20 秒', text)
        self.assertIn('0 码', text)
        self.assertIn('瞬发', text)
        self.assertIn('142879 → 139621', text)
        self.assertIn("[DNT] Head Mason's Tablet → [DNT] Tainted Page", text)
        self.assertIn('基表', text)
        self.assertIn('新配置', text)
        self.assertNotIn('装备冷却', text)
        stories = doc.select('.reader-impact-summary')
        self.assertEqual(len(stories), 2)
        renamed = next(card for card in stories if '1271622' in card['data-search'])
        configured = next(card for card in stories if '1322323' in card['data-search'])
        self.assertIn('拾取', renamed.get_text(' ', strip=True))
        self.assertIn('作用：拾取', renamed.get_text(' ', strip=True))
        self.assertIn('相对同 build 客户端基表', renamed.get_text(' ', strip=True))
        self.assertIn("[DNT] Head Mason's Tablet → [DNT] Tainted Page",
                      renamed.get_text(' ', strip=True))
        self.assertIn('142879 → 139621', renamed.get_text(' ', strip=True))
        self.assertIn('具体指向未核实', renamed.get_text(' ', strip=True))
        self.assertLess(renamed.get_text(' ', strip=True).index('名称：'),
                        renamed.get_text(' ', strip=True).index('拾取效果附加参数'))
        self.assertNotIn('Name_lang', renamed.get_text(' ', strip=True))
        self.assertIn('拾取', configured.get_text(' ', strip=True))
        self.assertIn('20 秒', configured.get_text(' ', strip=True))
        self.assertIn('0 码', configured.get_text(' ', strip=True))
        self.assertIn('瞬发', configured.get_text(' ', strip=True))
        self.assertIn('作用：拾取', configured.get_text(' ', strip=True))
        self.assertNotIn('拾取类施法效果', configured.get_text(' ', strip=True))
        self.assertIsNotNone(renamed.find_parent(class_='reader-impacts'))
        self.assertIsNotNone(configured.find_parent('details', id='readerImpactMore'))
        self.assertIsNone(doc.select_one('#readerImpactMore').get('open'))
        self.assertNotIn('装备', ' '.join(card.get_text(' ', strip=True) for card in stories))
        self.assertEqual(len(doc.select('.technical-report article.record')), 6)

    def test_verified_history_precedes_later_client_baseline(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        changed = {'id': 1, 'push_id': 112200, 'table_name': 'SpellEffect',
                   'record_id': 70, 'region_id': 3, 'locale': 'enUS',
                   'build': 69875, 'status': 1}
        baseline = {'id': 2, 'push_id': 112236, 'table_name': 'SpellName',
                    'record_id': 99, 'region_id': 3, 'locale': 'enUS',
                    'build': 69933, 'status': 1}
        facts = [
            {'source': changed, 'source_build': '12.1.0.69875',
             'after': {'ID': '70', 'SpellID': '700', 'Effect': '2',
                       'EffectBasePointsF': '10'},
             'before': {'ID': '70', 'SpellID': '700', 'Effect': '2',
                        'EffectBasePointsF': '30'},
             'after_verified': True, 'before_verified': True,
             'changes': [{'field': 'EffectBasePointsF', 'before': '30', 'after': '10'}]},
            {'source': baseline, 'source_build': '12.1.0.69933',
             'after': {'ID': '99', 'Name_lang': '新名字'},
             'before': None, 'after_verified': True,
             'before_verified': False, 'changes': []},
        ]
        monitor._fetch_hotfix_db2_baseline_row = (
            lambda table, build, rid, locale:
            {'ID': '99', 'Name_lang': '旧名字'} if table == 'SpellName' else None)
        with TemporaryDirectory() as root, override_settings(BASE_DIR=root,
                     WAGO_HOTFIX_FIELD_BASELINE_LOOKUPS=2):
            path, _ = monitor._write_hotfix_full_html(
                branch='wow', locale='enUS', region_id=3,
                from_push=112199, to_push=112236, summary_title='历史优先',
                wago_url='https://wago.tools/hotfixes', build_num='69933',
                db2_build='12.1.0.69933',
                table_stats=[('SpellEffect', 1), ('SpellName', 1)],
                by_table={'SpellEffect': [changed], 'SpellName': [baseline]},
                sample_per_table=1, enrich_max=0, facts=facts,
            )
            doc = BeautifulSoup(Path(path).read_text(encoding='utf-8'), 'html.parser')
        reader = doc.select_one('.reader-digest')
        self.assertLess(str(reader).index('id="readerConfirmed"'),
                        str(reader).index('id="readerImpacts"'))
        self.assertIn('30 → 10', reader.select_one('.reader-confirmed').get_text(' ', strip=True))
        self.assertIn('旧名字 → 新名字', reader.select_one('.reader-impacts').get_text(' ', strip=True))
        self.assertEqual(len(doc.select('.technical-report article.record')), 2)

    def test_verified_range_index_uses_only_its_source_build_reference_pair(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        source = {'id': 1, 'push_id': 112186, 'table_name': 'SpellMisc',
                  'record_id': 865878, 'region_id': 3, 'locale': 'enUS',
                  'build': 69814, 'status': 1}
        fact = {'source': source, 'source_build': '12.1.0.69814',
                'after': {'ID': '865878', 'SpellID': '1298417', 'RangeIndex': '13'},
                'before': {'ID': '865878', 'SpellID': '1298417', 'RangeIndex': '6'},
                'after_verified': True, 'before_verified': True,
                'changes': [{'field': 'RangeIndex', 'before': '6', 'after': '13'}]}
        rows = {6: {'ID': 6, 'DisplayNameShort_lang': 'Vision', 'RangeMax_0': 100,
                    'RangeMax_1': 100},
                13: {'ID': 13, 'DisplayNameShort_lang': 'Anywhere - Unlimited',
                     'RangeMax_0': 50000, 'RangeMax_1': 50000}}
        lookup = lambda table, build, rid, locale: rows.get(rid) if (
            table == 'SpellRange' and build == '12.1.0.69814' and locale == 'enUS') else None
        with TemporaryDirectory() as root, override_settings(BASE_DIR=root,
                     WAGO_HOTFIX_READER_CONTEXT_LOOKUPS=2), \
             patch.object(monitor, '_fetch_hotfix_db2_baseline_row', side_effect=lookup) as fetch:
            path, _ = monitor._write_hotfix_full_html(
                branch='wow', locale='enUS', region_id=3,
                from_push=112185, to_push=112186, summary_title='范围引用',
                wago_url='https://wago.tools/hotfixes', build_num='69933',
                db2_build='12.1.0.69933', table_stats=[('SpellMisc', 1)],
                by_table={'SpellMisc': [source]}, sample_per_table=1,
                enrich_max=0, facts=[fact],
            )
            doc = BeautifulSoup(Path(path).read_text(encoding='utf-8'), 'html.parser')
        card = doc.select_one('.reader-confirmed-card')
        text = card.get_text(' ', strip=True)
        self.assertIn('6（Vision；记录上限 100 码）', text)
        self.assertIn('13（Anywhere - Unlimited；记录上限 50000 码）', text)
        self.assertNotIn('实际射程', text)
        self.assertIn('spellmisc:12.1.0.69814:enUS:865878:112186:6:13',
                      card['data-range-context'])
        self.assertEqual([(c.args[0], c.args[1], c.args[2]) for c in fetch.call_args_list],
                         [('SpellRange', '12.1.0.69814', 6),
                          ('SpellRange', '12.1.0.69814', 13)])
        monitor._fetch_hotfix_db2_baseline_row = lambda table, build, rid, locale: rows.get(6) if rid == 6 else None
        with TemporaryDirectory() as root, override_settings(BASE_DIR=root,
                     WAGO_HOTFIX_READER_CONTEXT_LOOKUPS=2):
            path, _ = monitor._write_hotfix_full_html(
                branch='wow', locale='enUS', region_id=3,
                from_push=112185, to_push=112186, summary_title='范围引用降级',
                wago_url='https://wago.tools/hotfixes', build_num='69933',
                db2_build='12.1.0.69933', table_stats=[('SpellMisc', 1)],
                by_table={'SpellMisc': [source]}, sample_per_table=1,
                enrich_max=0, facts=[fact],
            )
            degraded = BeautifulSoup(Path(path).read_text(encoding='utf-8'), 'html.parser')
        fallback = degraded.select_one('.reader-confirmed-card')
        self.assertIn('6 → 13', fallback.get_text(' ', strip=True))
        self.assertIsNone(fallback.get('data-range-context'))

    def test_exact_build_db2_identity_lookup_rejects_wrong_build_and_id(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        def response(build, rid, locale='enUS', selector='exact:136970'):
            props = {'filters': {'build': build, 'locale': locale,
                                 'filter': {'ID': selector}},
                     'entries': {'data': [{'ID': rid, 'OverrideName_lang': 'Reuse'}]}}
            return SimpleNamespace(status_code=200,
                                   text='<div data-page="' + html.escape(json.dumps({'props': props}), quote=True) + '"></div>')
        with patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.requests.get',
                   return_value=response('12.1.0.69587', 136970)):
            row = monitor._fetch_hotfix_db2_identity_row('TraitDefinition', '12.1.0.69587', 136970, 'enUS')
            self.assertEqual(row.get('OverrideName_lang'), 'Reuse')
        with patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.requests.get',
                   return_value=response('12.1.0.69933', 136970)):
            self.assertEqual(monitor._fetch_hotfix_db2_identity_row('TraitDefinition', '12.1.0.69587', 136970, 'enUS'), {})
        with patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.requests.get',
                   return_value=response('12.1.0.69587', 136971)):
            self.assertEqual(monitor._fetch_hotfix_db2_identity_row('TraitDefinition', '12.1.0.69587', 136970, 'enUS'), {})
        with patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.requests.get',
                   return_value=response('12.1.0.69587', 136970, locale='zhCN')):
            self.assertEqual(monitor._fetch_hotfix_db2_identity_row('TraitDefinition', '12.1.0.69587', 136970, 'enUS'), {})
        with patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.requests.get',
                   return_value=response('12.1.0.69587', 136970, selector='exact:136971')):
            self.assertEqual(monitor._fetch_hotfix_db2_identity_row('TraitDefinition', '12.1.0.69587', 136970, 'enUS'), {})

    def test_exact_build_high_value_row_retries_one_transient_timeout(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        for table, build, rid, field, value in (
                ('SpellEffect', '12.1.0.69933', 1284426, 'EffectMiscValue_0', '142879'),
                ('TraitDefinition', '12.1.0.69587', 136970, 'OverrideName_lang', 'Reuse')):
            props = {'filters': {'build': build, 'locale': 'enUS',
                                 'filter': {'ID': f'exact:{rid}'}},
                     'entries': {'data': [{'ID': rid, field: value}]}}
            response = SimpleNamespace(status_code=200,
                text='<div data-page="' + html.escape(json.dumps({'props': props}), quote=True) + '"></div>')
            with self.subTest(table=table), patch(
                    'botend.controller.plugins.wow.WagoSkillDiffMonitor.requests.get',
                    side_effect=[requests.ReadTimeout('transient'), response]) as get:
                if table == 'TraitDefinition':
                    row = monitor._fetch_hotfix_db2_identity_row(table, build, rid, 'enUS')
                else:
                    row = monitor._fetch_hotfix_db2_baseline_row(table, build, rid, 'enUS')
                self.assertEqual(row.get(field), value)
                self.assertEqual(get.call_count, 2)

    def test_unresolved_trait_uses_exact_build_db2_name_only_as_identity(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        source = {'id': 3, 'push_id': 112059, 'table_name': 'TraitDefinition',
                  'record_id': 136970, 'region_id': 3, 'locale': 'enUS',
                  'build': 69587, 'status': 1, 'data': ['Reuse', 136970]}
        sibling = {'id': 4, 'push_id': 112058, 'table_name': 'SpellName',
                   'record_id': 77, 'region_id': 3, 'locale': 'enUS',
                   'build': 69587, 'status': 1}
        facts = [
            {'source': source, 'after': None, 'before': None,
             'after_verified': False, 'before_verified': False, 'changes': []},
            {'source': sibling, 'source_build': '12.1.0.69587',
             'after': {'ID': '77', 'Name_lang': '早期名称'},
             'after_verified': True, 'before_verified': False, 'changes': []},
        ]
        with TemporaryDirectory() as root, override_settings(BASE_DIR=root,
                     WAGO_HOTFIX_READER_NAME_LOOKUPS=1), \
             patch.object(monitor, '_fetch_hotfix_db2_identity_row',
                          return_value={'ID': '136970', 'OverrideName_lang': 'Reuse'}) as lookup:
            path, _ = monitor._write_hotfix_full_html(
                branch='wow', locale='enUS', region_id=3, from_push=112057,
                to_push=112059, summary_title='未解码天赋身份',
                wago_url='https://wago.tools/hotfixes', build_num='69933',
                db2_build='12.1.0.69933',
                table_stats=[('TraitDefinition', 1), ('SpellName', 1)],
                by_table={'TraitDefinition': [source], 'SpellName': [sibling]},
                sample_per_table=1, enrich_max=0, facts=facts,
            )
            doc = BeautifulSoup(Path(path).read_text(encoding='utf-8'), 'html.parser')
        lookup.assert_called_once_with('TraitDefinition', '12.1.0.69587', 136970, 'enUS')
        record = doc.select_one('#table-traitdefinition article.record')
        self.assertIn('Reuse', record.select_one('.record-head').get_text(' ', strip=True))
        self.assertIn('未解码', record.get_text(' ', strip=True))
        label = doc.select_one('.reader-db2-name-card')
        self.assertIsNotNone(label)
        self.assertIn('Reuse', label.get_text(' ', strip=True))
        self.assertIn('136970', label.get_text(' ', strip=True))
        self.assertIn('改动未知', label.get_text(' ', strip=True))
        self.assertIsNone(doc.select_one('.reader-confirmed-card'))

    def test_unresolved_trait_keeps_literal_source_text_when_db2_lookup_fails(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        source = {'id': 3, 'push_id': 112059, 'table_name': 'TraitDefinition',
                  'record_id': 136970, 'region_id': 3, 'locale': 'enUS',
                  'build': 69587, 'status': 1, 'data': ['Reuse', 136970]}
        fact = {'source': source, 'after': None, 'before': None,
                'after_verified': False, 'before_verified': False, 'changes': []}
        with TemporaryDirectory() as root, override_settings(BASE_DIR=root,
                     WAGO_HOTFIX_READER_NAME_LOOKUPS=1), \
             patch.object(monitor, '_fetch_hotfix_db2_identity_row', return_value={}):
            path, _ = monitor._write_hotfix_full_html(
                branch='wow', locale='enUS', region_id=3, from_push=112058,
                to_push=112059, summary_title='来源文本回退',
                wago_url='https://wago.tools/hotfixes', build_num='69933',
                db2_build='12.1.0.69933', table_stats=[('TraitDefinition', 1)],
                by_table={'TraitDefinition': [source]}, sample_per_table=1,
                enrich_max=0, facts=[fact],
            )
            doc = BeautifulSoup(Path(path).read_text(encoding='utf-8'), 'html.parser')
        card = doc.select_one('.reader-source-text-card')
        self.assertIsNotNone(card)
        self.assertIn('来源文本：Reuse', card.get_text(' ', strip=True))
        self.assertIn('列名未核对', card.get_text(' ', strip=True))
        self.assertIsNone(doc.select_one('.reader-db2-name-card'))
        self.assertIsNone(doc.select_one('.reader-confirmed-card'))

    def test_item_relation_title_uses_previous_frozen_itemsparse_name(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        def source(table, rid, push):
            return {'id': rid * 100 + push, 'push_id': push, 'table_name': table,
                    'record_id': rid, 'region_id': 3, 'locale': 'enUS',
                    'build': 69814, 'status': 1}
        spec = source('ItemSpecOverride', 160122, 112055)
        before = source('ItemSparse', 270845, 112054)
        future = source('ItemSparse', 270845, 112056)
        facts = [
            {'source': spec, 'source_build': '12.1.0.69814',
             'after': {'ID': '160122', 'ItemID': '270845', 'ChrSpecializationID': '267'},
             'before': {'ID': '160122', 'ItemID': '270845', 'ChrSpecializationID': '62'},
             'after_verified': True, 'before_verified': True,
             'changes': [{'field': 'ChrSpecializationID', 'before': '62', 'after': '267'}]},
            {'source': before, 'source_build': '12.1.0.69814',
             'after': {'ID': '270845', 'Display_lang': '烈毒角斗士的法杖'},
             'after_verified': True, 'before_verified': False, 'changes': []},
            {'source': future, 'source_build': '12.1.0.69814',
             'after': {'ID': '270845', 'Display_lang': '未来改名'},
             'after_verified': True, 'before_verified': False, 'changes': []},
        ]
        with TemporaryDirectory() as root, override_settings(BASE_DIR=root):
            path, _ = monitor._write_hotfix_full_html(
                branch='wow', locale='enUS', region_id=3, from_push=112053,
                to_push=112056, summary_title='物品外键查名',
                wago_url='https://wago.tools/hotfixes', build_num='69814',
                db2_build='12.1.0.69814',
                table_stats=[('ItemSpecOverride', 1), ('ItemSparse', 2)],
                by_table={'ItemSpecOverride': [spec], 'ItemSparse': [before, future]},
                sample_per_table=2, enrich_max=0, facts=facts,
            )
            doc = BeautifulSoup(Path(path).read_text(encoding='utf-8'), 'html.parser')
        title = doc.select_one('.reader-confirmed-card h4').get_text(' ', strip=True)
        self.assertIn('烈毒角斗士的法杖', title)
        self.assertIn('270845', title)
        self.assertNotIn('未来改名', title)

    def test_verified_spell_title_uses_db2_name_at_source_build_not_report_end_build(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        source = {'id': 1, 'push_id': 112208, 'table_name': 'SpellEffect',
                  'record_id': 1322073, 'region_id': 3, 'locale': 'enUS',
                  'build': 69933, 'status': 1}
        fact = {'source': source, 'source_build': '12.1.0.69933',
                'after': {'ID': '1322073', 'SpellID': '1298418', 'EffectIndex': '0',
                          'Effect': '2',
                          'EffectBasePointsF': '2.55'},
                'before': {'ID': '1322073', 'SpellID': '1298418', 'EffectIndex': '0',
                           'Effect': '2',
                           'EffectBasePointsF': '4.25'},
                'after_verified': True, 'before_verified': True,
                'changes': [{'field': 'EffectBasePointsF', 'before': '4.25', 'after': '2.55'}]}
        queries = []
        def names(ids, branch, build, **kwargs):
            queries.append(build)
            return {1298418: {'name': '岩石剧毒' if build == '12.1.0.69933' else '未来名称'}}
        with TemporaryDirectory() as root, override_settings(BASE_DIR=root), \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.database_spell_metadata', side_effect=names):
            path, _ = monitor._write_hotfix_full_html(
                branch='wow', locale='enUS', region_id=3, from_push=112207,
                to_push=112208, summary_title='按来源 build 查名',
                wago_url='https://wago.tools/hotfixes', build_num='70000',
                db2_build='12.1.0.70000', table_stats=[('SpellEffect', 1)],
                by_table={'SpellEffect': [source]}, sample_per_table=1,
                enrich_max=0, facts=[fact],
            )
            doc = BeautifulSoup(Path(path).read_text(encoding='utf-8'), 'html.parser')
        title = doc.select_one('.reader-confirmed-card h4').get_text(' ', strip=True)
        self.assertIn('岩石剧毒', title)
        self.assertIn('作用：造成伤害', doc.select_one('.reader-confirmed-card').get_text(' ', strip=True))
        self.assertIn('1298418', title)
        self.assertNotIn('未来名称', title)
        self.assertIn('12.1.0.69933', queries)
        self.assertEqual(doc.select_one('.reader-confirmed-card')['data-search'].count('1322073'), 1)

    def test_trait_definition_frozen_override_name_is_title_not_generic_id(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        source = {'id': 2, 'push_id': 112059, 'table_name': 'TraitDefinition',
                  'record_id': 136970, 'region_id': 3, 'locale': 'enUS',
                  'build': 69587, 'status': 1}
        fact = {'source': source, 'source_build': '12.1.0.69587',
                'after': {'ID': '136970', 'OverrideName_lang': 'Reuse', 'SpellID': '0'},
                'before': {'ID': '136970', 'OverrideName_lang': 'Old', 'SpellID': '0'},
                'after_verified': True, 'before_verified': True,
                'changes': [{'field': 'OverrideName_lang', 'before': 'Old', 'after': 'Reuse'}]}
        with TemporaryDirectory() as root, override_settings(BASE_DIR=root):
            path, _ = monitor._write_hotfix_full_html(
                branch='wow', locale='enUS', region_id=3, from_push=112058,
                to_push=112059, summary_title='天赋冻结名',
                wago_url='https://wago.tools/hotfixes', build_num='69587',
                db2_build='12.1.0.69587', table_stats=[('TraitDefinition', 1)],
                by_table={'TraitDefinition': [source]}, sample_per_table=1,
                enrich_max=0, facts=[fact],
            )
            doc = BeautifulSoup(Path(path).read_text(encoding='utf-8'), 'html.parser')
        title = doc.select_one('.reader-confirmed-card h4').get_text(' ', strip=True)
        self.assertIn('Reuse', title)
        self.assertIn('136970', title)
        self.assertNotIn('技能 #0', title)

    def test_same_spell_effect_change_groups_effect_positions_without_losing_sources(self):
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        sources = [{'id': rid, 'push_id': 112233, 'table_name': 'SpellEffect',
                    'record_id': rid, 'region_id': 3, 'locale': 'enUS', 'status': 1}
                   for rid in (1357276, 1357280, 1357281)]
        facts = [
            {'source': source,
             'after': {'ID': str(source['record_id']), 'SpellID': '1222923',
                       'EffectIndex': str(index), 'Effect': '6', 'EffectAura': '649'},
             'before': {'ID': str(source['record_id']), 'SpellID': '1222923',
                        'EffectIndex': str(index), 'Effect': '6', 'EffectAura': '218'},
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
        self.assertEqual(card.get_text(' ', strip=True).count('→'), 1)
        self.assertIn('218（标签百分比修正）', card.get_text(' ', strip=True))
        self.assertIn('649（标签 PvP 倍率百分比修正）', card.get_text(' ', strip=True))
        self.assertEqual(card.get_text(' ', strip=True).count('作用：施加光环'), 1)
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
        self.assertIsNone(reader.select_one('.reader-confirmed'))
        self.assertIsNone(reader.select_one('.reader-verdict'))
        self.assertIn('20 秒', reader.select_one('.reader-configs').get_text(' ', strip=True))
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
        self.assertIn('1 项热修历史变化', doc.select_one('.quick-facts').get_text(' ', strip=True))
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
        self.assertLess(str(reader).index('class="reader-confirmed"'),
                        str(reader).index('class="reader-configs"'))
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
