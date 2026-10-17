"""Local browser UI fixture: no external network, credentials, or production writes.

Run: python3 botend/tests/player_analysis_management_browser_test.py
Real Chromium renders Django markup and actual CSS/JS at desktop and 390px.
The API below is explicitly a UI test fixture, not backend integration evidence.
"""
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'LMonitor.settings_test_sqlite')
import django
django.setup()
from django.template.loader import render_to_string
from playwright.sync_api import sync_playwright


def main():
    html = render_to_string('dashboard/player_analysis_management.html', {'csrf_token': 'ui-test-token'})
    fixture = {'id': 7, 'revision': 1, 'player_id': 42,
               'player': {'id': 42, 'character_name': '真实角色测试名字' * 8, 'region': 'cn', 'realm': '测试服务器',
                          'class_name': '法师', 'spec_name': '火焰', 'season_id': 4},
               'status': 'ready', 'review_status': 'pending', 'is_published': False, 'review_note': '',
               'source_kind': 'ai', 'manual': None,
               'ai': {'tags': [{'label': '固定队', 'value': '已核验', 'reason': '<script>不能执行</script>', 'evidence': []}],
                      'conclusion': '冻结结论', 'evidence': [{'text': '冻结证据', 'url': 'https://example.com/proof'}],
                      'result_json': {'fixture': True}, 'evidence_json': {}}, 'effective': {}}
    calls = []

    def route(request_route):
        request = request_route.request
        path = urlparse(request.url).path
        if path.startswith('/static/'):
            asset = ROOT / path.lstrip('/')
            request_route.fulfill(body=asset.read_text(), content_type='text/css' if path.endswith('.css') else 'text/javascript')
        elif path.startswith('/dashboard/api/player-analyses/'):
            calls.append((path, request.method))
            if request.method == 'PATCH':
                payload = request.post_data_json
                assert payload['revision'] == fixture['revision']
                assert request.headers['x-csrftoken'] == 'ui-test-token'
                fixture.update(payload)
                fixture['revision'] += 1
            data = fixture if path.endswith('/7/') else {'items': [fixture], 'page': 1, 'total': 1, 'page_size': 20, 'total_pages': 1}
            request_route.fulfill(json={'status': 'success', 'data': data})
        elif path == '/dashboard/player-analyses/':
            request_route.fulfill(body=html, content_type='text/html')
        else:
            request_route.abort()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/*', route)
        page.goto('http://analysis-ui.test/dashboard/player-analyses/')
        page.locator('[data-action="open"]').wait_for()
        assert calls == [('/dashboard/api/player-analyses/', 'GET')], calls
        page.locator('[data-action="open"]').click()
        page.locator('.pa-detail h2').wait_for()
        assert page.locator('.pa-detail').inner_text().find('冻结证据') >= 0
        assert page.locator('.pa-detail script').count() == 0
        for width in (1440, 390):
            page.set_viewport_size({'width': width, 'height': 1000})
            for theme in ('light', 'dark'):
                page.evaluate('(theme) => document.documentElement.dataset.dashboardTheme = theme', theme)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, theme)
                assert page.locator('a[href="/dashboard/"]').is_visible()
                assert page.locator('[data-action="save"]').is_visible()
                colors = page.locator('body').evaluate('(node) => [getComputedStyle(node).backgroundColor, getComputedStyle(node).color]')
                assert colors[0] != colors[1]
        page.locator('.pa-edit-form [data-field="review_status"]').select_option('approved')
        page.locator('[data-action="save"]').click()
        page.get_by_role('status').filter(has_text='已保存').wait_for()
        patch_index = calls.index(('/dashboard/api/player-analyses/7/', 'PATCH'))
        assert calls[patch_index + 1] == ('/dashboard/api/player-analyses/7/', 'GET')
        page.locator('[data-action="edit-manual"]').click()
        page.locator('[data-action="add-tag"]').click()
        page.locator('.pa-tag-editor [data-action="add-evidence"]').click()
        assert page.locator('[data-evidence-field="text"]').is_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert page.evaluate('Object.keys(localStorage).length') == 0
        assert errors == [], errors
        browser.close()
    print(json.dumps({'browser': 'Chromium', 'viewports': [1440, 390], 'themes': ['light', 'dark'],
                      'overflow': False, 'page_errors': errors, 'lazy_detail': True,
                      'csrf_and_readback': True, 'manual_form': True}, ensure_ascii=False))


if __name__ == '__main__':
    main()
