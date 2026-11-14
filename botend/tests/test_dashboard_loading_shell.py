from html.parser import HTMLParser

from django.contrib.auth import get_user_model
from django.test import TestCase


class _Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class DashboardLoadingShellTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser('loading-shell-admin')

    def setUp(self):
        self.client.force_login(self.user)

    def test_home_and_deep_link_have_visible_server_rendered_loading_state(self):
        for url in ('/dashboard/', '/dashboard/?section=simc-workflow'):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                elements = _Elements(response.content.decode()).elements
                shells = [attrs for _, attrs in elements if attrs.get('id') == 'dashboard-page-loading']
                self.assertEqual(len(shells), 1)
                self.assertEqual(shells[0].get('role'), 'status')
                self.assertEqual(shells[0].get('aria-live'), 'polite')
                self.assertNotIn('hidden', shells[0])
                self.assertNotIn('hidden', shells[0].get('class', '').split())
                self.assertContains(response, '长时间未响应？重新加载')
                self.assertContains(response, 'id="simc-talent-string-pagination"')

    def test_external_head_scripts_do_not_block_loading_shell_parsing(self):
        response = self.client.get('/dashboard/')
        head = response.content.decode().split('</head>', 1)[0]
        scripts = [attrs for tag, attrs in _Elements(head).elements if tag == 'script' and attrs.get('src')]
        self.assertTrue(scripts)
        for attrs in scripts:
            with self.subTest(src=attrs['src']):
                self.assertTrue('defer' in attrs or 'async' in attrs, attrs['src'])
        ordered = [attrs['src'].split('?')[0].rsplit('/', 1)[-1] for attrs in scripts]
        self.assertLess(ordered.index('main.js'), ordered.index('simc-workbench.js'))
