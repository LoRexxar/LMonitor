"""Captured exact-build enUS DB2 responses, never a fabricated drop result."""
from copy import deepcopy
import json
from pathlib import Path
from django.test import SimpleTestCase
from botend.services.wow_item_difficulty import resolve_difficulty_bonus_facts

BUILD = '12.1.0.69587'
FIXTURES = Path(__file__).parent / 'fixtures'


def envelopes():
    return json.loads((FIXTURES / 'wow_item_fixed_cosmetic_258045_69587.json').read_text())


def resolve(context=1, records=None, item_id=258045):
    records = envelopes() if records is None else records
    lookup = {(d['table'], *next(iter(d['filter'].items()))): d for d in records}
    return resolve_difficulty_bonus_facts(item_id, context, BUILD,
        tables=lambda t, f, k: lookup.get((t, f, k)))


class FixedCosmeticRuleTests(SimpleTestCase):
    def test_real_empty_tree_is_fixed_in_all_three_contexts(self):
        for context in (1, 2, 23):
            with self.subTest(context=context):
                facts = resolve(context)
                self.assertEqual(facts['status'], 'resolved')
                self.assertEqual(facts['initial_drop_kind'], 'fixed_cosmetic')
                self.assertEqual((facts['item_level'], facts['quality'], facts['bonus_ids']), (1, 3, []))
                self.assertEqual(facts['unresolved'], [])
                self.assertEqual(facts['base_rule']['item_sparse']['RequiredLevel'], 1)
                sparse = next(d for d in facts['evidence'] if d['table'] == 'ItemSparse')
                self.assertIn('locale=enUS', sparse['url'])
                self.assertEqual(sparse['rows'][0]['Flags_3'], 65536)
                self.assertEqual(len(sparse['source_sha256']), 64)

    def test_typed_flags_curves_and_base_values_fail_closed(self):
        invalid = [('Flags_3', v) for v in (0, True, False, 65536.0, '65536')]
        for f in ('PlayerLevelToItemLevelCurveID', 'ItemLevelOffsetCurveID', 'ItemLevelOffsetItemLevel'):
            invalid += [(f, v) for v in (None, False, 0.0, '0', 1)]
        invalid += [('ItemLevel', v) for v in (None, True, 1.0, 0)]
        invalid += [('OverallQualityID', v) for v in (None, True, 3.0, 9)]
        invalid += [('RequiredLevel', v) for v in (None, False, 1.0, -1)]
        for field, value in invalid:
            with self.subTest(field=field, value=value):
                records = envelopes()
                sparse = next(d for d in records if d['table'] == 'ItemSparse')
                if value is None:
                    sparse['rows'][0].pop(field)
                else:
                    sparse['rows'][0][field] = value
                self.assertEqual(resolve(records=records)['status'], 'unresolved')

    def test_empty_assignment_missing_or_partial_sparse_is_not_fixed(self):
        for table in ('ItemSparse', 'ItemXBonusTree'):
            for alteration in ('missing', 'partial', 'crossbuild'):
                with self.subTest(table=table, alteration=alteration):
                    records = envelopes()
                    target = next(d for d in records if d['table'] == table)
                    if alteration == 'missing':
                        records.remove(target)
                    elif alteration == 'partial':
                        target['total'] += 1
                    else:
                        target['build'] = '12.1.5.99999'
                    self.assertEqual(resolve(records=records)['status'], 'unresolved')

    def test_sparse_english_identity_and_generic_not_item_id_special_case(self):
        for locale in ('zhCN', ''):
            records = envelopes()
            sparse = next(d for d in records if d['table'] == 'ItemSparse')
            sparse['url'] = sparse['url'].replace('locale=enUS', 'locale=' + locale)
            self.assertEqual(resolve(records=records)['status'], 'unresolved')
        # An explicitly mutated identity tests generic logic, not live evidence for this ID.
        records = deepcopy(envelopes())
        for d in records:
            field = next(iter(d['filter']))
            d['filter'][field] = 12345
            d['url'] = d['url'].replace('258045', '12345')
            for row in d['rows']:
                row[field] = 12345
        self.assertEqual(resolve(records=records, item_id=12345)['status'], 'resolved')
