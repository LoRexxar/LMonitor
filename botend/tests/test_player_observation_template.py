"""Compact player observations: only supported labels earn public evidence."""
from datetime import datetime, timezone
from html.parser import HTMLParser

from django.template.loader import render_to_string
from django.test import SimpleTestCase, override_settings


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
            'eligible_labels': [{'code': 'fixed_team', 'label': '五人固定车队', 'evidence_ids': ['fixed_team']}],
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
        return render_to_string('portal/spec_detail/_player_observation.html', {'observation': observation})

    def test_only_supported_judgements_show_reasons_and_collapsed_evidence(self):
        observation = self.observation()
        html = self.render(observation)
        markup = ObservationMarkup(html)
        visible = ''.join(text for text, depth in markup.text if not depth)
        self.assertEqual(html.count('data-judgement='), 3)
        self.assertEqual(visible.count('证据不足'), 2)
        self.assertIn('五人固定车队', visible)
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

    def test_all_unsupported_are_three_bare_insufficient_rows(self):
        evidence = self.evidence()
        evidence['judgements'][2].update(status='未支持带人判断', reason='无效长解释')
        html = self.render(self.observation(evidence))
        self.assertEqual(html.count('证据不足'), 3)
        for unwanted in ('依据：', '<details', '<a ', '无效', '非历史加分', '置信度'):
            self.assertNotIn(unwanted, html)

    def test_pre_progression_four_core_cannot_leak_unsupported_carry_label(self):
        evidence = self.evidence()
        evidence['metrics']['team_pattern']['kind'] = 'suspected_four'
        evidence['eligible_labels'] = [{'code':'rotating_core', 'label':'疑似四人带人队伍', 'evidence_ids':['fixed_team']}]
        html = self.render(self.observation(evidence))
        self.assertEqual(html.count('证据不足'), 3)
        self.assertNotIn('疑似四人带人队伍', html)
        self.assertNotIn('依据：', html)

    def test_supported_progression_uses_short_basis_and_keeps_source_details(self):
        evidence = self.evidence()
        evidence['metrics']['team_pattern']['kind'] = 'suspected_four'
        evidence['metrics']['progression'] = {'status':'supported', 'core_runs':9, 'below_core_runs':7,
            'core_comparable_runs':9, 'low_fifth_runs':5, 'fifth_comparable_runs':7}
        evidence['eligible_labels'] = [{'code':'rotating_core', 'label':'疑似四人带人队伍', 'evidence_ids':['fixed_team']}]
        html = self.render(self.observation(evidence))
        self.assertIn('疑似四人带人队伍', html)
        self.assertIn('7 / 9', html)
        self.assertIn('5 / 7', html)
        self.assertIn('当前成绩非历史加分', html)
        self.assertNotIn('无效流程说明', html)

    def test_state_gating_autoescape_and_absent_observation(self):
        self.assertEqual(self.render(None).strip(), '')
        for status, message in (('stale','已过期'), ('pending','尚未生成'), ('error','生成失败'), ('unexpected','暂不可用')):
            observation = self.observation()
            observation['status'] = status
            html = self.render(observation)
            self.assertIn(message, html)
            if status != 'stale':
                self.assertNotIn('五人固定车队', html)
                self.assertNotIn('<a ', html)
                self.assertNotIn('证据不足', html)
        observation = self.observation()
        row = observation['display_judgements'][2]
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
