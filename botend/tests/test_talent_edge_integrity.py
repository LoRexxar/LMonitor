"""Physical TraitNode edges survive choice-entry grouping and shared spells."""
from django.test import TestCase

from botend.models import WowTalentNodeMetadata, WowTalentVersion
from botend.wow.talents.metadata import TalentMetadataProvider
from botend.wow.talents.view_model import build_talent_view_model


class TalentEdgeIntegrityTests(TestCase):
    def test_distinct_nodes_and_choice_entry_parents_keep_authoritative_edges(self):
        version = WowTalentVersion.objects.create(
            key='edge-test', branch='retail', current_build='12.1.0.test',
            is_active=True, is_default_simulator=True,
        )

        def node(entry, physical, spell, row, col, parents=(), *, tree='class', subtree=0):
            WowTalentNodeMetadata.objects.create(
                talent_version=version, class_name='Warrior', spec_name='Protection',
                tree_type=tree, db2_subtree_id=subtree, source='db2_backfill',
                node_id=entry, talent_id=physical, spell_id=spell,
                name=f'Entry {entry}', row=row, column=col,
                parents_json=list(parents),
            )

        # One selectable physical node has two TraitNodeEntry IDs. The edge
        # points at the entry which is not necessarily the rendered option.
        node(112210, 90348, 1010, 900, 2400)
        node(112211, 90348, 1011, 900, 2400)
        node(112233, 90366, 1033, 1500, 2400, [112210])
        # Separate physical nodes may legitimately reference the same spell.
        node(126352, 102292, 147362, 2100, 1800)
        node(126466, 102402, 147362, 2100, 4200)
        node(126468, 102404, 378002, 2700, 4200, [126466])
        node(126470, 102406, 378003, 2700, 5400, [999999])
        # Two physical nodes at one slot share a spell; the second entry may
        # still be the authoritative parent of a later node.
        node(134271, 108722, 232893, 3304, 2404, [112210, 112928])
        node(112928, 91008, 232893, 3300, 2400)
        node(112852, 91010, 379001, 3900, 2400, [134271])
        # A shared spell in two hero subtrees is not a shared physical slot.
        node(117496, 94899, 452406, 4800, 8400, tree='hero', subtree=61)
        node(136620, 110117, 452406, 1800, 8400, tree='hero', subtree=62)

        nodes = TalentMetadataProvider(talent_version=version).get_full_tree_nodes(
            'Warrior', 'Protection',
        )
        physical_ids = {node['talent_id'] for node in nodes}
        self.assertEqual(len(nodes), 10)  # choice + same-slot duplicates collapse
        self.assertIn(102292, physical_ids)
        self.assertIn(102402, physical_ids)
        self.assertIn(94899, physical_ids)
        self.assertIn(110117, physical_ids)
        tree = next(
            tree for tree in build_talent_view_model(
                nodes, class_name='Warrior', spec_name='Protection',
            )['trees'] if tree['tree_type'] == 'class'
        )
        edges = {(path['parent_key'], path['child_key']) for path in tree['paths']}
        choice = next(node for node in nodes if node['talent_id'] == 90348)
        self.assertIn((f"class:{choice['node_id']}", 'class:112233'), edges)
        self.assertIn(('class:126466', 'class:126468'), edges)
        self.assertNotIn(('class:126352', 'class:126468'), edges)
        collapsed_parent = next(node for node in nodes if node['spell_id'] == 232893)
        self.assertIn((f"class:{collapsed_parent['node_id']}", 'class:112852'), edges)
        self.assertIn((f"class:{choice['node_id']}", f"class:{collapsed_parent['node_id']}"), edges)
        self.assertNotIn((f"class:{collapsed_parent['node_id']}", f"class:{collapsed_parent['node_id']}"), edges)
        self.assertFalse(any(child == 'class:126470' for _parent, child in edges))
