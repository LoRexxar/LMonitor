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

    def test_dashboard_superuser_link_is_not_a_spa_target(self):
        for superuser in (False, True):
            user = SimpleNamespace(is_superuser=superuser, is_staff=superuser, username='test')
            html = render_to_string('dashboard/index.html', {'user': user, 'request': SimpleNamespace(user=user)})
            markup = Markup(html)
            links = [attrs for tag, attrs in markup.elements if tag == 'a' and attrs.get('href') == '/dashboard/player-analyses/']
            self.assertEqual(len(links), int(superuser))
            if superuser:
                index = next(i for i, item in enumerate(markup.elements) if item[0] == 'a' and item[1].get('href') == '/dashboard/player-analyses/')
                parent = markup.elements[index - 1][1]
                self.assertNotIn('nav-item', parent.get('class', '').split())
                self.assertNotIn('submenu-item', parent.get('class', '').split())

    def test_independent_template_contains_return_link_and_csrf(self):
        html = render_to_string('dashboard/player_analysis_management.html', {'csrf_token': 'ui-csrf'})
        elements = Markup(html).elements
        self.assertTrue(any(t == 'a' and a.get('href') == '/dashboard/' for t, a in elements))
        self.assertTrue(any(t == 'meta' and a.get('name') == 'csrf-token' and a.get('content') == 'ui-csrf' for t, a in elements))
        self.assertIn('player-analysis-app', html)
