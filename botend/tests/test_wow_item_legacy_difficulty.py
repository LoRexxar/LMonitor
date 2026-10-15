"""Legacy base contracts; mutations are synthetic, not observed loot."""
import unittest
from botend.services.wow_item_difficulty import resolve_difficulty_bonus_facts

BUILD = '12.1.0.69587'

class LegacyDifficultyTests(unittest.TestCase):
    def setUp(self):
        self.sparse = dict(ID=123, ItemLevel=37, OverallQualityID=4, Flags_3=0,
            PlayerLevelToItemLevelCurveID=0, ItemLevelOffsetCurveID=0,
            ItemLevelOffsetItemLevel=0, ItemSquishEraID=2, ContentTuningID=0,
            RequiredLevel=30, InventoryType=1)
        self.rows = {('ItemSparse', 'ID', 123): [self.sparse],
                     ('ItemXBonusTree', 'ItemID', 123): [],
                     ('ItemSquishEra', None, None): [dict(ID=1, Patch=0, CurveID=0, Flags=0),
                                                   dict(ID=2, Patch=120000, CurveID=1, Flags=2)]}
    def source(self, table, field, key):
        rows = self.rows.get((table, field, key), [])
        return dict(build=BUILD, table=table, filter={} if field is None else {field:key},
            total=len(rows), rows=rows, provider='wago',
            url=f'https://wago.tools/api/db2-find/{table}?build={BUILD}&locale=enUS&filter[{field}]=exact:{key}')
    def resolve(self, context=0):
        return resolve_difficulty_bonus_facts(123, context, BUILD, tables=self.source)
    def tree(self, effects):
        self.rows['ItemXBonusTree','ItemID',123] = [dict(ID=1, ItemID=123, ItemBonusTreeID=10)]
        self.rows['ItemBonusTreeNode','ParentItemBonusTreeID',10] = [dict(ID=2,
            ParentItemBonusTreeID=10, ItemContext=0, ChildItemBonusTreeID=0,
            ChildItemBonusListID=100, ChildItemLevelSelectorID=0,
            ChildItemBonusListGroupID=0, IblGroupPointsModSetID=0,
            MinMythicPlusLevel=0, MaxMythicPlusLevel=0, ItemCreationContextGroupID=0, Flags=0)]
        self.rows['ItemBonus','ParentItemBonusListID',100] = [dict(ID=i+3,
            ParentItemBonusListID=100, Type=t, Value_0=v, Value_1=p, OrderIndex=i)
            for i,(t,v,p) in enumerate(effects)]
    def test_empty_assignment_equipment_uses_base_quality_without_override(self):
        r=self.resolve(); self.assertEqual(r['status'],'resolved',r['unresolved'])
        self.assertEqual((r['item_level'],r['quality'],r['bonus_ids']),(37,4,[]))
        self.assertEqual(r['initial_drop_kind'],'fixed_base')
    def test_safe_tree_and_type1_addition_inherit_quality(self):
        self.tree([(1,6,0),(6,1234,0)])
        r=self.resolve(3);self.assertEqual((r['item_level'],r['quality']),(43,4),r['unresolved'])
    def test_type42_priority_replaces_base_before_type1_and_type3(self):
        self.tree([(42,55,9),(42,48,1),(1,6,0),(3,3,0)])
        r=self.resolve(6);self.assertEqual((r['item_level'],r['quality']),(54,3),r['unresolved'])
    def test_unknown_scaling_gates_never_use_base(self):
        for field in ('PlayerLevelToItemLevelCurveID','ItemLevelOffsetCurveID','ContentTuningID'):
            self.setUp();self.sparse[field]=1
            self.assertEqual(self.resolve()['status'],'unresolved')
        for kind in (48,51,53):
            self.setUp();self.tree([(kind,1,0)])
            self.assertEqual(self.resolve()['status'],'unresolved')
    def test_context_zero_is_typed_not_bool(self):
        self.assertEqual(self.resolve(0)['status'],'resolved')
        for context in (False,True,-1):
            with self.assertRaises(ValueError):self.resolve(context)
    def test_equal_priority_base_conflict_rejected(self):
        self.tree([(42,55,1),(42,48,1)])
        self.assertEqual(self.resolve()['status'],'unresolved')
    def test_static_type49_ignore_squish_inherits_base_quality(self):
        self.tree([(49,8,0)])
        self.rows['ItemScalingConfig','ID',8] = [dict(ID=8,Flags=1,
            ItemOffsetCurveID=9,ItemLevel=10,RequiredLevel=1,ItemSquishEraID=0)]
        self.rows['ItemOffsetCurve','ID',9] = [dict(ID=9,CurveID=10,Offset=5)]
        self.rows['Curve','ID',10] = [dict(ID=10,Type=0)]
        self.rows['CurvePoint','CurveID',10] = [dict(ID=11,CurveID=10,OrderIndex=0,Pos_0=10,Pos_1=40)]
        r=self.resolve(3)
        self.assertEqual((r['item_level'],r['quality']),(45,4),r['unresolved'])

    def test_real_bulk_legacy_samples_replay(self):
        import json
        from pathlib import Path
        data=json.loads((Path(__file__).parent/'fixtures/wow_item_legacy_difficulty_69587.json').read_text())
        self.assertEqual(len(data['samples']),5)
        for sample in data['samples']:
            f=sample['facts'];records={(d['table'],d['field'],d['key']):d for d in f['evidence']}
            with self.subTest(item=f['item_id'],context=f['item_context']):
                r=resolve_difficulty_bonus_facts(f['item_id'],f['item_context'],BUILD,
                    tables=lambda t,field,key:records.get((t,field,key)))
                self.assertEqual(r['status'],'resolved',r['unresolved'])
                self.assertEqual((r['item_level'],r['quality'],r['bonus_ids']),
                    (f['item_level'],f['quality'],f['bonus_ids']))

    def test_missing_squish_proof_rejected(self):
        self.rows.pop(('ItemSquishEra',None,None))
        self.assertEqual(self.resolve()['status'],'unresolved')
