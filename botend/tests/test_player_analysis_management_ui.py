"""UI-only contracts for the independent management page and manual portal projection."""
from html.parser import HTMLParser
from types import SimpleNamespace
from django.template.loader import render_to_string
from django.test import SimpleTestCase


class Markup(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class PlayerAnalysisManagementUITests(SimpleTestCase):
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
            user = SimpleNamespace(is_superuser=superuser, is_staff=True, username='test')
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
