"""Real public snapshot shell, release rollover and browser request-order contracts."""
import hashlib
import json
import re
from pathlib import Path
from socketserver import ThreadingMixIn
from tempfile import TemporaryDirectory
from threading import Thread
from time import sleep
from unittest.mock import patch
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from django.contrib.staticfiles.handlers import StaticFilesHandler
from django.core.management import call_command
from django.core.wsgi import get_wsgi_application
from django.template.loader import render_to_string
from django.test import SimpleTestCase, override_settings

from botend import site_image_map as resource
from botend.services import class_guide_snapshot as snapshots, site_image_assets as assets


def records(count):
    return {f'https://wow.zamimg.com/images/wow/icons/small/spell_{i}.jpg':
            [f'wow_icons_oss/small/spell_{i}.jpg', 'a' * 64] for i in range(count)}


class SiteImageMapResourceTests(SimpleTestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.manifest = self.root / 'release.json'
        self.enterContext(patch.object(assets, 'MANIFEST', self.manifest))
        self.enterContext(override_settings(CLASS_GUIDE_SNAPSHOT_ROOT=self.root))
        self.addCleanup(assets._published.cache_clear)
        self.addCleanup(resource.release_script.cache_clear)

    def release(self, rows):
        self.manifest.write_text(json.dumps({'schema': 1, 'images': rows}))
        assets._published.cache_clear()  # new release process
        resource.release_script.cache_clear()

    def shell(self):
        header = render_to_string('portal/_header.html').encode()
        page = snapshots._publish_page('portal/class_guides.html', {}, header)
        return snapshots._html_content(page['html'])

    def test_public_shell_does_not_scale_with_release_size(self):
        self.release(records(1))
        small = self.shell()
        self.release(records(800))
        large = self.shell()
        self.assertEqual(len(small), len(large), (len(small), len(large)))
        self.assertNotIn(b'spell_799', large)
        url = self.script_url(large)
        self.assertNotEqual(self.script_url(small), url)
        body = self.client.get(url).content
        self.assertIn(hashlib.sha256(body).hexdigest(), url)
        self.assertIn(b'spell_799', body)
        with patch.object(resource, 'published_image_map', side_effect=AssertionError('warm map rebuilt')):
            self.assertEqual(self.shell(), large)
            response = self.client.head(url)
            self.assertEqual(int(response['Content-Length']), len(body))
            self.assertEqual(response.content, b'')
            cached = self.client.get(url, HTTP_IF_NONE_MATCH=response['ETag'])
            self.assertEqual((cached.status_code, cached.content), (304, b''))
            self.assertEqual(cached['Cache-Control'], 'public, max-age=31536000, immutable')

    @staticmethod
    def script_url(html):
        return re.search(rb'<script src="(/assets/site-image-maps/[^"?]+)"', html)[1].decode()

    def test_archived_snapshot_keeps_its_map_after_release_change(self):
        rows = records(2)
        self.release(rows)
        url = self.script_url(self.shell())
        original = self.client.get(url).content
        with override_settings(BASE_DIR=self.root):
            call_command('freeze_site_image_map', verbosity=0)
            self.release(dict(reversed(list(rows.items()))))
            self.assertEqual(self.script_url(self.shell()), url)
            self.release({})
            self.assertNotIn(b'/assets/site-image-maps/', self.shell())
            self.assertEqual(self.client.get(url).content, original)
            path = resource.archive_root() / url.split('/')[-1]
            path.write_bytes(b'corrupt')
            missing = self.client.get(url)
            self.assertEqual((missing.status_code, missing['Cache-Control']), (404, 'no-store'))

    def test_chromium_cold_and_warm_consumer(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest('Playwright required for browser regression')
        self.release(records(800))
        # The real parser sees a consumer immediately after the shared include.
        # Unlike Playwright routing, DNS isolation leaves the HTTP cache enabled.
        consumer = b'''
<script src="/static/dashboard/js/combat_spell_icon.js"></script>
<combat-spell-icon data-spell="1"></combat-spell-icon><combat-spell-icon data-spell="2"></combat-spell-icon>'''
        html = render_to_string('shared/_site_images.html').encode() + consumer
        url = self.script_url(html)
        django_app = StaticFilesHandler(get_wsgi_application())
        requests = []

        def app(environ, start_response):
            path = environ['PATH_INFO']
            if path == '/consumer':
                start_response('200 OK', [('Content-Type', 'text/html'), ('Cache-Control', 'no-store')])
                return [html]
            if path == url:
                requests.append(path)
                sleep(0.1)  # cold miss: consumers must wait for the map
            return django_app(environ, start_response)

        class QuietHandler(WSGIRequestHandler):
            def log_message(self, *args):
                pass

        class ThreadedServer(ThreadingMixIn, WSGIServer):
            daemon_threads = True

        with make_server('127.0.0.1', 0, app, server_class=ThreadedServer, handler_class=QuietHandler) as httpd:
            thread = Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(args=['--no-proxy-server', '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1'])
                    context = browser.new_context()
                    context.add_init_script("sessionStorage.setItem('combat-spell-icons:v1', "
                                            "JSON.stringify([['1','spell_799'],['2','spell_missing']]))")
                    page = context.new_page()
                    seen = []
                    page.on('request', lambda req: seen.append(req.url) if req.resource_type == 'image' else None)
                    upstream = 'https://wow.zamimg.com/images/wow/icons/small/'
                    mirror = 'https://oss.wowdaily.cn/wow_icons_oss/small/'
                    for visit in (1, 2, 3):
                        if visit == 3:
                            self.release({})
                            html = render_to_string('shared/_site_images.html').encode() + consumer
                        seen.clear()
                        page.goto(f'http://127.0.0.1:{httpd.server_port}/consumer?visit={visit}')
                        page.wait_for_function("Array.from(document.querySelectorAll('combat-spell-icon')).every(n => n.hidden || n.firstElementChild)")
                        page.wait_for_load_state('networkidle')
                        self.assertCountEqual(seen, [mirror + 'spell_799.jpg'] if visit < 3 else [])
                        self.assertFalse(any(src.startswith(upstream) for src in seen))
                        self.assertEqual(requests, [url])  # second navigation: no HTTP round trip
                    browser.close()
            finally:
                httpd.shutdown()
                thread.join()
