"""Complete exact-build bulk and exact-find facts must resolve identically."""
import json
import unittest
from copy import deepcopy
from pathlib import Path
from botend.services.wow_item_difficulty import resolve_difficulty_bonus_facts

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
