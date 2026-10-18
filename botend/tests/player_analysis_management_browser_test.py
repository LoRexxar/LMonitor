"""Local browser UI fixture: no external network, credentials, or production writes.

Run: python3 botend/tests/player_analysis_management_browser_test.py
Real Chromium renders the Django panel, shared Dashboard styles and actual router.
The surrounding shell and API are UI fixtures, not full-site integration evidence.
"""
import json
import os
import re
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'LMonitor.settings_test_sqlite')
import django
django.setup()
from django.template.loader import render_to_string
from playwright.sync_api import sync_playwright


def main():
    panel = render_to_string('dashboard/player_analysis_management.html')
    dashboard = render_to_string('dashboard/index.html', {'csrf_token': 'ui-test-token'})
    style_match = re.search(r'<style>(.*?)</style>', dashboard, re.S)
    assert style_match
    shared_styles = style_match.group(1)
    html = '''<!doctype html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
        <meta name="csrf-token" content="ui-test-token"><style>''' + shared_styles + '''
        .dashboard-shell {display:flex} .main-content {padding:16px} body {margin:0}
        </style><link rel="stylesheet" href="/static/dashboard/css/player-analysis-management.css">
        <script defer src="/static/dashboard/js/player-analysis-management.js"></script></head>
        <body class="dashboard-page"><header><button id="dashboard-theme-toggle"><i id="dashboard-theme-icon"></i>主题</button></header>
        <div class="dashboard-shell"><nav><ul>
        <li class="nav-item" data-section="dashboard-home"><a href="?">首页</a></li>
        <li class="nav-item" data-section="player-analyses"><a href="?section=player-analyses">角色分析管理</a></li>
        </ul></nav><main class="main-content"><section class="content-section active" id="dashboard-home">首页</section>''' + panel + '''</main></div></body></html>'''
    fixture = {'id': 7, 'revision': 1, 'player_id': 42,
               'player': {'id': 42, 'character_name': '真实角色测试名字' * 8, 'region': 'cn', 'realm': '测试服务器',
                          'class_name': 'Mage', 'spec_name': 'Fire', 'class_label': '法师', 'spec_label': '火焰',
                          'season_id': 3, 'season_name': '至暗之夜第1赛季'},
               'status': 'ready', 'review_status': 'pending', 'is_published': False, 'review_note': '',
               'source_kind': 'ai', 'manual': None, 'conclusion': '不应出现在列表的综合简述',
               'tags': [{'label': '组队模式', 'value': '五人固定车队', 'confidence': 'low', 'confidence_label': '低'},
                        {'label': '人工标签', 'value': '<script>不执行</script>'}],
               'ai': {'tags': [{'label': '固定队', 'value': '已核验', 'reason': '<script>不能执行</script>', 'evidence': []}],
                      'conclusion': '冻结结论', 'evidence': [{'text': '冻结证据', 'url': 'https://example.com/proof'}],
                      'result_json': {'fixture': True}, 'evidence_json': {}}, 'effective': {}}
    calls = []
    list_queries = []

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
            if path.endswith('/recheck/'):
                data = {'state': 'idle', 'message': ''}
            elif path.endswith('/7/'):
                data = fixture
            else:
                params = parse_qs(urlparse(request.url).query, keep_blank_values=True)
                list_queries.append(params)
                number = int(params['page'][0])
                data = {'items': [fixture if number == 1 else {**fixture, 'tags': []}], 'page': number,
                        'total': 21, 'page_size': 20, 'total_pages': 2,
                        'tag_options': ['组队模式', '人工标签', '其他页标签']}
            request_route.fulfill(json={'status': 'success', 'data': data})
        elif path == '/dashboard/':
            request_route.fulfill(body=html, content_type='text/html')
        else:
            request_route.abort()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/*', route)
        page.goto('http://analysis-ui.test/dashboard/')
        # Load the actual router after DOMContentLoaded, avoiding unrelated
        # Dashboard widgets in this isolated component/API fixture.
        page.add_script_tag(path=str(ROOT / 'static/dashboard/js/main.js'))
        page.evaluate('initNavigation(); initDashboardTheme();')
        assert calls == [], calls
        page.locator('[data-section="player-analyses"] a').click()
        page.locator('[data-action="open"]').wait_for()
        assert page.url.endswith('?section=player-analyses')
        assert calls == [('/dashboard/api/player-analyses/', 'GET')], calls
        listing = page.locator('.pa-results')
        text = listing.inner_text()
        assert '法师 火焰' in text and '至暗之夜第1赛季' in text
        assert not any(value in text for value in ('Mage', 'Fire', '赛季 3', '综合简述', '冻结结论'))
        assert listing.locator('.pa-list-tag').all_text_contents() == ['组队模式 · 五人固定车队 · 置信度：低', '人工标签 · <script>不执行</script>']
        assert listing.locator('script').count() == 0
        tag_filter = page.locator('.pa-filters [data-field="tag"]')
        assert tag_filter.locator('option').all_text_contents() == ['全部', '组队模式', '人工标签', '其他页标签']
        tag_filter.select_option('组队模式')
        with page.expect_response(lambda r: '/player-analyses/?' in r.url):
            page.locator('.pa-filters button[type="submit"]').click()
        with page.expect_response(lambda r: '/player-analyses/?' in r.url):
            listing.locator('[data-action="next"]').click()
        page.get_by_text('暂无标签', exact=True).wait_for()
        assert list_queries[-1]['tag'] == ['组队模式'] and list_queries[-1]['page'] == ['2']
        assert tag_filter.input_value() == '组队模式'
        with page.expect_response(lambda r: '/player-analyses/?' in r.url):
            listing.locator('[data-action="prev"]').click()
        listing.locator('.pa-list-tag').first.wait_for()
        for width in (1440, 390):
            page.set_viewport_size({'width': width, 'height': 1000})
            for theme in ('light', 'dark'):
                page.evaluate('(theme) => document.documentElement.dataset.dashboardTheme = theme', theme)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, theme)
                assert listing.evaluate('(n) => n.scrollWidth <= n.clientWidth'), (width, theme)
                assert listing.locator('.pa-list-tag').first.is_visible()
        page.locator('[data-action="open"]').click()
        page.locator('.pa-detail h2').wait_for()
        assert page.locator('.pa-detail').inner_text().find('冻结证据') >= 0
        assert page.locator('.pa-detail script').count() == 0
        for width in (1440, 390):
            page.set_viewport_size({'width': width, 'height': 1000})
            for theme in ('light', 'dark'):
                page.evaluate('(theme) => document.documentElement.dataset.dashboardTheme = theme', theme)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, theme)
                assert page.locator('[data-section="player-analyses"] a').is_visible()
                assert page.locator('[data-action="save"]').is_visible()
                colors = page.locator('.pa-detail').evaluate('(node) => [getComputedStyle(node).backgroundColor, getComputedStyle(node).color]')
                assert colors[0] != colors[1]
                box = page.locator('.pa-detail').bounding_box()
                assert box is not None
                assert box['y'] == 0 and box['height'] == 1000, box
                assert box['width'] == (width if width == 390 else 800), box
                assert page.locator('.pa-detail').evaluate('(n) => n.scrollWidth <= n.clientWidth'), (width, theme)
                assert page.locator('[role="dialog"]').count() == 1
        page.locator('.pa-edit-form [data-field="review_status"]').select_option('approved')
        page.locator('[data-action="save"]').click()
        try:
            page.get_by_role('status').filter(has_text='已保存').wait_for(timeout=5000)
        except Exception:
            print({'calls': calls, 'status': page.locator('.pa-message').text_content(), 'errors': errors})
            raise
        patch_index = calls.index(('/dashboard/api/player-analyses/7/', 'PATCH'))
        assert calls[patch_index + 1] == ('/dashboard/api/player-analyses/7/', 'GET')
        page.locator('[data-action="edit-manual"]').click()
        page.locator('[data-action="add-tag"]').click()
        page.locator('.pa-tag-editor [data-action="add-evidence"]').click()
        assert page.locator('[data-evidence-field="text"]').is_visible()
        page.locator('[data-field="conclusion"]').fill('保留未保存草稿')
        before_return = len(calls)
        # Full-screen mobile modal intentionally covers navigation. A history
        # navigation still hides it and must retain its exact unsaved DOM.
        page.evaluate('history.replaceState({dashboardLocation:{section:"dashboard-home"}}, "", "/dashboard/"); activateDashboardLocation();')
        page.locator('.pa-detail').wait_for(state='hidden')
        page.locator('[data-section="player-analyses"] a').click()
        assert page.locator('[data-field="conclusion"]').input_value() == '保留未保存草稿'
        page.wait_for_timeout(100)
        assert calls[before_return:] == [('/dashboard/api/player-analyses/7/recheck/', 'GET')]
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert page.evaluate('Object.keys(localStorage).length') == 0
        assert errors == [], errors
        page.once('dialog', lambda dialog: dialog.dismiss())
        page.keyboard.press('Escape')
        assert page.locator('.pa-detail').is_visible()
        page.once('dialog', lambda dialog: dialog.accept())
        page.keyboard.press('Escape')
        page.locator('.pa-detail').wait_for(state='hidden')
        assert page.locator('[data-action="open"]').evaluate('(n) => n === document.activeElement')
        browser.close()
    print(json.dumps({'browser': 'Chromium', 'viewports': [1440, 390], 'themes': ['light', 'dark'],
                      'overflow': False, 'page_errors': errors, 'lazy_detail': True,
                      'csrf_and_readback': True, 'manual_form': True,
                      'localized_identity_and_real_season': True, 'tags_and_filter_pagination': True,
                      'no_list_conclusion': True, 'empty_tags': True,
                      'spa_return_keeps_draft': True, 'fixture_scope': 'panel/router, not full site'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
