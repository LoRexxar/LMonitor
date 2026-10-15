"""Native legacy edge contracts; synthetic mutations are not observed loot."""
import unittest
from botend.tests import test_wow_item_legacy_difficulty as legacy


class LegacyNativeEdgeTests(unittest.TestCase):
    rows: dict
    sparse: dict
    setUp = legacy.LegacyDifficultyTests.setUp
    source = legacy.LegacyDifficultyTests.source
    tree = legacy.LegacyDifficultyTests.tree
    resolve = legacy.LegacyDifficultyTests.resolve

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
