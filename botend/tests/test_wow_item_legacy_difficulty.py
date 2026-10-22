"""Legacy difficulty resolution: base facts and native edge gates; synthetic mutations are not observed loot."""
import unittest
from botend.services.wow_item_difficulty import resolve_difficulty_bonus_facts

# Legacy base contracts; mutations are synthetic, not observed loot.

BUILD = '12.1.0.69587'

class LegacyDifficultyFixture:
    """Shared synthetic DB2 source; no inherited test methods."""
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


class LegacyDifficultyTests(LegacyDifficultyFixture, unittest.TestCase):
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


# Native legacy edge contracts; synthetic mutations are not observed loot.


class LegacyNativeEdgeTests(LegacyDifficultyFixture, unittest.TestCase):
    rows: dict
    sparse: dict

    def test_complete_empty_assigned_tree_is_terminal(self):
        self.tree([])
        self.rows['ItemBonusTreeNode', 'ParentItemBonusTreeID', 10] = []
        result = self.resolve()
        self.assertEqual((result['status'], result['item_level'], result['quality']),
                         ('resolved', 37, 4), result['unresolved'])

    def test_missing_assigned_tree_is_not_empty(self):
        self.tree([])
        source = self.source
        from botend.services.wow_item_difficulty import resolve_difficulty_bonus_facts
        result = resolve_difficulty_bonus_facts(123, 0, '12.1.0.69587', tables=lambda t, f, k:
            None if t == 'ItemBonusTreeNode' else source(t, f, k))
        self.assertEqual(result['status'], 'unresolved')

    def test_required_level_curve_does_not_change_static_level_quality(self):
        self.tree([(27, 999, 0)])
        result = self.resolve()
        self.assertEqual((result['item_level'], result['quality']), (37, 4), result['unresolved'])

    def test_direct_offset_curve_has_explicit_input_not_player_level(self):
        self.tree([(48, 9, 10), (1, 200, 0)])
        self.rows['ItemBonus', 'ParentItemBonusListID', 100][0]['Value_3'] = 1
        self.rows['Curve', 'ID', 9] = [dict(ID=9, Type=0)]
        self.rows['CurvePoint', 'CurveID', 9] = [dict(ID=8, CurveID=9,
            OrderIndex=0, Pos_0=10, Pos_1=45)]
        result = self.resolve()
        self.assertEqual((result['item_level'], result['quality']), (45, 4), result['unresolved'])
        self.assertEqual(result['level_evaluation']['input'], 10)

    def test_direct_offset_applies_sparse_era_squish(self):
        self.tree([(48, 9, 10)])
        self.rows['ItemBonus', 'ParentItemBonusListID', 100][0]['Value_3'] = 1
        self.rows['Curve', 'ID', 9] = [dict(ID=9, Type=0)]
        self.rows['CurvePoint', 'CurveID', 9] = [dict(ID=8, CurveID=9,
            OrderIndex=0, Pos_0=10, Pos_1=45)]
        self.sparse['ItemSquishEraID'] = 0
        self.rows['Curve', 'ID', 1] = [dict(ID=1, Type=0)]
        self.rows['CurvePoint', 'CurveID', 1] = [dict(ID=7, CurveID=1,
            OrderIndex=0, Pos_0=45, Pos_1=12)]
        result = self.resolve()
        self.assertEqual(result['item_level'], 12, result['unresolved'])
        self.assertEqual(result['base_rule']['squish_evaluations'][0]['input'], 45)

    def test_equal_priority_different_player_curves_are_not_equivalent(self):
        self.tree([(13, 0, 1), (13, 0, 1)])
        effects = self.rows['ItemBonus', 'ParentItemBonusListID', 100]
        effects[0].update(Value_2=0, Value_3=0)
        effects[1].update(Value_2=0, Value_3=9)
        result = self.resolve()
        self.assertEqual(result['status'], 'unresolved')
        self.assertTrue(any('equal-priority' in d['reason'] for d in result['unresolved']))

    def test_fixed_scaling_zero_curve_is_native_static_not_missing(self):
        self.tree([(13, 0, 1)])
        effect = self.rows['ItemBonus', 'ParentItemBonusListID', 100][0]
        effect.update(Value_2=0, Value_3=0)
        result = self.resolve()
        self.assertEqual((result['item_level'], result['quality']), (37, 4), result['unresolved'])

    def test_fixed_scaling_nonzero_curve_requires_creation_level(self):
        self.tree([(13, 0, 1)])
        self.rows['ItemBonus', 'ParentItemBonusListID', 100][0].update(Value_2=0, Value_3=9)
        result = self.resolve()
        self.assertEqual(result['status'], 'unresolved')
        self.assertTrue(any('creation/player level' in d['reason'] for d in result['unresolved']))
