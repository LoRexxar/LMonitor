from collections import OrderedDict
from urllib.parse import parse_qs, urlsplit
from django.test import SimpleTestCase
from unittest.mock import patch

from botend.services.wago_hotfix_facts import decode_hotfix_row, previous_hotfix_payload, field_changes, build_hotfix_facts, project_class_spell_changes
from botend.services.wago_hotfix_source import collect_hotfix_build_rows, collect_hotfix_push_rows, HotfixSourceIncomplete


class WagoHotfixFactsTests(SimpleTestCase):
    def test_push_search_recovers_from_repeated_pages_by_conserved_record_prefixes(self):
        rows = [
            {'id': index + 1, 'push_id': 111885, 'locale': 'enUS',
             'region_id': 3 if index % 2 else 1, 'table_name': 'SpellEffect',
             'record_id': record_id, 'build': 69587}
            for index, record_id in enumerate((10, 11, 20, 21))
        ]
        def fetch(url, **kwargs):
            params = parse_qs(urlsplit(url).query)
            query = params['search'][0]
            prefix = query.removeprefix('enUS 111885').strip()
            page = int(params.get('page', ['1'])[0])
            matched = [row for row in rows if str(row['record_id']).startswith(prefix)]
            if not prefix:
                # Both pages return the same slice despite a stable total.
                data, last_page, per_page = rows[:2], 2, 2
            else:
                data, last_page, per_page = matched, 1, 25
            return {'filters': {'search': parse_qs(urlsplit(url).query)['search'][0]}, 'hotfixes': {'data': data, 'total': len(matched),
                                 'current_page': page, 'last_page': last_page,
                                 'per_page': per_page}}
        self.assertEqual(
            [row['id'] for row in collect_hotfix_push_rows(
                fetch, lambda value: value, 111885, region_id=3, locale='enUS', max_pages=2)],
            [2, 4],
        )

    def test_push_prefix_search_rejects_nonconserved_totals(self):
        def fetch(url, **kwargs):
            query = parse_qs(urlsplit(url).query)['search'][0]
            prefix = query.removeprefix('enUS 111885').strip()
            page = int(parse_qs(urlsplit(url).query).get('page', ['1'])[0])
            row = {'id': 1, 'push_id': 111885, 'locale': 'enUS', 'region_id': 3,
                   'record_id': 10, 'table_name': 'SpellEffect'}
            return {'filters': {'search': parse_qs(urlsplit(url).query)['search'][0]}, 'hotfixes': {'data': [row] if not prefix else [],
                                 'total': 2 if not prefix else 0,
                                 'current_page': page, 'last_page': 2 if not prefix else 1,
                                 'per_page': 1}}
        with self.assertRaises(HotfixSourceIncomplete):
            collect_hotfix_push_rows(fetch, lambda value: value, 111885,
                                     region_id=3, locale='enUS', max_pages=2)

    def test_push_prefix_search_rejects_rows_matching_other_fields_not_record_prefix(self):
        # The site can match the query in payload/data, not just record_id.
        row = {'id': 1, 'push_id': 111885, 'locale': 'enUS', 'region_id': 3,
               'record_id': 97, 'table_name': 'SpellEffect'}
        def fetch(url, **kwargs):
            query = parse_qs(urlsplit(url).query)['search'][0]
            page = int(parse_qs(urlsplit(url).query).get('page', ['1'])[0])
            prefix = query.removeprefix('enUS 111885').strip()
            if not prefix:
                data, total, pages, per_page = [row], 2, 2, 1
            elif prefix == '1':
                data, total, pages, per_page = [row], 1, 1, 25
            else:
                data, total, pages, per_page = [], 0, 1, 25
            return {'filters': {'search': parse_qs(urlsplit(url).query)['search'][0]}, 'hotfixes': {'data': data, 'total': total, 'current_page': page,
                                 'last_page': pages, 'per_page': per_page}}
        with self.assertRaisesRegex(HotfixSourceIncomplete, 'prefix row mismatched'):
            collect_hotfix_push_rows(fetch, lambda value: value, 111885,
                                     region_id=3, locale='enUS', max_pages=2)

    def test_pagination_retries_cannot_combine_two_individually_incomplete_snapshots(self):
        attempts = 0
        def fetch(url, **kwargs):
            nonlocal attempts
            page = 2 if 'page=2' in url else 1
            if page == 1:
                attempts += 1
            source_id = 1 if attempts == 1 else 2
            row = {'id': source_id, 'push_id': 112185, 'region_id': 3, 'locale': 'enUS',
                   'table_name': 'SpellEffect', 'record_id': source_id, 'build': 69933}
            return {'filters': {'search': parse_qs(urlsplit(url).query)['search'][0]}, 'hotfixes': {'data': [row], 'total': 2, 'current_page': page,
                                 'last_page': 2, 'per_page': 1}}
        with self.assertRaises(HotfixSourceIncomplete):
            collect_hotfix_push_rows(fetch, lambda value: value, 112185,
                                     region_id=3, locale='enUS', max_pages=2)

    def test_build_discovery_requires_full_source_pages_and_exact_build_region(self):
        rows = [
            {'id': 1, 'region_id': 3, 'locale': 'enUS', 'table_name': 'SpellEffect', 'record_id': 17, 'push_id': 112185, 'build': 69933},
            {'id': 2, 'region_id': 1, 'locale': 'enUS', 'table_name': 'SpellEffect', 'record_id': 17, 'push_id': 112218, 'build': 69933},
            {'id': 3, 'region_id': 3, 'locale': 'enUS', 'table_name': 'SpellEffect', 'record_id': 18, 'push_id': 112208, 'build': 69932},
        ]
        def fetch(url, **kwargs):
            page = 2 if 'page=2' in url else 1
            subset = rows[(page - 1) * 2:page * 2]
            return {'filters': {'search': parse_qs(urlsplit(url).query)['search'][0]}, 'hotfixes': {'data': subset, 'total': 3, 'current_page': page,
                                 'last_page': 2, 'per_page': 2}}
        self.assertEqual(collect_hotfix_build_rows(fetch, lambda value: value, 69933,
                                                   region_id=3, locale='enUS', max_pages=2), [rows[0]])
        with self.assertRaisesRegex(HotfixSourceIncomplete, 'inconsistent pagination'):
            collect_hotfix_build_rows(fetch, lambda value: value, 69933,
                                      region_id=3, locale='enUS', max_pages=1)

    def setUp(self):
        self.schema = OrderedDict((key, value) for key, value in (
            ('ID', '1106904'), ('EffectAura', '0'), ('EffectMiscValue_0', '0'),
            ('EffectMiscValue_1', '0'), ('BonusCoefficientFromAP', '6.9677400588989'),
            ('PvpMultiplier', '0.68000000715256'), ('SpellID', '427453'),
        ))
        self.old = [1106904, 0, [0, 0], '6.9677400588989', '0.68000000715256', 427453]
        self.new = [1106904, 0, [0, 0], '10.451600074768', '0.5440000295639', 427453]

    def test_decodes_grouped_columns_without_reordering_or_rounding(self):
        row = decode_hotfix_row(self.new, self.schema, record_id=1106904)
        self.assertEqual(row['SpellID'], '427453')
        self.assertEqual(row['EffectMiscValue_0'], '0')
        self.assertEqual(row['EffectMiscValue_1'], '0')
        self.assertEqual(row['BonusCoefficientFromAP'], '10.451600074768')
        self.assertEqual(field_changes(
            decode_hotfix_row(self.old, self.schema, record_id=1106904), row,
        ), [
            {'field': 'BonusCoefficientFromAP', 'before': '6.9677400588989', 'after': '10.451600074768'},
            {'field': 'PvpMultiplier', 'before': '0.68000000715256', 'after': '0.5440000295639'},
        ])

    def test_rejects_missing_or_wrong_payload_identity_and_layout(self):
        self.assertIsNone(decode_hotfix_row(None, self.schema, record_id=1106904))
        self.assertIsNone(decode_hotfix_row([1] + self.new[1:], self.schema, record_id=1106904))
        self.assertIsNone(decode_hotfix_row(self.new[:-1], self.schema, record_id=1106904))
        self.assertIsNone(decode_hotfix_row(self.new[:2] + [[0]] + self.new[3:], self.schema, record_id=1106904))

    def test_table_schema_is_reusable_without_becoming_another_records_values(self):
        different_id = 1106905
        payload = [different_id] + self.new[1:]
        decoded = decode_hotfix_row(payload, self.schema, record_id=different_id)
        self.assertEqual(decoded['ID'], str(different_id))
        self.assertEqual(decoded['BonusCoefficientFromAP'], '10.451600074768')
        self.assertNotEqual(decoded['BonusCoefficientFromAP'], self.schema['BonusCoefficientFromAP'])
        self.assertIsNone(decode_hotfix_row(payload, self.schema, record_id=1106904))

    def test_previous_payload_is_scoped_to_exact_region_locale_table_and_id(self):
        records = [
            {'id': 1, 'region_id': 1, 'locale': 'enUS', 'table_name': 'SpellEffect', 'record_id': 1106904, 'push_id': 111070, 'data': self.old},
            {'id': 2, 'region_id': 3, 'locale': 'enUS', 'table_name': 'SpellEffect', 'record_id': 1106904, 'push_id': 112184, 'data': self.new},
            {'id': 3, 'region_id': 1, 'locale': 'esMX', 'table_name': 'SpellEffect', 'record_id': 1106904, 'push_id': 112184, 'data': self.new},
            {'id': 4, 'region_id': 1, 'locale': 'enUS', 'table_name': 'SpellMisc', 'record_id': 1106904, 'push_id': 112184, 'data': self.new},
            {'id': 5, 'region_id': 1, 'locale': 'enUS', 'table_name': 'SpellEffect', 'record_id': 1106904, 'push_id': 112185, 'data': self.new},
        ]
        self.assertEqual(previous_hotfix_payload(records, records[-1]), self.old)
        self.assertIsNone(previous_hotfix_payload(records[:4], records[0]))

    def test_conflicting_same_push_does_not_choose_arbitrary_predecessor(self):
        records = [
            {'id': 1, 'region_id': 1, 'locale': 'enUS', 'table_name': 'SpellEffect', 'record_id': 1106904, 'push_id': 112184, 'data': self.old},
            {'id': 2, 'region_id': 1, 'locale': 'enUS', 'table_name': 'SpellEffect', 'record_id': 1106904, 'push_id': 112184, 'data': self.new},
            {'id': 3, 'region_id': 1, 'locale': 'enUS', 'table_name': 'SpellEffect', 'record_id': 1106904, 'push_id': 112185, 'data': self.new},
        ]
        self.assertIsNone(previous_hotfix_payload(records, records[-1]))

    def test_shared_fact_set_preserves_verified_delta_and_unknown_predecessor(self):
        hammer = {'id': 11, 'region_id': 1, 'locale': 'enUS', 'table_name': 'SpellEffect',
                  'record_id': 1106904, 'push_id': 112185, 'status': 1, 'data': self.new}
        another = {'id': 12, 'region_id': 1, 'locale': 'enUS', 'table_name': 'SpellEffect',
                   'record_id': 1106905, 'push_id': 112185, 'status': 1, 'data': [1106905] + self.new[1:]}
        facts = build_hotfix_facts(
            [hammer, another],
            schema_for=lambda row: self.schema if row['record_id'] == 1106904 else OrderedDict([('ID', '1106905')] + list(self.schema.items())[1:]),
            previous_for=lambda row: self.old if row['record_id'] == 1106904 else None,
        )
        self.assertEqual(facts[0]['changes'][0], {
            'field': 'BonusCoefficientFromAP', 'before': '6.9677400588989', 'after': '10.451600074768',
        })
        self.assertEqual(facts[0]['after']['SpellID'], '427453')
        self.assertEqual(facts[0]['source']['region_id'], 1)
        self.assertIsNone(facts[1]['before'])
        self.assertEqual(facts[1]['changes'], [])
        self.assertEqual(facts[1]['after']['BonusCoefficientFromAP'], '10.451600074768')

    def test_class_projection_keeps_physical_effect_and_unverified_predecessor_explicit(self):
        hammer = {'source': {'table_name': 'SpellEffect', 'record_id': 1106904, 'push_id': 112185},
                  'after': {'ID': '1106904', 'SpellID': '427453', 'EffectIndex': '0', 'BonusCoefficientFromAP': '10.451600074768'},
                  'before_verified': True,
                  'changes': [{'field': 'BonusCoefficientFromAP', 'before': '6.9677400588989', 'after': '10.451600074768'}]}
        unknown = {'source': {'table_name': 'SpellEffect', 'record_id': 891602, 'push_id': 112183},
                   'after': {'ID': '891602', 'SpellID': '363726', 'EffectIndex': '0', 'EffectBonusCoefficient': '4.84'},
                   'before_verified': False, 'changes': []}
        npc = {**hammer, 'after': {**hammer['after'], 'ID': '9900', 'SpellID': '999999'},
               'source': {**hammer['source'], 'record_id': 9900}}
        spells = project_class_spell_changes([hammer, unknown, npc], is_class_spell=lambda sid, source: sid != 999999)
        self.assertEqual(set(spells), {427453, 363726})
        effect = spells[427453]['diffs']['spelleffect'][0]
        self.assertEqual(effect['id'], 1106904)
        self.assertEqual(effect['meta']['EffectIndex'], 0)
        self.assertEqual(effect['meta']['PushID'], 112185)
        self.assertEqual(effect['fields'][0]['before'], '6.9677400588989')
        self.assertEqual(spells[363726]['diffs']['spelleffect'][0]['fields'][0]['before'], '旧值未核实')

    def test_predecessor_decodes_with_its_own_build_layout_and_unknown_new_column(self):
        old_source = {'id': 1, 'region_id': 3, 'locale': 'enUS', 'table_name': 'SpellEffect',
                      'record_id': 1106904, 'push_id': 112180, 'build': 69875,
                      'data': [1106904, 427453, '6.9677400588989']}
        new_source = {**old_source, 'id': 2, 'push_id': 112185, 'build': 69933,
                      'data': [1106904, 427453, '10.451600074768', '0.5440000295639']}
        def schema(source):
            keys = [('ID', str(source['record_id'])), ('SpellID', '427453'), ('BonusCoefficientFromAP', '0')]
            if source['build'] == 69933:
                keys.append(('PvpMultiplier', '1'))
            return OrderedDict(keys)
        facts = build_hotfix_facts([new_source], schema_for=schema, previous_for=lambda source: old_source)
        self.assertTrue(facts[0]['before_verified'])
        self.assertEqual(facts[0]['changes'], [
            {'field': 'BonusCoefficientFromAP', 'before': '6.9677400588989', 'after': '10.451600074768'},
            {'field': 'PvpMultiplier', 'before': '旧值未核实', 'after': '0.5440000295639'},
        ])

    def test_identity_only_spell_effect_change_remains_a_class_fact(self):
        source = {'id': 4, 'table_name': 'SpellEffect', 'record_id': 1106904, 'push_id': 112185,
                  'build': 69933}
        fact = {'source': source, 'after': {'ID': '1106904', 'SpellID': '427453', 'EffectIndex': '1'},
                'before': {'ID': '1106904', 'SpellID': '363726', 'EffectIndex': '0'},
                'after_verified': True, 'before_verified': True,
                'changes': [{'field': 'SpellID', 'before': '363726', 'after': '427453'},
                            {'field': 'EffectIndex', 'before': '0', 'after': '1'}]}
        spells = project_class_spell_changes([fact], is_class_spell=lambda spell_id, row: spell_id == 427453)
        effect = spells[427453]['diffs']['spelleffect'][0]
        self.assertEqual(effect['id'], 1106904)
        self.assertEqual(effect['meta']['EffectIndex'], 1)
        self.assertEqual(effect['fields'], fact['changes'])
    def test_push_search_rejects_provider_ignoring_search_parameter(self):
        row = {'id': 1, 'push_id': 111885, 'locale': 'enUS', 'region_id': 3,
               'record_id': 10, 'table_name': 'SpellEffect'}
        def fetch(url, **kwargs):
            return {'filters': {'search': 'ignored'},
                    'hotfixes': {'data': [row], 'total': 1, 'current_page': 1,
                                 'last_page': 1, 'per_page': 25}}
        with self.assertRaises(HotfixSourceIncomplete):
            collect_hotfix_push_rows(fetch, lambda value: value, 111885,
                                     region_id=3, locale='enUS')
    def test_distinct_source_ids_for_same_record_are_never_silently_collapsed(self):
        rows = [
            {'id': sid, 'push_id': 111885, 'locale': 'enUS', 'region_id': 3,
             'table_name': 'SpellEffect', 'record_id': 10, 'status': 1, 'data': [10]}
            for sid in (91, 92)
        ]
        def fetch(url, **kwargs):
            return {'filters': {'search': parse_qs(urlsplit(url).query)['search'][0]},
                    'hotfixes': {'data': rows, 'total': 2, 'current_page': 1,
                                 'last_page': 1, 'per_page': 25}}
        with self.assertRaisesRegex(HotfixSourceIncomplete, 'duplicate record'):
            collect_hotfix_push_rows(fetch, lambda value: value, 111885,
                                     region_id=3, locale='enUS')
