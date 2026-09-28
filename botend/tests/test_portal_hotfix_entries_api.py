import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.test import TestCase, override_settings
from django.core.cache import cache

from botend.models import WowHotfixReport, WowSpellSnapshot
from botend.services.wago_db2.schema import WagoDB2Schema
from botend.services.wow_skill_report_metadata import database_spell_metadata


@override_settings(ALLOWED_HOSTS=['testserver'])
class PortalHotfixEntriesAPITests(TestCase):
    def setUp(self):
        self.spell = self.fact(11, 100, 'SpellName', 500, {'ID': '500', 'Name_lang': '岩石剧毒'})
        self.effect = self.fact(12, 100, 'SpellEffect', 900, {
            'ID': '900', 'SpellID': '500', 'EffectIndex': '0', 'EffectBasePointsF': '2.55',
        }, before={'ID': '900', 'SpellID': '500', 'EffectIndex': '0', 'EffectBasePointsF': '4.25'},
            changes=[{'field': 'EffectBasePointsF', 'before': '4.25', 'after': '2.55'}])
        self.misc = self.fact(13, 101, 'SpellMisc', 901, {
            'ID': '901', 'SpellID': '500', 'RangeIndex': '13',
        })
        self.older = self.report(100, 101, [self.spell, self.effect, self.misc])
        self.invalidate = self.fact(14, 102, 'SpellScript', 86172, None, status=3)
        self.newer = self.report(101, 102, [self.invalidate, self.effect])
        self.report(102, 103, [self.fact(15, 103, 'SpellEffect', 999, {'ID': '999'})],
                    complete=False)

    @staticmethod
    def fact(source_id, push, table, rid, after, status=1, before=None, changes=None,
             build='12.1.0.69933'):
        return {
            'source': {'id': source_id, 'push_id': push, 'table_name': table,
                       'record_id': rid, 'status': status, 'data': None if status == 3 else ['payload'],
                       'build': 69933, 'locale': 'enUS', 'region_id': 3,
                       'created_at': f'2026-09-24T00:00:{push % 60:02d}'},
            'source_build': build if after is not None else None,
            'after_verified': after is not None,
            'before_verified': before is not None,
            'after': after, 'before': before, 'changes': changes or [],
        }

    @staticmethod
    def report(start, end, facts, *, complete=True, branch='wow'):
        return WowHotfixReport.objects.create(
            branch=branch, locale='enUS', region_id=3,
            build_str='12.1.0.69933', build_num='69933',
            from_push=start, to_push=end,
            entry_count=len(facts), table_count=len({x['source']['table_name'] for x in facts}),
            collection_complete=complete, source_facts_json=json.dumps(facts),
            content_html_path=f'portal/reports/test-{branch}-{end}.html',
        )

    def read(self, **params):
        return self.client.get('/portal/api/hotfix-entries/', params)

    def test_continuous_rows_sort_by_push_and_dedupe_overlapping_reports(self):
        response = self.read(page=1, page_size=2)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['meta']['total'], 4)
        self.assertEqual(data['meta']['total_pages'], 2)
        self.assertEqual([(r['table'], r['record_id'], r['push']) for r in data['data']],
                         [('SpellScript', 86172, 102), ('SpellMisc', 901, 101)])
        invalid = data['data'][0]
        self.assertEqual(invalid['kind'], 'status')
        self.assertEqual(invalid['spell_id'], None)
        self.assertEqual(invalid['region_name'], 'Retail EU')
        self.assertEqual(invalid['locale'], 'enUS')
        self.assertNotIn('Spell 86172', invalid['title'])
        self.assertIn('Invalidate', invalid['status_label'])
        self.assertTrue(invalid['report_url'].endswith(f'/{self.newer.id}/'))
        self.assertEqual(data['meta']['page_size'], 2)
        second = self.read(page=2, page_size=2).json()['data']
        self.assertEqual([r['push'] for r in second], [100, 100])
        effect = next(r for r in second if r['table'] == 'SpellEffect')
        self.assertEqual(effect['title'], '岩石剧毒')
        self.assertEqual(effect['kind'], 'change')
        self.assertEqual(effect['spell_id'], 500)
        self.assertIn('EffectBasePointsF', effect['fields'][0]['label'])
        self.assertIn('4.25', effect['fields'][0]['text'])
        self.assertIn('2.55', effect['fields'][0]['text'])

    def test_filters_field_name_status_table_build_and_mode_before_pagination(self):
        for params, count in [
            ({'mode': 'changes'}, 1), ({'mode': 'values'}, 3), ({'mode': 'status'}, 1),
            ({'table': 'SpellEffect'}, 1), ({'q': 'EffectBasePointsF'}, 1),
            ({'q': '岩石剧毒'}, 3), ({'q': '86172'}, 1),
            ({'q': '101'}, 1), ({'build': '12.1.0.69933'}, 4),
            ({'build': '12.1.0.1'}, 0), ({'branch': 'wowt'}, 0),
        ]:
            with self.subTest(params=params):
                self.assertEqual(self.read(**params).json()['meta']['total'], count)
        self.assertEqual(self.read(mode='changes').json()['data'][0]['kind'], 'change')
        self.assertEqual(self.read(page_size=1000).json()['meta']['page_size'], 50)
        self.assertEqual(self.read(mode='bad').status_code, 400)

    def test_explicit_change_first_sort_preserves_kind_counts_and_push_order(self):
        original = self.read(mode='values', page_size=2).json()
        self.assertEqual([(row['push'], row['kind']) for row in original['data']],
                         [(101, 'new_value'), (100, 'change')])
        self.assertEqual(original['meta']['sort'], 'latest')
        prioritized = self.read(mode='values', sort='changes_first', page_size=2).json()
        self.assertEqual(prioritized['meta']['total'], 3)
        self.assertEqual(prioritized['meta']['counts'],
                         {'change': 1, 'new_value': 2, 'status': 0, 'unresolved': 0})
        self.assertEqual(prioritized['meta']['sort'], 'changes_first')
        self.assertEqual([(row['push'], row['kind']) for row in prioritized['data']],
                         [(100, 'change'), (101, 'new_value')])
        self.assertEqual(prioritized['data'][0]['fields'][0]['text'], '4.25 → 2.55')
        second = self.read(mode='values', sort='changes_first', page_size=2, page=2).json()
        self.assertEqual([(row['push'], row['kind']) for row in second['data']],
                         [(100, 'new_value')])
        self.assertEqual(self.read(sort='not-allowed').status_code, 400)

    def test_first_screen_spell_id_uses_existing_build_scoped_chinese_snapshot_name(self):
        WowSpellSnapshot.objects.create(branch='wow', locale='zhCN', spell_id=1222923,
            snapshot_build='12.1.0.69587', name='Windwalker Monk', name_zh='踏风武僧')
        WowSpellSnapshot.objects.create(branch='wowt', locale='zhCN', spell_id=1222923,
            snapshot_build='12.1.0.69933', name_zh='错误的其他分支')
        WowSpellSnapshot.objects.create(branch='wow', locale='enUS', spell_id=1222923,
            snapshot_build='12.1.0.70001', name_zh='未来名称')
        effect = self.fact(26, 104, 'SpellEffect', 1357281,
                           {'ID': '1357281', 'SpellID': '1222923', 'EffectIndex': '0',
                            'EffectAura': '649'},
                           before={'ID': '1357281', 'SpellID': '1222923',
                                   'EffectIndex': '0', 'EffectAura': '218'},
                           changes=[{'field': 'EffectAura', 'before': '218', 'after': '649'}])
        self.report(103, 104, [effect])
        row = self.read(q='1357281', sort='changes_first').json()['data'][0]
        self.assertEqual(row['title'], '踏风武僧')
        self.assertEqual(row['spell_id'], 1222923)
        self.assertEqual(row['record_id'], 1357281)
        self.assertEqual(row['fields'][0]['before'], '218')
        self.assertEqual(self.read(q='踏风武僧').json()['meta']['total'], 1)

    def test_frozen_spell_names_use_nearest_prior_push_and_exact_scope(self):
        effect = self.fact(30, 102, 'SpellEffect', 902,
                           {'ID': '902', 'SpellID': '620', 'EffectBasePointsF': '9'},
                           build='12.1.0.69814')
        effect['source']['build'] = 69814
        earlier = self.fact(31, 100, 'SpellName', 620,
                            {'ID': '620', 'Name_lang': '此前名称'},
                            build='12.1.0.69814')
        earlier['source']['build'] = 69814
        future = self.fact(32, 104, 'SpellName', 620,
                           {'ID': '620', 'Name_lang': '未来改名'},
                           build='12.1.0.69814')
        future['source']['build'] = 69814
        other_build = self.fact(33, 101, 'SpellName', 620,
                                {'ID': '620', 'Name_lang': '另一构建'},
                                build='12.1.0.69933')
        other_branch = self.report(99, 105, [self.fact(34, 101, 'SpellName', 620,
            {'ID': '620', 'Name_lang': '另一分支'})], branch='wowt')
        self.report(99, 105, [effect, earlier, future, other_build])
        self.assertTrue(other_branch.collection_complete)
        row = self.read(q='902', branch='wow').json()['data'][0]
        self.assertEqual(row['title'], '此前名称')
        self.assertEqual(row['build'], '12.1.0.69814')
        self.assertEqual(row['spell_id'], 620)

    def test_spell_names_are_batched_once_and_reused_across_page_requests(self):
        WowSpellSnapshot.objects.create(branch='wow', locale='zhCN', spell_id=1222923,
            snapshot_build='12.1.0.69587', name_zh='踏风武僧')
        effect = self.fact(36, 104, 'SpellEffect', 1357281,
                           {'ID': '1357281', 'SpellID': '1222923', 'EffectAura': '649'})
        self.report(103, 104, [effect])
        cache.clear()
        with patch('botend.services.wow_hotfix_entries.database_spell_metadata',
                   wraps=database_spell_metadata) as lookup:
            for query in ('1357281', '踏风武僧'):
                row = self.read(q=query).json()['data'][0]
                self.assertEqual(row['title'], '踏风武僧')
        lookup.assert_called_once()
        self.assertEqual(lookup.call_args.args[1:], ('wow', '12.1.0.69933'))
        self.assertEqual(lookup.call_args.kwargs, {'allow_compatible_name': True})

    def test_frozen_spell_name_does_not_cross_region_or_locale(self):
        effect = self.fact(40, 104, 'SpellEffect', 903,
                           {'ID': '903', 'SpellID': '621', 'EffectBasePointsF': '1'})
        name = self.fact(41, 103, 'SpellName', 621,
                         {'ID': '621', 'Name_lang': '其他区域的名字'})
        name['source']['region_id'] = 2
        other_region = self.report(102, 106, [name])
        other_region.region_id = 2
        other_region.save(update_fields=['region_id'])
        self.report(103, 104, [effect])
        row = self.read(q='903').json()['data'][0]
        self.assertEqual(row['title'], '技能 #621')
        self.assertEqual(self.read(q='其他区域的名字').json()['meta']['total'], 1)

    def test_noncomplete_and_nonmatching_facts_are_not_mislabeled_as_changes(self):
        self.assertNotIn(999, [r['record_id'] for r in self.read().json()['data']])
        misc = next(r for r in self.read().json()['data'] if r['record_id'] == 901)
        self.assertEqual(misc['kind'], 'new_value')
        self.assertTrue(misc['fields'])
        self.assertTrue(all(field['before'] is None for field in misc['fields']))
        self.assertNotIn('→', ' '.join(field['text'] for field in misc['fields']))
        invalid = self.read(mode='status').json()['data'][0]
        self.assertEqual(invalid['fields'], [])
        self.assertTrue(invalid['source_url'].startswith('https://wago.tools/hotfixes?'))

    def test_published_exact_baseline_card_is_presented_but_not_counted_as_history(self):
        with TemporaryDirectory() as root, override_settings(BASE_DIR=root):
            path = Path(root) / 'static' / self.older.content_html_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('<article class="record">other source</article>' * 600 + '''<article class="reader-impact-card"
                data-baseline-key="spellmisc:12.1.0.69933:enUS:901:101"><ul><li>
                <span>范围配置 / RangeIndex</span>
                <strong>6（100 码） → 13（50000 码）</strong></li></ul></article>
                <article class="reader-impact-card"
                data-baseline-key="spellmisc:12.1.0.69933:enUS:999:101"><ul><li>
                <span>其他 / RangeIndex</span><strong>999 → 13</strong></li></ul></article>''',
                encoding='utf-8')
            cache.clear()
            with patch('botend.services.wow_hotfix_entries.BeautifulSoup', wraps=BeautifulSoup) as parser:
                row = self.read(q='901').json()['data'][0]
            self.assertTrue(parser.call_args_list)
            self.assertLess(max(len(call.args[0]) for call in parser.call_args_list), 5000)
            self.assertEqual(row['kind'], 'new_value')
            self.assertIn('非线上前态', row['status_label'])
            self.assertIn('6（100 码） → 13（50000 码）', row['fields'][0]['text'])
            self.assertEqual(self.read(mode='changes').json()['meta']['total'], 1)
            self.assertNotIn('999 → 13', json.dumps(self.read(q='901').json()['data'], ensure_ascii=False))

    def test_baseline_card_can_expose_a_field_hidden_in_new_value_preview(self):
        item = self.fact(20, 101, 'ItemSparse', 171692,
                         {'ID': '171692', 'Display_lang': 'Shoulderpads', 'Flags_3': '4',
                          **{f'A_{n:02d}': str(n + 1) for n in range(20)}})
        self.report(100, 101, [item], branch='wowt')
        report = WowHotfixReport.objects.get(branch='wowt')
        with TemporaryDirectory() as root, override_settings(BASE_DIR=root):
            path = Path(root) / 'static' / report.content_html_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('''<article class="reader-impact-card"
                data-baseline-key="itemsparse:12.1.0.69933:enUS:171692:101"><ul><li>
                <span>物品标志[3] / Flags_3</span><strong>0 → 4</strong></li></ul></article>''',
                encoding='utf-8')
            cache.clear()
            row = self.read(branch='wowt', q='171692').json()['data'][0]
        self.assertIn('非线上前态', row['status_label'])
        self.assertIn(('Flags_3', '0 → 4'), [(field['key'], field['text']) for field in row['fields']])

    def test_same_record_on_a_later_push_remains_a_separate_physical_event(self):
        self.report(103, 104, [self.fact(16, 104, 'SpellScript', 86172, None, status=3)])
        payload = self.read(q='86172').json()
        self.assertEqual(payload['meta']['total'], 2)
        self.assertEqual([row['push'] for row in payload['data']], [104, 102])
        self.assertTrue(all(row['kind'] == 'status' for row in payload['data']))

    def test_completed_report_with_mismatched_source_count_fails_closed(self):
        self.newer.entry_count += 1
        self.newer.save(update_fields=['entry_count'])
        response = self.read()
        self.assertEqual(response.status_code, 503)
        self.assertIn('frozen source count mismatch', response.json()['error'])

    def test_searches_unpreviewed_frozen_field_and_shows_its_raw_value(self):
        facts = json.loads(self.older.source_facts_json)
        for fact in facts:
            if fact['source']['record_id'] == 901:
                fact['after'].update({f'Attributes_{n}': str(n) for n in range(20)})
        self.older.source_facts_json = json.dumps(facts)
        self.older.save(update_fields=['source_facts_json'])
        rows = self.read(q='Attributes_19').json()['data']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['record_id'], 901)
        self.assertIn('Attributes_19', [field['key'] for field in rows[0]['fields']])
        self.assertNotIn('_raw_search', rows[0])
        self.assertLessEqual(len(rows[0]['fields']), 13)

    def test_visual_and_attribute_fields_use_central_labels_with_original_keys(self):
        self.assertEqual(WagoDB2Schema().field_label('Attributes_9'), '属性位组[9]')
        visual = self.fact(26, 104, 'SpellXSpellVisual', 536273,
                           {'ID': '536273', 'SpellID': '500', 'Probability': '1',
                            'SpellVisualID': '154439'})
        misc = self.fact(27, 104, 'SpellMisc', 867977,
                         {'ID': '867977', 'SpellID': '500', 'Attributes_9': '4096'})
        self.report(103, 104, [visual, misc], branch='wowt')
        rows = self.read(branch='wowt', page_size=20).json()['data']
        visual_fields = {f['key']: f for f in next(r for r in rows if r['table'] == 'SpellXSpellVisual')['fields']}
        misc_fields = {f['key']: f for f in next(r for r in rows if r['table'] == 'SpellMisc')['fields']}
        self.assertIn('概率 / Probability', visual_fields['Probability']['label'])
        self.assertIn('技能视觉效果 ID / SpellVisualID', visual_fields['SpellVisualID']['label'])
        self.assertIn('属性位组[9] / Attributes_9', misc_fields['Attributes_9']['label'])
