"""Native difficulty resolver contracts over complete frozen DB2 envelopes; keep bulk, tree, and selector test classes."""
import json
import unittest
from copy import deepcopy
from pathlib import Path
from botend.services.wow_item_difficulty import resolve_difficulty_bonus_facts
from botend.services.wow_item_difficulty_selector import selector_bonus_ids, default_group_entry, SelectorUnresolved


class BulkCosmeticSourceTests(unittest.TestCase):
    def setUp(self):
        self.sparse=json.loads((Path(__file__).parent/'fixtures/wow_item_cosmetic_bulk_sparse_69587.json').read_text())
    def resolve(self):
        def source(table,field,key):
            if table=='ItemSparse':return self.sparse
            return dict(provider='wago',build=self.sparse['build'],table=table,filter={field:key},rows=[],total=0,
                url=f'https://wago.tools/api/db2-find/{table}?build={self.sparse["build"]}&locale=enUS&filter[{field}]=exact:{key}')
        return resolve_difficulty_bonus_facts(258045,1,self.sparse['build'],tables=source)
    def test_actual_complete_bulk_cosmetic_has_same_fixed_fact(self):
        r=self.resolve()
        self.assertEqual(r['status'],'resolved',r['unresolved'])
        self.assertEqual((r['item_level'],r['quality'],r['bonus_ids']),(1,3,[]))
    def test_bulk_identity_or_completeness_remains_required(self):
        for key,value in [('url',self.sparse['url'].replace('enUS','zhCN')),('file_sha256','nohash'),('source_total',0)]:
            original=deepcopy(self.sparse);self.sparse[key]=value
            with self.subTest(field=key):self.assertEqual(self.resolve()['status'],'unresolved')
            self.sparse=original


class RealWandTreeIntegration(unittest.TestCase):
    def test_candidate_random_trees_are_not_all_initial_bonuses(self):
        p = Path(__file__).parent / 'fixtures'
        rule = json.loads((p / 'wow_item_wand_difficulty_graph_69587.json').read_text())
        tree = json.loads((p / 'wow_item_tree_public_69587.json').read_text())
        records = {(e['table'], e['field'], e['key']): e for e in rule['evidence']}
        for e in tree['envelopes']:
            f, k = next(iter(e['filter'].items()))
            records[e['table'], f, k] = e
        r = resolve_difficulty_bonus_facts(65172, 2, rule['build'], tables=lambda t,f,k: records.get((t,f,k)))
        self.assertIn(8998, r['proven_bonus_ids'])
        self.assertTrue({40, 41, 42, 43}.isdisjoint(r['proven_bonus_ids']))
        self.assertTrue(any(d['table']=='ItemBonusTree' for d in r['diagnostics']))


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
