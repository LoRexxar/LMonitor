"""使用线上公开样本、真实模板和脚本，在隔离浏览器比较首屏及刷新功能。

首次加 --capture 获取公开只读接口样本；后续直接复用样本。
固定刷新响应延迟用于验证界面是否阻塞，不代表生产数据库优化后的耗时。
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'LMonitor.settings_test_sqlite')
import django
django.setup()
from django.template import Context, Engine
from playwright.async_api import async_playwright
from botend.services.gear_catalog_projection import compact_equipment_payload
from botend.services.variant_text import catalog_text

OUTPUT = ROOT / 'tmp' / 'gear-startup-20261023'
OUTPUT.mkdir(parents=True, exist_ok=True)


def capture():
    import requests
    session = requests.Session()
    session.trust_env = False
    base = 'https://wowdaily.cn'
    def get(path):
        response = session.get(base + path, timeout=30)
        response.raise_for_status()
        return response.json()
    bootstrap = get('/portal/api/gear-builder/bootstrap/')
    catalog = get('/portal/api/gear-builder/catalog/?class=Warrior&spec=Fury&slot=head&snapshot=1')
    item = catalog['items'][0]
    payload = dict(v=4, c='Warrior', s='Fury', b=catalog['catalog']['batch_key'], current=True, strict=True,
                   e=[['head', item['item_id'], item['variants'][0]['key'], [], 0, [], 0, 0]])
    response = session.post(base + '/portal/api/gear-builder/resolve-share/', json=payload, timeout=30)
    response.raise_for_status()
    (OUTPUT / 'public-sample.json').write_text(json.dumps(dict(
        bootstrap=bootstrap, catalog=catalog, refreshed=response.json()), ensure_ascii=False), encoding='utf-8')


async def run():
    sample = json.loads((OUTPUT / 'public-sample.json').read_text(encoding='utf-8'))
    current = compact_equipment_payload(sample['refreshed'])
    print(json.dumps({'原响应字节': len(json.dumps(sample['refreshed'], ensure_ascii=False).encode()),
                      '精简响应字节': len(json.dumps(current, ensure_ascii=False).encode())}, ensure_ascii=False))
    old_js = subprocess.check_output(['git', 'show', 'bb08e739:static/portal/js/gear_builder.js'], cwd=ROOT).decode('utf-8')
    html = Engine.get_default().get_template('portal/gear_builder.html').render(Context({
        'request': SimpleNamespace(path='/portal/gear-builder/', user=SimpleNamespace(is_authenticated=False))}))
    results = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel='msedge', headless=True)
        for baseline in (True, False):
            context = await browser.new_context(viewport={'width': 1440, 'height': 1000})
            initial = dict(className='Warrior', specName='Fury', batchKey=sample['catalog']['catalog']['batch_key'],
                           equipment=sample['refreshed']['equipment'], lockedSlots=[])
            await context.add_init_script('localStorage.setItem("wowdaily:gear-builder:v1:Warrior:Fury", '
                                          + json.dumps(json.dumps(initial)) + ');')
            page = await context.new_page()
            errors, calls = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            refreshed = asyncio.Event()
            async def route(request):
                path = urlparse(request.request.url).path
                calls.append(path)
                if path == '/portal/gear-builder/':
                    await request.fulfill(body=html, content_type='text/html')
                elif path.startswith('/static/'):
                    file = ROOT / path.lstrip('/')
                    if path == '/static/portal/js/gear_builder.js' and baseline:
                        await request.fulfill(body=old_js, content_type='application/javascript')
                    elif file.is_file():
                        await request.fulfill(path=str(file))
                    else:
                        await request.fulfill(status=404, body='')
                elif path.endswith('/bootstrap/'):
                    await asyncio.sleep(.15)
                    await request.fulfill(json=sample['bootstrap'])
                elif path.endswith('/resolve-share/'):
                    await asyncio.sleep(2.5)
                    await request.fulfill(json=sample['refreshed'] if baseline else current)
                    refreshed.set()
                elif path.endswith('/catalog/'):
                    await asyncio.sleep(.1)
                    await request.fulfill(json=sample['catalog'] if baseline else catalog_text(sample['catalog']))
                else:
                    await request.fulfill(json={'success': True, 'items': []})
            await page.route('**/*', route)
            started = time.perf_counter()
            await page.goto('http://gear-startup.test/portal/gear-builder/')
            await page.locator('#gear-slot-list [data-slot]').first.wait_for()
            slots_ms = round((time.perf_counter() - started) * 1000)
            await page.locator('.gear-candidate-row').first.wait_for()
            catalog_ms = round((time.perf_counter() - started) * 1000)
            assert await page.locator('#gear-slot-list [data-slot]').count() == 16
            original_stats = await page.locator('#gear-stat-grid').inner_text()
            if not baseline:
                assert not refreshed.is_set(), '刷新结束前就应当可以操作装备列表'
            await refreshed.wait()
            await page.wait_for_function("document.querySelector('.gear-builder-page').getAttribute('aria-busy') === 'false'")
            # 等待响应后的绘制完成，比较真实属性面板。
            await page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
            stats = await page.locator('#gear-stat-grid').inner_text()
            assert stats == original_stats
            await page.locator('#gear-slot-list [data-slot="head"]').click()
            await page.locator('.gear-candidate-row').nth(1).click()
            selected_name = await page.locator('.gear-candidate-row.is-equipped .gear-candidate-title').inner_text()
            assert selected_name in await page.locator('#gear-slot-list').inner_text()
            await page.screenshot(path=str(OUTPUT / ('baseline.png' if baseline else 'optimized.png')), full_page=True)
            assert not errors, errors
            assert not any('tailwind' in url for url in calls)
            results.append(dict(版本='基线' if baseline else '优化', 槽位显示毫秒=slots_ms,
                                目录显示毫秒=catalog_ms, 属性面板=stats, 页面错误=errors))
            await context.close()
        await browser.close()
    assert results[0]['属性面板'] == results[1]['属性面板']
    assert results[0]['槽位显示毫秒'] - results[1]['槽位显示毫秒'] > 1500
    (OUTPUT / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(results, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--capture', action='store_true')
    options = parser.parse_args()
    if options.capture:
        capture()
    asyncio.run(run())
