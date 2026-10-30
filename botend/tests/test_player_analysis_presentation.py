"""Manual and frozen-AI presentation: publication gating, escaping, and audit fidelity."""
from html.parser import HTMLParser
from types import SimpleNamespace
from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
import json
from copy import deepcopy
from pathlib import Path
from botend.services.player_analysis_management import frozen_ai
from botend.services.player_observation import _hash
from botend.services.player_observation_contract import build_contract

# UI-only contracts for the independent management page and manual portal projection.


class Markup(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class PlayerAnalysisManagementUITests(TestCase):
    def render_observation(self, **changes):
        observation = {'status': 'ready', 'source_kind': 'manual', 'is_published': True,
                       'review_status': 'approved', 'has_supported_judgements': False,
                       'display_judgements': [], 'conclusion': '人工核验结论 <script>bad</script>',
                       'evidence': [{'text': '结论证据', 'url': 'https://example.com/proof'},
                                    {'text': '非法链接', 'url': 'javascript:alert(1)'}]}
        observation.update(changes)
        return render_to_string('portal/spec_detail/_player_observation.html', {'observation': observation})

    def test_conclusion_only_manual_is_visible_escaped_and_links_are_safe(self):
        html = self.render_observation()
        self.assertIn('人工维护', html)
        self.assertIn('人工核验结论 &lt;script&gt;', html)
        self.assertIn('结论证据', html)
        links = [attrs for tag, attrs in Markup(html).elements if tag == 'a']
        self.assertEqual([a['href'] for a in links], ['https://example.com/proof'])

    def test_hidden_and_archived_cannot_leak_through_supported_raid(self):
        raid = {'supported': True, 'label': '团本标签', 'value': '支持'}
        for fields in ({'status': 'hidden'}, {'status': 'archived'}, {'is_published': False},
                       {'review_status': 'pending'}, {'review_status': 'rejected'}):
            self.assertEqual(self.render_observation(raid_judgement=raid, **fields).strip(), '')

    def test_manual_tag_evidence_and_existing_raid_sources(self):
        tag = {'supported': True, 'label': '人工标签', 'value': '确认', 'reason': '核验依据',
               'evidence': [{'text': '人工证据', 'url': 'https://example.com/manual'}]}
        raid = {'supported': True, 'label': '团本标签', 'value': '支持',
                'evidence': [{'text': '团本证据', 'source_url': 'https://example.com/raid'}]}
        html = self.render_observation(display_judgements=[tag], has_supported_judgements=True, raid_judgement=raid)
        self.assertIn('人工标签', html)
        self.assertIn('https://example.com/manual', html)
        self.assertIn('https://example.com/raid', html)

    def test_service_projection_without_editorial_flags_is_visible(self):
        # The service already gates publication; its public DTO omits review fields.
        html = render_to_string('portal/spec_detail/_player_observation.html', {'observation': {
            'status': 'ready', 'source_kind': 'manual', 'conclusion': '已审核结论',
            'has_supported_judgements': True, 'display_judgements': [],
            'evidence': [{'text': '冻结证据', 'url': 'https://example.com/proof',
                          'run_url': 'https://example.com/proof'}],
        }})
        self.assertIn('人工维护', html)
        self.assertIn('已审核结论', html)
        self.assertEqual(html.count('href="https://example.com/proof"'), 1)

    def test_dashboard_superuser_panel_and_spa_navigation(self):
        for superuser in (False, True):
            user = get_user_model().objects.create_user(
                username=f'test-{superuser}', is_superuser=superuser, is_staff=True)
            html = render_to_string('dashboard/index.html', {
                'user': user, 'request': SimpleNamespace(user=user), 'csrf_token': 'ui-csrf', 'page_name': 'dashboard'})
            elements = Markup(html).elements
            links = [a for t, a in elements if t == 'a' and a.get('href') == '?section=player-analyses']
            panels = [a for t, a in elements if a.get('id') == 'player-analyses']
            roots = [a for t, a in elements if a.get('id') == 'player-analysis-app']
            assets = [a for t, a in elements if 'player-analysis-management.' in (a.get('src', '') + a.get('href', ''))]
            self.assertEqual(len(links), int(superuser))
            self.assertEqual(len(panels), int(superuser))
            self.assertEqual(len(roots), int(superuser))
            self.assertEqual(len(assets), 2 * int(superuser))
            self.assertFalse(any(a.get('href') == '/dashboard/player-analyses/' for t, a in elements))
            if superuser:
                self.assertIn('content-section', panels[0]['class'].split())
                self.assertIn('none', panels[0]['style'])
                self.assertTrue(any(a.get('data-section') == 'player-analyses' and 'nav-item' in a.get('class', '').split() for t, a in elements))
                self.assertTrue(any(t == 'meta' and a.get('name') == 'csrf-token' and a.get('content') == 'ui-csrf' for t, a in elements))

    def test_panel_is_a_fragment_not_another_page_shell(self):
        html = render_to_string('dashboard/player_analysis_management.html')
        elements = Markup(html).elements
        self.assertFalse(any(t in ('html', 'head', 'body', 'main', 'script', 'iframe') for t, a in elements))
        self.assertFalse(any(a.get('id') == 'analysis-theme-toggle' for t, a in elements))
        self.assertIn('player-analysis-app', html)


# Management's real frozen validator/projection and Portal rendering boundary.



TEXT = 'A & B < C <img src=x onerror=alert(1)> 中文三场'


def publication(version=8):
    evidence = deepcopy(json.loads((Path(__file__).parent / 'fixtures/player_observation_contract_v8.json').read_text())[0]['evidence'])
    evidence['prompt_version'] = version
    evidence['eligible_labels'] = []
    evidence['conclusion_options'] = {'public_sample': {'text': '公开样本'}}
    for item in evidence['evidence']:
        item['text'] += ' ' + TEXT
    contract = build_contract(evidence)
    if version == 8:
        investigation = {'schema_version': 8, 'confidence': '低', 'hypotheses': [
            {'id': h['id'], 'assessment': 'possible', 'support_ids': h['support_ids'][:2],
             'counter_ids': h['counter_ids'], 'unknown_ids': h['unknown_ids'][:1]}
            for h in contract['hypotheses']], 'next_question_ids': contract['next_question_ids'][:3]}
    else:
        investigation = {'summary': '旧版冻结解释仍需核查', 'confidence': '低', 'hypotheses': [
            {'id': h['id'], 'assessment': 'possible', 'evidence_ids': h['support_ids'][:2],
             'counter_evidence_ids': h['counter_ids'], 'explanation': '仍需区分朋友互助等可能性'}
            for h in contract['hypotheses']], 'next_question_ids': contract['next_question_ids'][:3]}
    result = {'observations': [], 'conclusion_id': 'public_sample', 'investigation': investigation}
    digest = _hash(evidence)
    evidence['evidence_hash'] = digest
    return SimpleNamespace(evidence_json=evidence, result_json=result, evidence_hash=digest, prompt_version=version)


def portal(view):
    return render_to_string('portal/spec_detail/_player_observation.html', {'observation': {
        'status': 'ready', 'source_kind': 'ai', 'investigation': view}})


class FrozenManagementProjectionTests(SimpleTestCase):
    def test_real_versions_preserve_audit_and_render_full_unknowns(self):
        for version in (7, 8):
            row = publication(version)
            before = deepcopy(vars(row))
            ai = frozen_ai(row)
            self.assertEqual(ai['validation_status'], 'valid')
            self.assertEqual(vars(row), before)
            self.assertIs(ai['result_json'], row.result_json)
            self.assertIs(ai['evidence_json'], row.evidence_json)
            view = ai['rendered_investigation']
            self.assertTrue(view['hypotheses'])
            if version == 7:
                self.assertEqual(view['summary'], row.result_json['investigation']['summary'])
            for hypothesis in view['hypotheses']:
                frozen = next(h for h in row.evidence_json['metrics']['investigation']['hypotheses'] if h['id'] == hypothesis['id'])
                self.assertEqual([e['id'] for e in hypothesis['unknown_evidence']], frozen['unknown_ids'])
            html = portal(view)
            self.assertIn('A &amp; B &lt; C', html)
            self.assertNotIn('&amp;amp;', html)
            self.assertNotIn('<img', html)

    def test_v8_limitations_are_independent_paragraph_lists(self):
        row = publication()
        first = frozen_ai(row)['rendered_investigation']['limitations']
        second = frozen_ai(row)['rendered_investigation']['limitations']
        self.assertIsInstance(first, list)
        self.assertTrue(all(isinstance(text, str) and len(text) > 1 for text in first))
        self.assertIn('团队通关不是独立完成的证明。', ''.join(first))
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        first.clear()
        self.assertTrue(second)
        self.assertEqual(frozen_ai(row)['rendered_investigation']['limitations'], second)

    def test_tampered_hash_and_mixed_schema_are_audit_only(self):
        for mutation in ('hash', 'mixed', 'version'):
            row = publication()
            if mutation == 'hash':
                row.evidence_json['evidence'][0]['text'] = 'tampered'
            elif mutation == 'mixed':
                row.result_json['investigation']['summary'] = 'untrusted prose'
            else:
                row.prompt_version = 7
            ai = frozen_ai(row)
            self.assertEqual(ai['validation_status'], 'invalid')
            self.assertIsNone(ai['rendered_investigation'])
            self.assertEqual(ai['tags'], [])
            self.assertEqual(ai['conclusion'], '')
            self.assertIs(ai['result_json'], row.result_json)
