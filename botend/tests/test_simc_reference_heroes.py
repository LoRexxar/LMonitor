"""英雄天赋联动、局部覆盖和 APL 保守裁剪回归。"""

from unittest.mock import patch
from django.test import SimpleTestCase, TestCase
from botend.tests import test_portal_simc_reference as portal_tests
from botend.services.simc_apl.readable import readable_apl
from botend.services.simc_reference_heroes import filter_talent_rows
from botend.models import SimcTalentString


SOURCE = '\n'.join([
    'actions=auto_attack',
    'actions+=/call_action_list,name=slayer_st,if=hero_tree.slayer&active_enemies=1',
    'actions+=/run_action_list,name=thane_aoe,if=hero_tree.mountain_thane&active_enemies>1',
    'actions.slayer_st=bloodthirst',
    'actions.slayer_st+=/call_action_list,name=common',
    'actions.thane_aoe=thunder_clap',
    'actions.thane_aoe+=/call_action_list,name=common',
    'actions.common=rampage',
])


class HeroAplTests(SimpleTestCase):
    def test_upstream_hero_tokens_omit_apostrophes_and_hyphens(self):
        for hero_id, token in ((24, 'elunes_chosen'), (31, 'sanlayn'), (34, 'felscarred')):
            with self.subTest(token=token):
                source = f'actions=call_action_list,name={token}_aoe,if=hero_tree.{token}\nactions.{token}_aoe=spell'
                groups = readable_apl(source, {}, hero_id=hero_id)['groups']
                self.assertEqual(groups[1]['label'], '多目标')
                self.assertEqual(groups[0]['rows'][0]['fields'], [])
                self.assertEqual(len(readable_apl(source, {}, hero_id=60)['groups']), 1)

    def test_dispatch_prunes_only_unreachable_groups_and_simplifies_labels(self):
        result = readable_apl(SOURCE, {}, hero_id=60)
        self.assertEqual([g['name'] for g in result['groups']], [None, 'slayer_st', 'common'])
        self.assertEqual(result['groups'][1]['label'], '单体')
        row = result['groups'][0]['rows'][1]
        self.assertIn('有且仅有1个敌人', row['guide_text'])
        self.assertNotIn('屠戮者', row['guide_text'])
        self.assertIn('hero_tree.slayer', row['raw'])
        self.assertEqual(len(result['groups'][2]['entries']), 1)

    def test_unfiltered_retains_both_heroes(self):
        self.assertEqual(len(readable_apl(SOURCE, {})['groups']), 4)

    def test_other_tree_talent_is_false_but_selected_tree_optional_talent_is_unknown(self):
        symbols = {('talent', 'lightning_strikes'): {'hero_tree_ids': [61], 'name': '雷霆打击'},
                   ('talent', 'slayers_dominance'): {'hero_tree_ids': [60], 'name': '屠戮者之支配'}}
        source = '\n'.join(['actions=thunder_clap,if=talent.lightning_strikes',
                            'actions+=/bloodthirst,if=talent.slayers_dominance',
                            'actions+=/rampage,if=!talent.lightning_strikes.enabled'])
        rows = readable_apl(source, symbols, hero_id=60)['groups'][0]['rows']
        self.assertEqual([r['name'] for r in rows], ['bloodthirst', 'rampage'])
        self.assertIn('屠戮者之支配', rows[0]['guide_text'])
        self.assertEqual(rows[1]['fields'], [])

    def test_unknown_runtime_or_condition_and_unmapped_talent_are_kept(self):
        source = 'actions=bloodthirst,if=hero_tree.mountain_thane|rage>80\nactions+=/rampage,if=talent.unknown'
        rows = readable_apl(source, {}, hero_id=60)['groups'][0]['rows']
        self.assertEqual(len(rows), 2)
        self.assertIn('80', rows[0]['guide_text'])

    def test_boolean_subexpressions_keep_numeric_value(self):
        source = '\n'.join([
            'actions=bloodthirst,if=(hero_tree.slayer&2)=1',
            'actions+=/rampage,if=(hero_tree.mountain_thane|2)=1',
            'actions+=/execute,if=(hero_tree.slayer&rage)=1',
        ])
        rows = readable_apl(source, {}, hero_id=60)['groups'][0]['rows']
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]['fields'], [])
        self.assertEqual(rows[1]['fields'], [])
        self.assertTrue(rows[2]['fields'])

    def test_list_reset_does_not_make_an_unreferenced_group_disappear(self):
        source = 'actions=call_action_list,name=old,if=hero_tree.mountain_thane\nactions=bloodthirst\nactions.old=rampage'
        self.assertEqual(len(readable_apl(source, {}, hero_id=60)['groups']), 2)


class HeroPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        portal_tests.PortalSimcReferenceTests.setUpTestData.__func__(cls)

    def setUp(self):
        self.client.force_login(self.user)
        self.apl.content = SOURCE
        self.apl.save()
        self.slayer = SimcTalentString.objects.create(
            name='屠戮者方案', spec='warrior_fury', talent='屠戮者代码',
            hero_talent_names=['Slayer'], default_apl=self.apl, is_system=True, is_selectable=True)
        self.publication.talents.add(self.slayer)

    def test_page_filter_selects_matching_talent_and_apl(self):
        response = self.client.get(self.url, {'spec': 'warrior_fury', 'hero': '60'})
        self.assertEqual(response.context['talent'].id, self.slayer.id)
        self.assertEqual([r['id'] for r in response.context['selected_spec']['talents']], [self.slayer.id])
        self.assertEqual([g['name'] for g in response.context['apl_display']['groups']], [None, 'slayer_st', 'common'])
        self.assertNotContains(response, '<code>slayer_st</code>', html=True)

    def test_local_override_does_not_change_page_talent(self):
        response = self.client.get(self.url, {'spec': 'warrior_fury', 'hero': '60', 'apl_hero': '61'})
        self.assertEqual(response.context['talent'].id, self.slayer.id)
        self.assertEqual([g['name'] for g in response.context['apl_display']['groups']], [None, 'thane_aoe', 'common'])
        self.assertContains(response, 'name="hero" value="60"')
        self.assertContains(response, 'name="apl_hero" value="61"')

    def test_local_all_and_page_multiple_choices(self):
        for params in ({'hero': '60', 'apl_hero': 'all'}, {'hero': ['60', '61']}):
            response = self.client.get(self.url, {'spec': 'warrior_fury', **params})
            self.assertEqual(len(response.context['apl_display']['groups']), 4)

    def test_invalid_and_cross_spec_hero_is_404(self):
        for params in ({'hero': 'bad'}, {'hero': '62'}, {'apl_hero': '62'}):
            self.assertEqual(self.client.get(self.url, {'spec': 'warrior_fury', **params}).status_code, 404)

    def test_empty_match_does_not_display_other_hero_profile_code(self):
        self.slayer.is_active = False
        self.slayer.save()
        response = self.client.get(self.url, {'spec': 'warrior_fury', 'hero': '60'})
        self.assertIsNone(response.context['talent'])
        self.assertFalse(response.context['talent_code'])
        self.assertContains(response, '暂无符合所选英雄天赋的天赋方案')

    def test_legacy_membership_uses_current_branch_tree_metadata(self):
        self.slayer.hero_talent_names = []
        self.slayer.save()
        display = {'trees': [{'tree_type': 'hero', 'nodes': [{'selected': True, 'db2_subtree_id': 60}]}]}
        version = object()
        spec = {'class_name': 'Warrior', 'spec_name': 'Fury'}
        with patch('botend.services.simc_reference_heroes.reference_trees', return_value=display) as decode:
            rows, _ = filter_talent_rows([{'id': self.slayer.id}], {60}, spec, version)
        self.assertEqual(len(rows), 1)
        decode.assert_called_once_with(self.slayer.talent, spec, version)
