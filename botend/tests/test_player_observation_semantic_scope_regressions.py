"""Small semantic contracts, not copies of private rollout payloads.

Expected RED cases deliberately assert the semantic rejection code: digit,
length, schema and unrelated commercial-vocabulary rejection are not success.
Only _validate runs; SimpleTestCase forbids database access. No model or fetch.
"""
import json

from django.test import SimpleTestCase

from botend.services import player_observation as observation


class SemanticScopeRegressionTests(SimpleTestCase):
    def validate(self, text, *, states=('unknown', 'unknown'), peer_confirmed=False,
                 explanation=False, run_ids=(200,), prior_ids=(200,), team_states=None):
        events = []
        for rid, state in zip((200, 201), states):
            members = {'subject': {'state': state, 'required_level': 23}}
            for index in range(4):
                members[f'peer{index}'] = {
                    'state': 'confirmed_before_start' if peer_confirmed and index == 0 and rid == 200 else 'unknown',
                    'required_level': 23,
                }
            if team_states is not None:
                for member in members.values():
                    member['state'] = team_states[rid - 200]
            events.append({'run_id': rid, 'level': 22, 'members': members})
        unknown = [f'investigation:motive_unknown:{rid}' for rid in run_ids]
        counter = [f'behavior:prior_higher:{rid}' for rid in prior_ids]
        hypotheses = [
            {'id': name, 'status': 'unknown', 'support_ids': [],
             'unknown_ids': unknown, 'counter_ids': counter if name == 'later_progress' else []}
            for name in ('key_access', 'later_progress')
        ]
        evidence = {'eligible_labels': [], 'evidence': [{'id': ref} for ref in unknown + counter],
                    'metrics': {'investigation': {'triggered': True, 'questions': [], 'hypotheses': hypotheses},
                                'behavior_review': {'resilience': {'subject_key': 'subject', 'events': events}}}}
        rows = [{'id': h['id'], 'assessment': 'unknown', 'evidence_ids': h['unknown_ids'],
                 'counter_evidence_ids': h['counter_ids'],
                 'explanation': text if explanation and h['id'] == 'key_access' else '保留未知解释。'}
                for h in hypotheses]
        raw = json.dumps({'observations': [], 'conclusion_id': 'public_sample', 'investigation': {
            'summary': '保留未知解释。' if explanation else text,
            'confidence': '低', 'hypotheses': rows, 'next_question_ids': [],
        }}, ensure_ascii=False)
        self.assertLessEqual(len(text), 220)
        self.assertFalse(any(c.isdigit() for c in text))
        return observation._validate(raw, evidence, require_investigation=True)

    def reject_semantically(self, text, **kwargs):
        for explanation in (False, True):
            with self.subTest(text=text, explanation=explanation):
                with self.assertRaisesRegex(observation.InvalidResult, '^unsupported_inference_assertion$'):
                    self.validate(text, explanation=explanation, **kwargs)

    def test_inverted_natural_drop_cannot_borrow_single_dungeon_history(self):
        # Exact substantive clause from 156934, not its unrelated fee clause.
        self.reject_semantically('部分场次的自然掉钥解释受跑前同本更高记录削弱。')

    def test_natural_downlevel_synonym_has_same_evidence_requirement(self):
        # 170656: summary mixes protected and unprotected prior-higher runs.
        self.reject_semantically('跑前已有同本更高记录只削弱对应场次的自然降层解释。',
                                 states=('confirmed_before_start', 'unknown'), run_ids=(200, 201))

    def test_all_unknown_cannot_be_rewritten_as_mixed_confirmation(self):
        # An explicit observed-state assertion, not the ambiguous general rule
        # "保护状态混合意味着..." in 169093 (reviewed separately, not auto-banned).
        self.reject_semantically('各场保护状态在已确认与未知之间混合。')

    def test_some_members_protected_is_not_whole_team_protected(self):
        # 160382: at most two protected members per event, never all five.
        self.reject_semantically('部分场次全队跑前保护亦已确认。',
                                 states=('confirmed_before_start', 'unknown'), peer_confirmed=True)

    def test_confirmed_protection_cannot_be_erased_with_state_word(self):
        self.reject_semantically('所有场次、所有成员的跑前保护状态均为未知。',
                                 states=('confirmed_before_start', 'unknown'))

    def test_confirmed_peer_is_not_erased_by_remaining_members(self):
        # 139376: "其余成员" means members other than the subject, not unknown runs.
        self.reject_semantically('本人已在多场确认跑前满足要求层，其余成员跑前保护状态未知。',
                                 states=('confirmed_before_start', 'confirmed_before_start'), peer_confirmed=True)

    def test_prior_higher_is_not_no_net_benefit_with_temporal_insertion(self):
        # Unambiguous minimal counterpart of 163185's ambiguous "净收益可能".
        self.reject_semantically('已确认的更高成绩只说明本人无当时净收益。')

    def test_all_members_includes_confirmed_peer_when_subject_unknown(self):
        self.reject_semantically('所有场次、所有成员的跑前保护状态均为未知。', peer_confirmed=True)

    def test_current_higher_cannot_become_prior_higher_counterevidence(self):
        # Clear assertion; unlike a bare general rule, it binds to cited run 200.
        self.reject_semantically('本场开打前已有同本更高限时，后续进步解释已被反驳。',
                                 prior_ids=())

    def test_known_forward_and_plain_erasure_are_already_rejected(self):
        self.reject_semantically('本人同本更高削弱自然掉钥解释。')
        self.reject_semantically('本人跑前保护均未知。',
                                 states=('confirmed_before_start', 'unknown'))
        self.reject_semantically('本人当场无净收益。')

    def test_scoped_remaining_runs_and_non_entailment_are_legal(self):
        for text in ('本人部分场次跑前保护已确认，其余场次保护未知。',
                     '本人部分场次跑前保护已确认，其余场次跑前保护仍未知。',
                     '各场开打前本人已有同本更高限时记录，后续进步无法解释对应场次的低层活动。'
                     '但这只削弱该解释，不证明净收益、动机或坚韧资格，也不能推广为并非为本人上分。',
                     '不能据同本更高记录断言本人无当时净收益。',
                     '自然掉钥解释不能由跑前同本更高记录削弱。'):
            for explanation in (False, True):
                with self.subTest(text=text, explanation=explanation):
                    self.validate(text, states=('confirmed_before_start', 'unknown'),
                                  prior_ids=(200, 201), explanation=explanation)

    def test_all_unknown_and_general_conditional_rule_are_legal(self):
        for text in ('所有场次、所有成员的跑前保护状态均为未知。',
                     '若保护状态混合，已确认保护的场次与未知场次须分开比较。',
                     '本人跑前保护未知，不能说全队跑前保护已确认。'):
            for explanation in (False, True):
                with self.subTest(text=text, explanation=explanation):
                    self.validate(text, explanation=explanation)

    def test_other_members_unknown_does_not_erase_subject_confirmation(self):
        self.validate('本人已确认跑前保护，其余成员跑前保护状态未知。',
                      states=('confirmed_before_start', 'confirmed_before_start'))

    def test_context_clarifications_are_not_global_unknown_assertions(self):
        for text in (
                '本人部分场次跑前保护已确认，但保护状态多数未知，逐场坚韧未知。',
                '缺少完整历史与成员坚韧记录。',
                '多数成员跑前坚韧状态未知。',
                '已确认保护的场次与未知场次须分开比较。'):
            for explanation in (False, True):
                self.validate(text, states=('confirmed_before_start', 'unknown'), explanation=explanation)
        self.reject_semantically('保护状态多数未知，因此本人跑前保护均未知。',
                                 states=('confirmed_before_start', 'unknown'))

    def test_generic_rules_and_ambiguous_net_benefit_are_not_facts(self):
        for text in ('保护状态混合意味着已确认保护的场次与未知场次须分开比较。',
                     '被引用场次的跑前更高也不等于本人无净收益。',
                     '已确认的更高成绩只说明本人无当时净收益可能。',
                     '若本人已有同本较高层观测，只反驳对应场次的后续进步解释。'):
            for explanation in (False, True):
                self.validate(text, explanation=explanation, prior_ids=())

    def test_state_negation_and_conditions_do_not_license_next_assertion(self):
        for text in ('不能确认各场保护状态在已确认与未知之间混合。',
                     '不能确认全队跑前保护已确认。',
                     '若全队跑前保护已确认，仍不能推断钥匙归属。'):
            self.validate(text)
        for text in ('不能确认全队跑前保护已确认且全队跑前保护已确认。',
                     '若保护状态混合。因此全队跑前保护已确认。'):
            self.reject_semantically(text)

    def test_peer_contradiction_cannot_borrow_an_unreferenced_run(self):
        self.validate('其余成员跑前保护状态未知。', explanation=True,
                      states=('confirmed_before_start', 'unknown'), peer_confirmed=True, run_ids=(201,))

    def test_inverted_and_adjacent_joint_basis_use_same_event_subject_scope(self):
        for text in (
                '在本人跑前全本更高层保护已确认的联合前提下，本场自然掉钥解释受本人跑前同本更高记录削弱。',
                '本人跑前全本更高层保护已确认，本人同本更高可削弱本场自然掉钥解释。'):
            self.validate(text, explanation=True, states=('confirmed_before_start', 'unknown'))
            with self.assertRaisesRegex(observation.InvalidResult, '^unsupported_inference_assertion$'):
                self.validate(text, explanation=True, states=('confirmed_before_start', 'unknown'), run_ids=(201,))
            with self.assertRaisesRegex(observation.InvalidResult, '^unsupported_inference_assertion$'):
                self.validate(text.replace('削弱', '排除'), explanation=True,
                              states=('confirmed_before_start', 'unknown'))
        self.reject_semantically('本人同本更高削弱自然掉钥解释。',
                                 states=('confirmed_before_start', 'unknown'))

    def test_local_unknown_does_not_erase_unreferenced_confirmation(self):
        for text in ('本人本场跑前保护状态未知。', '本人本场跑前保护未知。'):
            self.validate(text, explanation=True, run_ids=(201,), states=('confirmed_before_start', 'unknown'))
            with self.assertRaisesRegex(observation.InvalidResult, '^unsupported_inference_assertion$'):
                self.validate(text, states=('confirmed_before_start', 'unknown'))
        for text in ('全样本所有场次本人跑前保护未知。', '全样本任何人跑前保护状态未知。'):
            with self.assertRaisesRegex(observation.InvalidResult, '^unsupported_inference_assertion$'):
                self.validate(text, explanation=True, run_ids=(201,), states=('confirmed_before_start', 'unknown'))
        self.validate('所有场次本人跑前保护未知。', explanation=True, run_ids=(201,),
                      states=('confirmed_before_start', 'unknown'))

    def test_event_quantifier_is_independent_of_whole_team_quantifier(self):
        kwargs = dict(explanation=True, run_ids=(200, 201), team_states=('confirmed_before_start', 'unknown'))
        self.validate('部分场次全队跑前保护已确认。', **kwargs)
        with self.assertRaisesRegex(observation.InvalidResult, '^unsupported_inference_assertion$'):
            self.validate('所有场次全队跑前保护已确认。', **kwargs)

    def test_mixed_assertion_requires_both_states(self):
        text = '各场保护状态在已确认与未知之间混合。'
        for state in ('confirmed_before_start', 'unknown'):
            with self.subTest(state=state):
                with self.assertRaisesRegex(observation.InvalidResult, '^unsupported_inference_assertion$'):
                    self.validate(text, explanation=True, run_ids=(200, 201), team_states=(state, state))
        self.validate(text, explanation=True, run_ids=(200, 201),
                      team_states=('confirmed_before_start', 'unknown'))

    def test_joint_protection_stays_bound_to_referenced_run(self):
        text = '本人同本更高且开打前全更高层坚韧保护已确认可削弱自然掉钥解释。'
        self.validate(text, explanation=True, states=('confirmed_before_start', 'unknown'))
        with self.assertRaisesRegex(observation.InvalidResult, '^unsupported_inference_assertion$'):
            self.validate(text, explanation=True, states=('confirmed_before_start', 'unknown'), run_ids=(201,))
