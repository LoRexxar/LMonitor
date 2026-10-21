"""Portal shows verified investigative explanations without making them verdicts."""
from django.template.loader import render_to_string
from django.test import SimpleTestCase


class InvestigationPortalTests(SimpleTestCase):
    def render(self, **updates):
        observation = {
            'status': 'ready', 'source_kind': 'ai', 'has_supported_judgements': False,
            'display_judgements': [], 'investigation': {
                'triggered': True, 'summary': '已有重复低层活动线索，第五人成绩仍未知。',
                'hypotheses': [{'id': 'key_supply', 'label': '钥匙供需或高分外援',
                    'assessment': 'possible', 'explanation': '只能作为待核查解释，不能确认付款。',
                    'evidence': [{'id': 'seed:123', 'text': '可验证的近期重复同场记录',
                                  'run_url': 'https://raider.io/mythic-plus-runs/season-mn-2/123-20-example'}],
                    'counter_evidence': []}],
                'questions': [{'id': 'score:outside', 'reason': '核查相关轮换成员的同副本成绩'}],
                'stop_reason': 'budget_exhausted',
            },
        }
        observation.update(updates)
        return render_to_string('portal/spec_detail/_player_observation.html', {'observation': observation})

    def test_clues_are_visible_without_a_final_supported_label(self):
        html = self.render()
        self.assertIn('data-player-investigation', html)
        self.assertIn('已有线索，尚待核查', html)
        self.assertIn('低置信', html)
        self.assertIn('钥匙供需或高分外援', html)
        self.assertIn('核查相关轮换成员', html)
        self.assertIn('可验证的近期重复同场记录', html)
        self.assertNotIn('score:outside', html)

    def test_hidden_rejected_and_manual_publications_do_not_expose_ai_investigation(self):
        for updates in ({'status': 'hidden'}, {'review_status': 'rejected'}, {'is_published': False},
                        {'source_kind': 'manual', 'conclusion': '人工结论'}, {'status': 'pending'}):
            with self.subTest(updates=updates):
                self.assertNotIn('data-player-investigation', self.render(**updates))
        self.assertNotIn('data-player-investigation', self.render(investigation=None))

    def test_validated_unknown_references_render_as_escaped_gaps_not_support(self):
        from django.utils.html import escape
        from botend.services.player_observation import _project_investigation, _validate_investigation
        from botend.tests.test_player_observation_investigation import InvestigationTests

        fixture = InvestigationTests()
        fixture.setUp()
        plan = fixture.build()
        model = {
            'summary': '已有线索仍待核查', 'confidence': '低',
            'hypotheses': [{
                'id': h['id'], 'assessment': 'possible', 'evidence_ids': h['unknown_ids'][:1],
                'counter_evidence_ids': h['counter_ids'], 'explanation': '仍需区分朋友互助等可能性',
            } for h in plan['hypotheses'][:2]],
            'next_question_ids': [],
        }
        self.assertTrue(all(h['evidence_ids'] for h in model['hypotheses']))
        _validate_investigation(model, plan, bounded=True)
        # Source text, not model prose: exercise the real template's escaping.
        unknown_id = model['hypotheses'][0]['evidence_ids'][0]
        source = next(e for e in plan['evidence'] if e['id'] == unknown_id)
        source['text'] += ' A & B < C <script>unsafe()</script><img src=x onerror=alert(1)>'
        source['run_url'] = 'javascript:alert(1)'
        projected = _project_investigation(model, {
            'metrics': {'investigation': plan}, 'evidence': plan['evidence'],
        })
        self.assertTrue(all(not h['evidence'] for h in projected['hypotheses']))
        self.assertTrue(all(h['unknown_evidence'] for h in projected['hypotheses']))
        html = self.render(investigation=projected)
        self.assertEqual(html.count('<p>尚未核实的事项（不作为支持或反证）</p>'), len(projected['hypotheses']))
        self.assertNotIn('<p>支持线索</p>', html)
        for h in projected['hypotheses']:
            for evidence in h['unknown_evidence']:
                self.assertInHTML(f'<span>{escape(evidence["text"])}</span>', html)
        self.assertNotIn('<script>', html)
        self.assertNotIn('<img', html)
        self.assertNotIn('href="javascript:', html)
        self.assertNotRegex(html, r'[0-9a-f]{64}|\[object Object\]')
        self.assertNotIn('unknown_evidence', html)

    def test_model_text_is_escaped_and_stale_state_is_clear(self):
        html = self.render(status='stale', investigation={
            'triggered': True, 'summary': '<script>unsafe()</script>', 'hypotheses': [], 'questions': []})
        self.assertIn('&lt;script&gt;unsafe()&lt;/script&gt;', html)
        self.assertNotIn('<script>unsafe()', html)
        self.assertIn('已过期', html)
