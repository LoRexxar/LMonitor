"""本地榜单组件验收：真实模板、样式和脚本，接口为隔离样本，不访问线上。"""
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'LMonitor.settings_test_sqlite')
import django
django.setup()
from django.template.loader import render_to_string
from playwright.sync_api import sync_playwright
from botend.services.player_observation_badges import high_confidence_badges


def main():
    rendered = render_to_string('portal/spec_detail/player_list.html', {
        'class_name': 'Mage', 'spec_name': 'Fire', 'nav': {'class_cn': '法师', 'spec_cn': '火焰'}})
    panel = re.search(r'<section id="module-players".*?</section>', rendered, re.S).group()
    html = '''<!doctype html><html><head><meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <link rel="stylesheet" href="/static/portal/css/portal.css">
        <link rel="stylesheet" href="/static/portal/css/spec-overview.css">
        <style>body{margin:0;font-family:Arial,"Microsoft YaHei",sans-serif}</style>
        <script defer src="/static/portal/js/spec-overview.js"></script></head>
        <body class="portal-bg"><main id="spec-overview" class="spec-overview">''' + panel + '</main></body></html>'
    def badge(code, label, confidence='高', level='strong'):
        return dict(code=code, value=label, supported=True,
                    evidence_strength=dict(level=level, confidence=confidence, scope='近期公开样本支持，详情可查看依据。'))
    strong = high_confidence_badges(dict(status='ready', source_kind='ai', display_judgements=[
        badge('carrying', '4带1协助上分模式'), badge('team', '固定队核心'), badge('helper', '高频协作')]))
    weak = high_confidence_badges(dict(status='ready', source_kind='ai', display_judgements=[
        badge('excluded', '已排除', level='excluded'), badge('weak', '待补证', confidence='低', level='partial')]))
    players = [dict(id=i, rank=i, character_name=['清风逐月', '霜火之心', '很长的角色名称' * 8][(i-1) % 3],
        realm='银月', region='cn', item_level=180, score=4000-i*10,
        detail_url=f'/portal/character/?player_id={i}', observation_badges=strong if i % 3 else weak)
        for i in range(1, 21)]
    # 多标签压力样本只用于验证全部展示与换行，不表示这些行为结论可以同时成立。
    players[0]['observation_badges'] = strong + [
        dict(code=f'layout-{i}', label=label, confidence='高') for i, label in enumerate(
            ('稳定配合', '重复协作记录', '近期公开样本中的持续协作表现', '较长标签的完整文字展示'))]
    calls = []
    def route(r):
        path = urlparse(r.request.url).path
        if path.startswith('/static/'):
            r.fulfill(body=(ROOT / path.lstrip('/')).read_text(encoding='utf-8'),
                content_type='text/css' if path.endswith('.css') else 'text/javascript')
        elif path.endswith('/players/'):
            calls.append(path)
            r.fulfill(json={'players': players, 'updated_at': '2026-10-27T10:00:00+08:00'})
        elif path == '/portal/character/':
            r.fulfill(body='<meta charset="UTF-8"><h1>角色详情入口</h1>', content_type='text/html; charset=utf-8')
        elif path == '/portal/spec/Mage/Fire/':
            r.fulfill(body=html, content_type='text/html')
        else:
            r.abort()
    output = Path(os.environ.get('BADGE_SCREENSHOT_DIR', ROOT / 'tmp' / 'player-badge-preview'))
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/*', route)
        for width in (1440, 390):
            for theme in ('light', 'dark'):
                page.set_viewport_size({'width': width, 'height': 1000})
                page.goto('http://badge-ui.test/portal/spec/Mage/Fire/')
                page.locator('#module-players[data-state="ready"]').wait_for()
                page.evaluate('(dark) => document.documentElement.classList.toggle("portal-theme-dark", dark)', theme == 'dark')
                rows = page.locator('.spec-module-row')
                assert rows.count() == 20
                assert rows.nth(0).locator('.spec-player-badge').all_text_contents() == [
                    item['label'] for item in players[0]['observation_badges']]
                assert rows.nth(1).locator('.spec-player-badge').count() == len(strong)
                assert page.locator('.spec-player-badge-more').count() == 0
                assert rows.nth(2).locator('.spec-player-badge').count() == 0
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert '高置信度' in rows.nth(0).locator('.spec-player-badge').first.get_attribute('title')
                assert page.locator('.spec-player-badge').evaluate_all('elements => elements.every(e => e.scrollWidth <= e.clientWidth + 2)')
                page.screenshot(path=str(output / f'Top20-{width}-{theme}.png'))
                rows.nth(0).locator('.spec-player-badge').last.click()
                page.wait_for_url('**/portal/character/?player_id=1')
                assert page.locator('h1').inner_text() == '角色详情入口'
        assert len(calls) == 4
        assert not errors, errors
        browser.close()
    print(f'桌面、手机、明暗主题及标签跳转全部通过；截图：{output}')


if __name__ == '__main__':
    main()
