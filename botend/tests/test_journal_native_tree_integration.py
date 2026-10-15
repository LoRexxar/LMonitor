"""Whole-tree applicability must affect the shared resolver, not just a helper."""
import json
import unittest
from pathlib import Path
from botend.services.wow_item_difficulty import resolve_difficulty_bonus_facts

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
