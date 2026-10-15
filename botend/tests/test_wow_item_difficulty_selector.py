"""Contracts on exact public DB2 rows, not mocked selector data."""
import json
import unittest
from pathlib import Path
from botend.services.wow_item_difficulty_selector import selector_bonus_ids, default_group_entry, SelectorUnresolved

class PublicSelectorContract(unittest.TestCase):
    def test_public_selector_and_order_contract(self):
        fixture=json.loads((Path(__file__).parent/'fixtures/wow_item_selector_public_69587.json').read_text())
        def read(table,field,key):
            found=[e for e in fixture['envelopes'] if e['table']==table and e['filter']=={field:key}]
            self.assertEqual(len(found),1,(table,field,key))
            return found[0]['rows']
        for item,selector,expected in [(128032,71,[1472]),(167556,814,[1464])]:
            sparse=read('ItemSparse','ID',item)[0]
            self.assertEqual(selector_bonus_ids(selector,sparse,read_table=read)['bonus_ids'],expected)
        from botend.services.wow_item_difficulty import resolve_difficulty_bonus_facts
        def envelopes(table, field, key):
            return next((e for e in fixture['envelopes'] if e['table'] == table and e['filter'] == ({} if field is None else {field: key})), None)
        result = resolve_difficulty_bonus_facts(128032, 4, fixture['build'], tables=envelopes)
        self.assertEqual(result['status'], 'resolved', result['unresolved'])
        self.assertEqual((result['item_level'], result['quality']), (16, 4))
        self.assertIn(1472, result['bonus_ids'])
        rows=read('ItemBonusListGroupEntry','ItemBonusListGroupID',331)
        # CSV ID order is not native unordered_multimap iteration order.
        with self.assertRaisesRegex(SelectorUnresolved,'order'):
            default_group_entry(rows,sequence_level=0)
        # With an externally established native traversal order, choose FIRST,
        # not minimum sequence. Real entries, reordered, no fabricated row.
        ordered=list(reversed(rows))
        self.assertEqual(default_group_entry(ordered,sequence_level=0,native_order_proven=True),ordered[0])
