"""Public observation rendering: supported verdicts and bounded investigative explanations."""
from django.template.loader import render_to_string
from django.test import SimpleTestCase
from datetime import datetime, timezone
from html.parser import HTMLParser
from django.test import override_settings

# Portal shows verified investigative explanations without making them verdicts.


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
        return _render_observation(observation)

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


# Compact player observations: only supported labels earn public evidence.



class ObservationMarkup(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements, self.text = [], []
        self.depth = 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))
        if tag == 'details':
            self.depth += 1

    def handle_endtag(self, tag):
        if tag == 'details':
            self.depth -= 1

    def handle_data(self, data):
        self.text.append((data, self.depth))


@override_settings(TIME_ZONE='UTC')
class PlayerObservationTemplateTests(SimpleTestCase):
    def evidence(self):
        return {
            'judgements': [
                {'code': 'assistance', 'status': '未见明显迹象', 'verdict': '旧的否定结论', 'reason': '无效长解释'},
                {'code': 'off_spec', 'status': '证据不足', 'verdict': '重复未知结论', 'reason': '无效长解释'},
                {'code': 'rotation', 'status': '支持', 'verdict': '五人固定车队', 'reason': '固定阵容依据'},
            ],
            'eligible_labels': [{'code': 'fixed_team', 'label': '近期固定五人／偶尔替补',
                'reason_options': {'stable_five': '同样五人共同出现8/10场。'}, 'evidence_ids': ['fixed_team']}],
            'evidence': [{'id': 'fixed_team', 'text': '同样五人共同出现8/10场。',
                          'run_url': 'https://raider.io/mythic-plus-runs/season-mn-2/123'}],
            'metrics': {'team_pattern': {'kind': 'fixed_five'}, 'progression': None},
        }

    def observation(self, evidence=None):
        from botend.services.player_observation import _display_judgements
        rows = _display_judgements(evidence or self.evidence())
        return {'status': 'ready', 'display_judgements': rows,
                'generated_at': datetime(2026, 10, 13, 9, 30, tzinfo=timezone.utc),
                'has_supported_judgements': any(row['supported'] for row in rows),
                # Old verbose fields must not leak into the compact layout.
                'conclusion': '无效综合长结论', 'observations': [{'label': '限时经历'}],
                'progression': {'scope': '无效流程说明'}, 'coverage': {'best_runs': 8}}

    def render(self, observation):
        return _render_observation(observation)

    def test_only_supported_judgements_show_reasons_and_collapsed_evidence(self):
        observation = self.observation()
        html = self.render(observation)
        markup = ObservationMarkup(html)
        visible = ''.join(text for text, depth in markup.text if not depth)
        self.assertEqual(html.count('data-judgement='), 1)
        self.assertNotIn('证据不足', html)
        self.assertIn('近期固定五人／偶尔替补', visible)
        self.assertIn('依据：同样五人共同出现8/10场。', visible)
        for unwanted in ('无效', '限时经历', '最佳记录', '样本覆盖', '重复未知结论', '旧的否定结论'):
            self.assertNotIn(unwanted, html)
        details = [attrs for tag, attrs in markup.elements if tag == 'details']
        self.assertEqual(len(details), 1)
        self.assertNotIn('open', details[0])
        links = [attrs for tag, attrs in markup.elements if tag == 'a']
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]['target'], '_blank')
        self.assertEqual(set(links[0]['rel'].split()), {'noopener', 'noreferrer'})
        self.assertNotIn('>fixed_team<', html)

    def test_unsupported_raid_is_hidden_beside_supported_mplus(self):
        observation = self.observation()
        observation['raid_judgement'] = {'code':'raid_consumer', 'supported':False,
            'label':'团本消费', 'value':'证据不足', 'reason':'不应展示的依据'}
        html = self.render(observation)
        self.assertIn('近期固定五人／偶尔替补', html)
        self.assertNotIn('raid_consumer', html)
        self.assertNotIn('证据不足', html)
        self.assertNotIn('不应展示', html)

    def test_all_unsupported_hide_the_entire_observation_section(self):
        evidence = self.evidence()
        evidence['judgements'][2].update(status='未支持带人判断', reason='无效长解释')
        evidence['eligible_labels'] = []
        html = self.render(self.observation(evidence))
        self.assertEqual(html.strip(), '')
        for unwanted in ('依据：', '<details', '<a ', '无效', '非历史加分', '置信度'):
            self.assertNotIn(unwanted, html)

    def test_pre_progression_four_core_cannot_leak_unsupported_carry_label(self):
        evidence = self.evidence()
        evidence['metrics']['team_pattern']['kind'] = 'suspected_four'
        evidence['eligible_labels'] = [{'code':'rotating_core', 'label':'近期固定组合与轮换成员',
            'reason_options': {'core_roster': '结构不证明带人。'}, 'evidence_ids':['fixed_team']}]
        html = self.render(self.observation(evidence))
        self.assertIn('近期固定组合与轮换成员', html)
        self.assertNotIn('疑似四人带人队伍', html)
        self.assertIn('结构不证明带人', html)

    def test_legacy_current_score_progression_does_not_publish_carry_label(self):
        evidence = self.evidence()
        evidence['metrics']['team_pattern']['kind'] = 'suspected_four'
        evidence['metrics']['progression'] = {'status':'supported', 'core_runs':9, 'below_core_runs':7,
            'core_comparable_runs':9, 'low_fifth_runs':5, 'fifth_comparable_runs':7}
        evidence['eligible_labels'] = [{'code':'rotating_core', 'label':'近期固定组合与轮换成员',
            'reason_options': {'core_roster': '结构不证明带人。'}, 'evidence_ids':['fixed_team']}]
        html = self.render(self.observation(evidence))
        self.assertNotIn('疑似四人带人队伍', html)
        self.assertNotIn('7 / 9', html)
        self.assertNotIn('5 / 7', html)
        self.assertIn('近期固定组合与轮换成员', html)
        self.assertIn('https://raider.io/mythic-plus-runs/season-mn-2/123', html)
        self.assertNotIn('无效流程说明', html)

    def test_state_gating_autoescape_and_absent_observation(self):
        self.assertEqual(self.render(None).strip(), '')
        for status, message in (('stale','已过期'), ('pending','尚未生成'), ('error','生成失败'), ('unexpected','暂不可用')):
            observation = self.observation()
            observation['status'] = status
            html = self.render(observation)
            if status == 'stale':
                self.assertIn(message, html)
            else:
                self.assertEqual(html.strip(), '')
            if status != 'stale':
                self.assertNotIn('近期固定五人／偶尔替补', html)
                self.assertNotIn('<a ', html)
                self.assertNotIn('证据不足', html)
        observation = self.observation()
        row = observation['display_judgements'][0]
        row['value'] = row['reason'] = '<img src=x onerror="bad">'
        row['evidence'][0]['text'] = '<script>bad</script>'
        row['evidence'][0]['run_url'] = 'https://raider.io/test?q=" onmouseover="bad'
        html = self.render(observation)
        self.assertNotIn('<img', html)
        self.assertNotIn('<script', html)
        self.assertNotIn('onmouseover', next(attrs for tag, attrs in ObservationMarkup(html).elements if tag == 'a'))

    def test_player_page_places_observation_after_activity_before_talents(self):
        html = render_to_string('portal/spec_detail/player_detail.html', {
            'class_name':'Mage', 'spec_name':'Fire', 'nav':{},
            'player_detail': {'observation':self.observation(), 'raiderio_activity':{'status':'pending'},
                              'talent_groups':[{'title':'测试天赋','nodes':[]}]},
        })
        self.assertLess(html.index('id="player-rio-activity"'), html.index('id="player-observation"'))
        self.assertLess(html.index('id="player-observation"'), html.index('天赋配置'))


def _render_observation(observation):
    return render_to_string('portal/spec_detail/_player_observation.html', {'observation': observation})
