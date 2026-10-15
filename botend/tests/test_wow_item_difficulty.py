"""Strict DB2 graph contracts; synthetic rows are not observed game facts."""
import unittest
from botend.services.wow_item_difficulty import resolve_difficulty_bonus_facts

BUILD = '12.1.0.69587'

class DifficultyFactsTests(unittest.TestCase):
    def source(self, table, field, key):
        rows = self.rows.get((table, field, key), [])
        return {'build': BUILD, 'table': table, 'filter': {field: key}, 'total': len(rows), 'rows': rows}

    def setUp(self):
        self.rows = {
            ('ItemXBonusTree', 'ItemID', 42): [{'ID': 1, 'ItemID': 42, 'ItemBonusTreeID': 10}],
            ('ItemBonusTreeNode', 'ParentItemBonusTreeID', 10): [dict(ID=2, ParentItemBonusTreeID=10, ItemContext=1, ChildItemBonusTreeID=0, ChildItemBonusListID=100, ChildItemLevelSelectorID=0, ChildItemBonusListGroupID=0, IblGroupPointsModSetID=0, MinMythicPlusLevel=0, MaxMythicPlusLevel=0, ItemCreationContextGroupID=0, Flags=0)],
            ('ItemBonus', 'ParentItemBonusListID', 100): [{'ID': 3, 'ParentItemBonusListID': 100, 'Type': 3, 'Value_0': 3, 'OrderIndex': 0}],
        }

    def resolve(self):
        return resolve_difficulty_bonus_facts(42, 1, BUILD, tables=self.source)

    def test_real_item250243_three_contexts_resolve(self):
        import json
        from pathlib import Path
        data = json.loads((Path(__file__).parent / 'fixtures/wow_item_difficulty_item250243_69587.json').read_text())
        records = {(d['table'], *next(iter(d['filter'].items()))): d for d in data}
        def source(table, field, key):
            return records.get((table, field, key))
        expected = {1: (214, 3, [13437, 13442]),
                    2: (220, 3, [12698, 12769, 13438]),
                    23: (233, 4, [12699, 12777, 13439])}
        for ctx, (level, quality, bonuses) in expected.items():
            with self.subTest(context=ctx):
                r = resolve_difficulty_bonus_facts(250243, ctx, BUILD, tables=source)
                self.assertEqual(r['status'], 'resolved', r['unresolved'])
                self.assertEqual(r['proven_bonus_ids'], bonuses)
                self.assertEqual(r['bonus_ids'], bonuses)
                self.assertEqual(r['quality'], quality)
                self.assertEqual(r['item_level'], level)
                self.assertEqual(r['curve_ids'], [88583])
                self.assertNotIn(13577, r['proven_bonus_ids'])
                self.assertNotIn(13578, r['proven_bonus_ids'])

    def real_records(self):
        import json
        from pathlib import Path
        data = json.loads((Path(__file__).parent / 'fixtures/wow_item_difficulty_item250243_69587.json').read_text())
        return {(d['table'], *next(iter(d['filter'].items()))): d for d in data}

    def test_real_group_no_cost_expansion_not_selected_and_not_min_rank(self):
        records = self.real_records()
        entries = records[('ItemBonusListGroupEntry', 'ItemBonusListGroupID', 608)]['rows']
        # Move the no-cost acquisition rank above other ranks. Its identity,
        # accessibility and cost, not sorted/minimum rank, must select it.
        entries[0]['SequenceValue'] = 10
        r = resolve_difficulty_bonus_facts(250243, 2, BUILD,
            tables=lambda t, f, k: records.get((t, f, k)))
        self.assertEqual(r['status'], 'resolved')
        self.assertEqual(r['initial_groups'][0]['sequence_value'], 10)
        self.assertIn(12769, r['bonus_ids'])
        self.assertNotIn(12775, r['bonus_ids'])
        self.assertNotIn(12776, r['bonus_ids'])

    def test_lower_priority_value_wins_not_largest_scaling_config_id(self):
        records = self.real_records()
        envelope = records[('ItemBonus', 'ParentItemBonusListID', 13442)]
        envelope['rows'].append(dict(ID=900001, ParentItemBonusListID=13442,
            Type=49, Value_0=211, Value_1=5, OrderIndex=2))
        envelope['total'] += 1
        r = resolve_difficulty_bonus_facts(250243, 1, BUILD,
            tables=lambda t, f, k: records.get((t, f, k)))
        self.assertEqual(r['status'], 'resolved')
        self.assertEqual(r['item_level'], 214)

    def test_missing_real_offset_points_blocks_only_numeric_resolution(self):
        records = self.real_records()
        records.pop(('CurvePoint', 'CurveID', 88583))
        r = resolve_difficulty_bonus_facts(250243, 1, BUILD,
            tables=lambda t, f, k: records.get((t, f, k)))
        self.assertEqual(r['status'], 'unresolved')
        self.assertEqual(r['proven_quality'], 3)
        self.assertIsNone(r['item_level'])
        self.assertTrue(any(x['table'] == 'CurvePoint' and x['key'] == 88583 for x in r['unresolved']))

    def test_quality_is_proven_without_guessing_level(self):
        r = self.resolve()
        self.assertEqual(r['proven_quality'], 3)
        self.assertIsNone(r['item_level'])
        self.assertEqual(r['status'], 'unresolved')

    def test_unknown_creation_gate_is_not_applied(self):
        self.rows[('ItemBonusTreeNode', 'ParentItemBonusTreeID', 10)][0]['ItemCreationContextGroupID'] = 6
        r = self.resolve()
        self.assertEqual(r['proven_bonus_ids'], [])
        self.assertTrue(any(x['key'] == 6 and x['field'] == 'ItemCreationContextGroupID' for x in r['diagnostics']))

    def test_points_not_minimum_rank(self):
        node = self.rows[('ItemBonusTreeNode', 'ParentItemBonusTreeID', 10)][0]
        node.update(ChildItemBonusListID=0, ChildItemBonusListGroupID=608, IblGroupPointsModSetID=2963)
        r = self.resolve()
        self.assertEqual(r['proven_bonus_ids'], [])
        self.assertTrue(any(x['key'] == 2963 for x in r['unresolved']))

    def test_cross_build_is_rejected(self):
        def wrong(t, f, k):
            d = self.source(t, f, k); d['build'] = '12.1.0.1'; return d
        r = resolve_difficulty_bonus_facts(42, 1, BUILD, tables=wrong)
        self.assertEqual(r['status'], 'unresolved')
        self.assertEqual(r['proven_bonus_ids'], [])

    def test_context_mismatch_is_excluded(self):
        self.rows[('ItemBonusTreeNode', 'ParentItemBonusTreeID', 10)][0]['ItemContext'] = 2
        self.assertEqual(self.resolve()['proven_bonus_ids'], [])

    def test_type49_requires_scaling_config_not_same_number_curve(self):
        self.rows[('ItemBonus', 'ParentItemBonusListID', 100)] = [{'ID': 3, 'ParentItemBonusListID': 100, 'Type': 49, 'Value_0': 183, 'Value_1': 0, 'OrderIndex': 0}]
        r = self.resolve()
        self.assertIsNone(r['item_level'])
        self.assertTrue(any(x['key'] == 183 and x['table'] == 'ItemScalingConfig' for x in r['unresolved']))
