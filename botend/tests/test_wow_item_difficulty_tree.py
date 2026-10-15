"""Native whole-tree applicability: public rows plus labelled synthetic edges."""
import copy
import json
from pathlib import Path
import unittest

from botend.services.wow_item_difficulty_tree import TreeUnresolved, tree_applies


class NativeTreeApplicability(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads((Path(__file__).parent / 'fixtures/wow_item_tree_public_69587.json').read_text())

    def setUp(self):
        self.envelopes = copy.deepcopy(self.fixture['envelopes'])
        self.reads = []

    def read(self, table, field, key):
        self.reads.append((table, field, key))
        entries = [e for e in self.envelopes if e['table'] == table and e['filter'] == {field: key}]
        if not entries:
            return None
        self.assertEqual(len(entries), 1)
        return entries[0]['rows']

    def apply(self, tree, item=65172, context=2):
        return tree_applies(tree, item, context, read_table=self.read)

    def test_real_wand_random_trees_rejected_as_whole_trees(self):
        for tree in (5902, 3753):
            with self.subTest(tree=tree):
                result = self.apply(tree)
                self.assertFalse(result['applies'])
                self.assertEqual(result['reason'], 'multiple_matching_nodes')
        for tree in (5903, 3754, 3755):
            with self.subTest(tree=tree):
                self.assertEqual(self.apply(tree)['reason'], 'inventory_mask_mismatch')
        for tree in (4865, 3953, 3043, 3756, 51):
            with self.subTest(tree=tree):
                self.assertTrue(self.apply(tree)['applies'])
        self.assertIn(('ItemBonusTreeNode', 'ParentItemBonusTreeID', 3753), self.reads)

    def test_real_modern_flags4_tree_accepts_multiple_nodes(self):
        self.assertTrue(self.apply(5743, item=250243)['applies'])
        self.assertEqual(self.apply(5743, item=250243)['reason'], 'flags_allow_multiple')
        self.assertNotIn(('ItemSparse', 'ID', 250243), self.reads)

    def synthetic(self, *, flags=0, mask=0, sparse_flags1=0, sparse_flags3=0, nodes=None):
        """Explicit synthetic mutation; never presented as public source evidence."""
        rows = {('ItemBonusTree', 'ID', 1): [dict(ID=1, Flags=flags, InventoryTypeSlotMask=mask)],
                ('ItemSparse', 'ID', 2): [dict(ID=2, InventoryType=26, Flags_1=sparse_flags1, Flags_3=sparse_flags3)],
                ('ItemBonusTreeNode', 'ParentItemBonusTreeID', 1): nodes if nodes is not None else []}
        return rows

    def test_synthetic_native_item_flags_and_gate_order(self):
        for tree_flag, field, bit in ((8, 'sparse_flags1', 0x200), (16, 'sparse_flags1', 0x200),
                                     (32, 'sparse_flags3', 0x02000000), (64, 'sparse_flags3', 0x02000000)):
            for present in (False, True):
                with self.subTest(tree_flag=tree_flag, present=present):
                    rows = self.synthetic(flags=tree_flag | 4, **{field: bit if present else 0})
                    result = tree_applies(1, 2, 2, read_table=lambda *key: rows.get(key))
                    self.assertEqual(result['applies'], present if tree_flag in (8, 32) else not present)
        rows = self.synthetic(flags=4, mask=1)
        self.assertFalse(tree_applies(1, 2, 2, read_table=lambda *key: rows.get(key))['applies'])

    def test_synthetic_count_matches_native_minimum_and_context_only(self):
        base = dict(ItemContext=0, MinMythicPlusLevel=0, MaxMythicPlusLevel=1,
                    Flags=99, ItemCreationContextGroupID=99)
        for nodes, expected in (([], True), ([base], True), ([base, base], False),
                                ([base, {**base, 'MinMythicPlusLevel': 1}], True),
                                ([base, {**base, 'MinMythicPlusLevel': -1}], False),
                                ([base, {**base, 'ItemContext': 3}], True)):
            with self.subTest(nodes=nodes):
                rows = self.synthetic(flags=0x80000003, nodes=nodes)
                self.assertEqual(tree_applies(1, 2, 2, read_table=lambda *key: rows.get(key))['applies'], expected)
        # Complete known-absent metadata row uses native no-flags fallback.
        rows = self.synthetic(nodes=[base, base])
        rows[('ItemBonusTree', 'ID', 1)] = []
        self.assertFalse(tree_applies(1, 2, 2, read_table=lambda *key: rows.get(key))['applies'])

    def test_synthetic_missing_and_malformed_inputs_fail_closed(self):
        for table in ('ItemBonusTree', 'ItemBonusTreeNode'):
            rows = self.synthetic()
            del rows[(table, 'ID' if table == 'ItemBonusTree' else 'ParentItemBonusTreeID', 1)]
            with self.subTest(table=table), self.assertRaises(TreeUnresolved):
                tree_applies(1, 2, 2, read_table=lambda *key: rows.get(key))
        for field, value in (('flags', True), ('flags', -1), ('flags', '4'), ('mask', True), ('mask', -1)):
            rows = self.synthetic(**{field: value})
            with self.subTest(field=field, value=value), self.assertRaises(TreeUnresolved):
                tree_applies(1, 2, 2, read_table=lambda *key: rows.get(key))
        for field in ('InventoryType', 'Flags_1', 'Flags_3'):
            rows = self.synthetic(flags=0x28, mask=1 << 26, sparse_flags1=0x200)
            rows[('ItemSparse', 'ID', 2)][0][field] = True
            with self.subTest(field=field), self.assertRaises(TreeUnresolved):
                tree_applies(1, 2, 2, read_table=lambda *key: rows.get(key))
        for field in ('MinMythicPlusLevel', 'ItemContext'):
            rows = self.synthetic(nodes=[dict(MinMythicPlusLevel=0, ItemContext=0, **{})])
            rows[('ItemBonusTreeNode', 'ParentItemBonusTreeID', 1)][0][field] = True
            with self.subTest(field=field), self.assertRaises(TreeUnresolved):
                tree_applies(1, 2, 2, read_table=lambda *key: rows.get(key))
