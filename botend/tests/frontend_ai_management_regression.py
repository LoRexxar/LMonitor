"""Standalone local-HTTP browser regression; fixtures only, no model calls.

Run with the Python environment containing Playwright. This is intentionally not
Django autodiscovery: python botend/tests/frontend_ai_management_regression.py
"""
import copy
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import unittest

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
PROVIDER = dict(provider_key='codex', name='Codex', enabled=True, priority=0,
                request_timeout_seconds=90, model=None, base_url=None,
                version=0, scopes=['translation'], scope_label='文章翻译专用',
                effective=False, has_api_key=False, config_source='settings',
                api_key_source='settings', state={'code': 'incomplete'})


class BrowserRegression(unittest.TestCase):
    provider = {}
    payloads = []
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def send(self, body, content_type='application/json'):
                self.send_response(200)
                self.send_header('Content-Type', content_type + '; charset=utf-8')
                self.end_headers()
                self.wfile.write(body.encode())

            def data(self):
                return json.dumps({'status': 'success', 'data': {'providers': [cls.provider]}})

            def do_GET(self):
                if self.path.startswith('/api/'):
                    self.send(self.data())
                elif self.path == '/script.js':
                    self.send((ROOT / 'static/dashboard/js/ai-management.js').read_text(), 'text/javascript')
                else:
                    html = (ROOT / 'templates/dashboard/_ai_management.html').read_text()
                    self.send('<meta name="csrf-token" content="fixture-csrf">' +
                              html.replace('display: none;', 'display: block;') +
                              '<script src="/script.js"></script>', 'text/html')

            def do_PATCH(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                cls.payloads.append(payload)
                cls.provider.update({k: v for k, v in payload.items() if k in cls.provider})
                cls.provider['version'] += 1
                self.send(self.data())

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True, args=['--no-sandbox'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        type(self).provider = copy.deepcopy(PROVIDER)
        type(self).payloads = []
        self.page = self.browser.new_page()
        self.page.on('dialog', lambda dialog: dialog.accept())
        self.page.goto(f'http://127.0.0.1:{self.server.server_port}/')
        self.page.get_by_role('button', name='编辑配置').click()

    def tearDown(self):
        self.page.close()

    def save(self, expected):
        self.page.locator('#ai-save').click()
        expect(self.page.locator('#ai-editor')).not_to_be_visible(timeout=1500)
        self.assertEqual(self.payloads, [expected])
        expect(self.page.locator('#ai-message')).to_contain_text('重新读取确认')
        self.assertEqual(self.page.locator('#ai-api-key').input_value(), '')

    def test_unconfigured_disable_is_sparse(self):
        self.page.locator('#ai-enabled').uncheck()
        self.save({'version': 0, 'enabled': False})

    def test_unconfigured_priority_is_sparse(self):
        self.page.locator('#ai-priority').fill('1')
        self.save({'version': 0, 'priority': 1})

    def test_zero_priority_and_false_bootstrap_are_preserved(self):
        self.page.locator('#ai-editor-close').click()
        type(self).provider.update(priority=10, enabled=False)
        self.page.reload()
        self.page.get_by_role('button', name='编辑配置').click()
        expect(self.page.locator('#ai-enabled')).not_to_be_checked()
        self.page.locator('#ai-priority').fill('0')
        self.save({'version': 0, 'priority': 0})

    def test_explicit_empty_model_and_url_are_rejected(self):
        self.page.locator('#ai-editor-close').click()
        type(self).provider.update(model='Exact.Model-V2', base_url='https://codex.example/v1')
        self.page.reload()
        self.page.get_by_role('button', name='编辑配置').click()
        for selector in ('#ai-model', '#ai-base-url'):
            with self.subTest(selector=selector):
                self.page.locator(selector).fill('')
                self.page.locator('#ai-save').click()
                expect(self.page.locator('#ai-editor')).to_be_visible()
                self.assertEqual(self.payloads, [])
                self.page.locator('#ai-editor-close').click()
                self.page.get_by_role('button', name='编辑配置').click()

    def test_unconfigured_clear_key_is_sparse(self):
        self.page.locator('#ai-clear-key').check()
        self.save({'version': 0, 'clear_api_key': True})

    def test_explicit_fields_are_preserved_without_normalizing(self):
        self.page.locator('#ai-model').fill('Exact.Model-V2')
        self.page.locator('#ai-base-url').fill('https://codex.example/v1')
        self.save({'version': 0, 'model': 'Exact.Model-V2', 'base_url': 'https://codex.example/v1'})

    def test_invalid_changed_fields_do_not_patch(self):
        for selector, value in [('#ai-timeout', '0'), ('#ai-timeout', '601'),
                                ('#ai-priority', '-1'), ('#ai-model', ' spaced '),
                                ('#ai-base-url', 'http://codex.example')]:
            with self.subTest(selector=selector, value=value):
                self.page.locator(selector).fill(value)
                self.page.locator('#ai-api-key').fill('fixture-review-secret')
                self.page.locator('#ai-save').click()
                expect(self.page.locator('#ai-editor')).to_be_visible()
                expect(self.page.locator('#ai-api-key')).to_have_value('')
                expect(self.page.locator(selector)).to_have_value(value)
                self.assertEqual(self.payloads, [])
                self.page.locator('#ai-editor-close').click()
                self.page.get_by_role('button', name='编辑配置').click()

    def test_clear_and_refill_conflict_clears_key_without_patch(self):
        key = self.page.locator('#ai-api-key')
        key.fill('fixture-review-secret')
        self.page.locator('#ai-clear-key').check()
        expect(key).to_have_value('')
        # The change handler disables the key; emulate an autofill after that event.
        key.evaluate("input => { input.value = 'fixture-review-secret'; }")
        self.page.locator('#ai-save').click()
        expect(key).to_have_value('')
        expect(self.page.locator('#ai-clear-key')).to_be_checked()
        expect(self.page.locator('#ai-editor-error')).to_contain_text('不能同时设置和清除')
        self.assertEqual(self.payloads, [])

    def test_valid_key_is_sent_exactly_then_cleared(self):
        self.page.locator('#ai-api-key').fill('fixture-review-secret')
        self.save({'version': 0, 'api_key': 'fixture-review-secret'})


if __name__ == '__main__':
    unittest.main()
