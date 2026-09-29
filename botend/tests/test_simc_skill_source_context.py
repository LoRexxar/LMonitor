"""Source provenance is explanatory; it must never alter cast arithmetic."""
import copy
import unittest

from botend.services.simc_skill_source_context import attach_skill_damage_source_context


def action(token, spell_id, name, parent='', **extra):
    return dict(token=token, spell_id=spell_id, display_name=name,
                parent_token=parent, reporting_root_token=token,
                product={'final_normalized_damage': 120}, **extra)


class SkillSourceContextTests(unittest.TestCase):
    def test_distinct_proc_paths_and_multilevel_sources_preserve_all_facts(self):
        roots = [('arbitrary_a', 335097, '碎甲猛击'),
                 ('arbitrary_b', 5308, '斩杀'), ('arbitrary_c', 435222, '雷霆轰击')]
        rows = []
        for index, (token, spell_id, name) in enumerate(roots):
            rows.extend([action(token, spell_id, name, token),
                         action(f'proc_{index}', 435791, '闪电打击', token),
                         action(f'child_{index}', 460670, '贯地电流', f'proc_{index}')])
        # Repeated talent variants are lookup evidence, not duplicate output rows.
        rows.append(dict(copy.deepcopy(rows[1]), variant={'talent_id': 74914}))
        actor = {'actions': rows}
        before = copy.deepcopy(actor)
        self.assertIs(attach_skill_damage_source_context(actor), actor)
        self.assertEqual(len(actor['actions']), len(before['actions']))
        for old, row in zip(before['actions'], actor['actions']):
            self.assertEqual({k: v for k, v in row.items() if k != 'source_context'}, old)
        for index, (token, spell_id, name) in enumerate(roots):
            parent = rows[index * 3 + 1]['source_context']
            child = rows[index * 3 + 2]['source_context']
            self.assertEqual(parent['relation'], 'reporting_parent')
            self.assertEqual(parent['status'], 'resolved')
            self.assertEqual(parent['chain'][0]['spell_id'], spell_id)
            self.assertEqual(child['display_label'], f'报告来源：{name} → 闪电打击')
            self.assertEqual([node['token'] for node in child['chain']], [token, f'proc_{index}'])
            self.assertNotIn('触发', child['display_label'])
        self.assertEqual(len({rows[i]['source_context']['display_label'] for i in (2, 5, 8)}), 3)
        self.assertNotIn('source_context', rows[0])  # self-parent is a terminal, not recursion
        first = copy.deepcopy(actor)
        attach_skill_damage_source_context(actor)
        self.assertEqual(actor, first)

    def test_missing_ambiguous_and_cyclic_parents_fail_honestly(self):
        rows = [action('orphan', 1, '孤立伤害', 'unknown_execute'),
                action('ambiguous_child', 2, '歧义伤害', 'shared'),
                action('shared', 3, '同名甲'), action('shared', 4, '同名乙'),
                action('cycle_a', 5, '循环甲', 'cycle_b'),
                action('cycle_b', 6, '循环乙', 'cycle_a')]
        attach_skill_damage_source_context({'actions': rows})
        missing = rows[0]['source_context']
        self.assertEqual(missing['status'], 'unresolved')
        self.assertEqual(missing['chain'][0]['reason'], 'missing_parent')
        self.assertIsNone(missing['chain'][0]['spell_id'])
        self.assertIn('unknown_execute', missing['display_label'])
        self.assertIn('未解析', missing['display_label'])
        self.assertNotIn('斩杀', missing['display_label'])
        self.assertEqual(rows[1]['source_context']['chain'][0]['reason'], 'ambiguous_parent')
        self.assertEqual(rows[4]['source_context']['status'], 'partial')
        self.assertIn('循环', rows[4]['source_context']['display_label'])
        # Parent resolution stays within one actor; do not borrow another actor's facts.
        attach_skill_damage_source_context({'actions': [action('unknown_execute', 9, '另一职业')]})
        attach_skill_damage_source_context({'actions': [rows[0]]})
        self.assertEqual(rows[0]['source_context'], missing)

    def test_conflicting_ancestry_and_same_named_sources_are_not_guessed(self):
        rows = [action('one', 1, '同名'), action('two', 1, '同名'),
                action('proc_one', 2, '伤害', 'one'), action('proc_two', 2, '伤害', 'two'),
                action('conflict', 3, '相同', 'one'), action('conflict', 3, '相同', 'two'),
                action('child', 4, '子技能', 'conflict')]
        attach_skill_damage_source_context({'actions': rows})
        self.assertNotEqual(rows[2]['source_context']['display_label'], rows[3]['source_context']['display_label'])
        self.assertEqual(rows[-1]['source_context']['chain'][0]['reason'], 'ambiguous_parent')


if __name__ == '__main__':
    unittest.main()
