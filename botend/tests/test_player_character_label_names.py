"""Stable character label codes have one current public name, even for old facts."""
from copy import deepcopy
from django.test import SimpleTestCase
from django.template.loader import render_to_string
from botend.services.player_character_labels import (
    confirmed_character_judgements, confirmed_character_badges)


class CharacterLabelNamesTests(SimpleTestCase):
    def view(self, code, value):
        return {'status': 'ready', 'display_judgements': [{
            'code': code, 'label': '近期组队结构', 'value': value,
            'supported': True, 'reason': '原始依据，内容保持不变。',
            'evidence_strength': {'level': 'strong', 'confidence': '高', 'scope': '原始限定。'}}]}

    def test_all_three_names_apply_to_existing_and_new_public_projections(self):
        for code, old, new in (
            ('raid_consumer', '团本消费选手', '异常团本经历'),
            ('fixed_team', '5人固定', '固定5人车队'),
            ('fixed_team', '近期固定五人／偶尔替补', '固定5人车队'),
            ('rotating_core', '4人+轮换', '固定4人+轮换玩家'),
            ('rotating_core', '近期固定组合与轮换成员', '固定4人+轮换玩家'),
        ):
            with self.subTest(code=code, old=old):
                view = self.view(code, old)
                before = deepcopy(view)
                judgements = confirmed_character_judgements(view)
                badges = confirmed_character_badges(view)
                self.assertEqual(judgements[0]['value'], new)
                self.assertEqual(badges[0], {'code': code, 'label': new,
                    'confidence': '高', 'scope': '原始限定。'})
                html = render_to_string('portal/spec_detail/_player_observation.html', {'observation': view})
                self.assertIn(new, html)
                self.assertNotIn(f'<strong>{old}</strong>', html)
                if code == 'raid_consumer':
                    self.assertNotIn('团本消费', html)
                self.assertEqual(view, before)
                self.assertEqual(judgements[0]['reason'], before['display_judgements'][0]['reason'])

    def test_suffix_and_evidence_survive_a_copy_only_display_rename(self):
        view = self.view('fixed_team', '旧名称 · 证据充分')
        view['display_judgements'][0]['evidence'] = [{'id': 'original', 'text': '原始场次'}]
        before = deepcopy(view)
        rows = confirmed_character_judgements(view)
        self.assertEqual(rows[0]['value'], '固定5人车队 · 证据充分')
        self.assertEqual(confirmed_character_badges(view)[0]['label'], '固定5人车队')
        self.assertEqual(rows[0]['evidence'], before['display_judgements'][0]['evidence'])
        rows[0]['evidence'][0]['text'] = '修改返回副本'
        self.assertEqual(view, before)

    def test_independent_rule_uses_the_same_name_when_ai_is_pending(self):
        view = self.view('raid_consumer', '团本消费选手')
        view['status'] = 'pending'
        view['raid_judgement'] = view.pop('display_judgements')[0]
        self.assertEqual(confirmed_character_badges(view)[0]['label'], '异常团本经历')

    def test_rename_does_not_promote_unconfirmed_or_empty_values(self):
        for code in ('raid_consumer', 'fixed_team', 'rotating_core'):
            view = self.view(code, '原名称')
            view['display_judgements'][0]['evidence_strength']['level'] = 'candidate'
            self.assertEqual(confirmed_character_badges(view), [])
            self.assertEqual(confirmed_character_badges(self.view(code, '')), [])

    def test_unrelated_codes_and_own_manual_text_are_not_rewritten(self):
        for code in ('carrying', 'being_carried', 'joint_progress', 'manual:0'):
            view = self.view(code, '团本消费选手')
            self.assertEqual(confirmed_character_badges(view)[0]['label'], '团本消费选手')
        view = {'status': 'ready', 'source_kind': 'manual', 'review_status': 'approved',
            'manual': {'tags': [{'label': '人工说明', 'value': '5人固定'}]}}
        self.assertEqual(confirmed_character_badges(view)[0]['label'], '5人固定')
