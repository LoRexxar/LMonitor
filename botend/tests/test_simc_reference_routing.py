"""分流入口必须保留调用顺序、接管语义和无法翻译的条件。"""

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from botend.services.simc_apl.readable import readable_apl


class AplRoutingTests(SimpleTestCase):
    symbols = {('buff', 'metamorphosis'): {'name': '虚空变形'},
               ('action', 'call_action_list'): {'name': '不应显示的控制技能'}}

    def groups(self, source, **kwargs):
        return {g['name']: g for g in readable_apl(source, self.symbols, **kwargs)['groups']}

    def test_live_devourer_dispatch_keeps_implicit_else_and_upstream(self):
        source = '\n'.join([
            'actions=call_action_list,name=variables',
            'actions+=/call_action_list,name=cooldowns',
            'actions+=/run_action_list,name=voidscarred,if=hero_tree.voidscarred',
            'actions.variables=variable,name=x,value=1',
            'actions.cooldowns=potion',
            'actions.voidscarred=run_action_list,name=voidscarred_meta,if=buff.metamorphosis.up',
            'actions.voidscarred+=/run_action_list,name=voidscarred_out',
            'actions.voidscarred_meta=hit',
            'actions.voidscarred_out=hit',
        ])
        groups = self.groups(source, hero_id=126)
        branch = groups['voidscarred']
        self.assertIn('具有虚空变形效果', branch['rows'][0]['entry_text'])
        self.assertEqual(branch['rows'][1]['entry_guards'][0]['text'], '没有虚空变形效果')
        outside = groups['voidscarred_out']
        self.assertIn('没有虚空变形效果', outside['nav_hint'])
        self.assertEqual([e['parent_name'] for e in outside['entry_paths'][0]], [None, 'voidscarred'])
        self.assertIsNone(groups[None]['rows'][0]['reference'])
        self.assertEqual(groups[None]['rows'][2]['entry_guards'], [])
        html = render_to_string('portal/_simc_reference_apl.html', {
            'apl_display': {'groups': list(groups.values())}, 'heroes': {'apl_id': 126},
        })
        self.assertIn('什么时候进入本组', html)
        self.assertIn('检查 ', html)
        self.assertIn('data-apl-rule="apl-list-3-rule-1"', html)
        self.assertNotIn('不应显示的控制技能', html)

    def test_unknown_subcondition_is_preserved_and_negated_as_a_whole(self):
        groups = self.groups('actions=run_action_list,name=a,if=active_enemies>1|variable.custom>3\n'
                             'actions+=/run_action_list,name=b\nactions.a=hit\nactions.b=hit')
        guard = groups['b']['entries'][0]['guards'][0]['text']
        self.assertIn('并且', guard)
        self.assertIn('(variable.custom>3)不成立', guard)
        self.assertIn('敌人', guard)

    def test_all_prior_runs_constrain_later_entry_but_calls_do_not(self):
        groups = self.groups('actions=run_action_list,name=a,if=active_enemies>2\n'
                             'actions+=/call_action_list,name=a,if=active_enemies=1\n'
                             'actions+=/run_action_list,name=a,if=active_enemies=2\n'
                             'actions+=/run_action_list,name=b\nactions.a=hit\nactions.b=hit')
        self.assertEqual([g['priority'] for g in groups['b']['entries'][0]['guards']], [1, 3])
        self.assertEqual(len(groups['a']['entries']), 3)

    def test_extra_execution_settings_do_not_invent_an_inverse_condition(self):
        groups = self.groups('actions=run_action_list,name=a,if=active_enemies>1,line_cd=10\n'
                             'actions+=/run_action_list,name=b\nactions.a=hit\nactions.b=hit')
        self.assertIn('执行设置', groups['b']['entries'][0]['guards'][0]['text'])
        self.assertTrue(groups['a']['entries'][0]['extra'])

    def test_unconditional_run_marks_following_entry_unreachable(self):
        groups = self.groups('actions=run_action_list,name=a\nactions+=/run_action_list,name=b\n'
                             'actions.a=hit\nactions.b=hit')
        self.assertTrue(groups['b']['entries'][0]['blocked'])

    def test_reset_discards_old_dispatch_and_cycles_are_bounded(self):
        groups = self.groups('actions=run_action_list,name=old\nactions=call_action_list,name=a\n'
                             'actions.old=hit\nactions.a=call_action_list,name=b\n'
                             'actions.b=call_action_list,name=a')
        self.assertEqual(groups['old']['entries'], [])
        self.assertTrue(groups['a']['paths_incomplete'])
        self.assertEqual(len(groups['a']['entry_paths']), 1)

    def test_unknown_conditions_are_escaped_in_rendered_output(self):
        html = render_to_string('portal/_simc_reference_condition.html', {
            'segments': [{'raw': '<script>alert(1)</script>'}],
        })
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<script>', html)

    def test_hierarchy_follows_call_order_not_definition_order(self):
        result = readable_apl('\n'.join([
            'actions.later=hit', 'actions.earlier=hit',
            'actions=call_action_list,name=earlier',
            'actions+=/run_action_list,name=later',
        ]), {})
        items = result['hierarchy'][0]['items']
        self.assertEqual([i['group']['name'] for i in items], [None, 'earlier', 'later'])
        self.assertEqual([i['path'] for i in items], ['', '1', '2'])
        # 原锚点保持文件顺序，已有外链仍指向原列表。
        self.assertEqual(items[1]['group']['anchor'], 'apl-list-1')
        self.assertEqual(items[2]['group']['anchor'], 'apl-list-0')

    def test_nested_dispatch_has_local_numbers_and_inline_decisions(self):
        source = '\n'.join([
            'actions=hit',
            'actions+=/run_action_list,name=voidscarred,if=hero_tree.voidscarred',
            'actions.voidscarred=run_action_list,name=voidscarred_meta,if=buff.metamorphosis.up',
            'actions.voidscarred+=/run_action_list,name=voidscarred_out',
            'actions.voidscarred_meta=hit',
            'actions.voidscarred_out=call_action_list,name=entry,if=active_enemies=1',
            'actions.entry=hit',
        ])
        result = readable_apl(source, self.symbols, hero_id=126)
        groups = {g['name']: g for g in result['groups']}
        self.assertEqual(groups['voidscarred']['label'], '阶段分流')
        self.assertEqual(groups['voidscarred_meta']['label'], '虚空变形期间')
        self.assertEqual(groups['voidscarred_out']['label'], '虚空变形之外')
        self.assertEqual(groups['entry']['hierarchy_path'], '2.2.1')
        branches = groups[None]['rows'][1]['branches']
        self.assertEqual([r['label'] for r in branches], ['虚空变形期间', '虚空变形之外'])
        self.assertEqual(branches[1]['guards'][0]['text'], '没有虚空变形效果')
        html = render_to_string('portal/_simc_reference_apl.html', {'apl_display': result})
        self.assertIn('调用路径 2.2.1', html)
        self.assertIn('进入后，按下一级顺序判断', html)

    def test_shared_list_has_no_false_unique_parent_or_global_rank(self):
        result = readable_apl('\n'.join([
            'actions=call_action_list,name=shared,if=active_enemies>1',
            'actions+=/run_action_list,name=phase',
            'actions.phase=call_action_list,name=shared,if=active_enemies=1',
            'actions.shared=hit',
        ]), {})
        group = next(g for g in result['groups'] if g['name'] == 'shared')
        self.assertEqual(group['role_label'], '公共子优先级')
        self.assertEqual(group['hierarchy_path'], '')
        self.assertEqual(group['hierarchy_section'], '公共子优先级')
        self.assertEqual(len(group['entries']), 2)
        items = [item for section in result['hierarchy'] for item in section['items']]
        self.assertEqual(sum(i['group']['name'] == 'shared' for i in items), 1)

    def test_phase_names_are_not_guessed_from_list_tokens(self):
        groups = self.groups('actions=run_action_list,name=meta,if=variable.unknown\n'
                             'actions+=/run_action_list,name=out\nactions.meta=hit\nactions.out=hit')
        self.assertEqual(groups['meta']['label'], 'meta')
        self.assertEqual(groups['out']['label'], 'out')

    def test_phase_with_extra_gate_does_not_get_a_false_simple_title(self):
        groups = self.groups('actions=run_action_list,name=meta,if=buff.metamorphosis.up,line_cd=10\n'
                             'actions+=/run_action_list,name=out\nactions.meta=hit\nactions.out=hit')
        self.assertEqual(groups['out']['label'], 'out')

    def test_multiple_gates_and_multiple_entries_do_not_get_simple_phase_names(self):
        groups = self.groups('actions=call_action_list,name=meta,if=active_enemies>1\n'
                             'actions+=/run_action_list,name=meta,if=buff.metamorphosis.up\n'
                             'actions+=/run_action_list,name=out\nactions.meta=hit\nactions.out=hit')
        self.assertEqual(groups['meta']['label'], 'meta')
        self.assertEqual(len(groups['meta']['entries']), 2)

    def test_precombat_orphans_and_cycles_are_retained_once(self):
        result = readable_apl('actions=hit\nactions.precombat=hit\nactions.orphan=hit\n'
                              'actions.a=call_action_list,name=b\nactions.b=call_action_list,name=a', {})
        items = [i for s in result['hierarchy'] for i in s['items']]
        self.assertCountEqual([i['group']['name'] for i in items], [None, 'precombat', 'orphan', 'a', 'b'])
        self.assertEqual(len({i['group']['anchor'] for i in items}), len(items))

    def test_short_unknown_list_uses_verified_skill_order(self):
        result = readable_apl('actions=call_action_list,name=entry\nactions.entry=first\nactions.entry+=/second', {
            ('action', 'first'): {'name': '技能甲'}, ('action', 'second'): {'name': '技能乙'},
        })
        self.assertEqual(result['groups'][1]['label'], '技能甲 → 技能乙')
