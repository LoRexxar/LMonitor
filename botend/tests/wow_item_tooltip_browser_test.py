"""Run with Python Playwright; exercises the actual shared JS/CSS in Chromium."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]


def main():
    receipts = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        for width, height, count in [(1280, 577, 12), (1280, 577, 80), (390, 664, 80), (320, 400, 80)]:
            page = browser.new_page(viewport={"width": width, "height": height})
            page.set_content('<button id="item" style="position:fixed;top:45%;left:25%" data-wow-item-tooltip-name="测试装备">装备名称</button>')
            page.locator('#item').evaluate('(el, n) => el.dataset.wowItemTooltip = Array.from({length:n}, (_,i)=>`装备：第${i}条完整说明 A & B < C`).join("\\n")', count)
            page.add_style_tag(path=str(ROOT / 'static/shared/css/wow-item-tooltip.css'))
            page.add_script_tag(path=str(ROOT / 'static/shared/js/wow-item-tooltip.js'))
            page.locator('#item').hover()
            tip = page.locator('.wow-item-tooltip')
            tip.wait_for(state='visible')
            rect = tip.bounding_box()
            assert rect is not None
            assert rect['x'] >= 8 and rect['y'] >= 8, rect
            assert rect['x'] + rect['width'] <= width - 7, rect
            assert rect['y'] + rect['height'] <= height - 7, rect
            trigger = page.locator('#item').bounding_box()
            assert trigger is not None
            assert rect['y'] >= trigger['y'] + trigger['height'] or rect['y'] + rect['height'] <= trigger['y']
            assert tip.locator('span').count() == count
            assert (tip.locator('span').first.text_content() or '').endswith('A & B < C')
            if count == 80:
                page.mouse.move(rect['x'] + rect['width'] / 2, rect['y'] + 50)
                page.wait_for_timeout(180)
                assert tip.is_visible()
                page.mouse.wheel(0, 3000)
                page.wait_for_function("document.querySelector('.wow-item-tooltip').scrollTop > 0")
                assert tip.is_visible()
                tip.evaluate('(el)=>el.scrollTop=el.scrollHeight')
                page.wait_for_timeout(80)
                last = tip.locator('span').last.bounding_box()
                assert last is not None
                assert last['y'] + last['height'] <= height - 7, last
            page.keyboard.press('Escape')
            assert not tip.is_visible()
            page.locator('#item').focus()
            assert tip.is_visible()
            page.set_viewport_size({'width': width, 'height': max(240, height - 100)})
            page.wait_for_function("(() => {const r=document.querySelector('.wow-item-tooltip').getBoundingClientRect();return r.bottom <= innerHeight - 7;})()")
            rect = tip.bounding_box()
            assert rect is not None
            assert rect['y'] + rect['height'] <= max(240, height - 100) - 7, rect
            page.mouse.move(width-1, height-1)
            page.locator('#item').evaluate('(el)=>el.blur()')
            page.wait_for_timeout(180)
            assert not tip.is_visible()
            receipts.append({'viewport': [width, height], 'lines': count, 'passed': True})
            page.close()
        context = browser.new_context(viewport={'width': 390, 'height': 664}, has_touch=True, is_mobile=True)
        page = context.new_page()
        page.set_content('<meta name="viewport" content="width=device-width,initial-scale=1"><button id="item" style="position:fixed;top:40%;left:25%" data-wow-item-tooltip-name="触屏装备">装备名称</button>')
        page.locator('#item').evaluate('(el) => el.dataset.wowItemTooltip = Array.from({length:80}, (_,i)=>`装备：第${i}条完整说明`).join("\\n")')
        page.add_style_tag(path=str(ROOT / 'static/shared/css/wow-item-tooltip.css'))
        page.add_script_tag(path=str(ROOT / 'static/shared/js/wow-item-tooltip.js'))
        page.locator('#item').tap()
        tip = page.locator('.wow-item-tooltip')
        tip.wait_for(state='visible')
        rect = tip.bounding_box()
        assert rect is not None
        cdp = context.new_cdp_session(page)
        x, y = rect['x'] + 100, rect['y'] + rect['height'] - 30
        cdp.send('Input.dispatchTouchEvent', {'type':'touchStart', 'touchPoints':[{'x':x,'y':y}]})
        for distance in [20, 50, 90, 140]:
            cdp.send('Input.dispatchTouchEvent', {'type':'touchMove', 'touchPoints':[{'x':x,'y':y-distance}]})
            page.wait_for_timeout(50)
        cdp.send('Input.dispatchTouchEvent', {'type':'touchEnd', 'touchPoints':[]})
        page.wait_for_timeout(250)
        assert tip.is_visible(), 'touch scroll/leave must keep tooltip open'
        assert tip.evaluate('(el)=>el.scrollTop') > 0
        page.touchscreen.tap(5, 5)
        assert not tip.is_visible()
        receipts.append({'viewport':[390,664], 'touch_scroll_and_dismiss':True, 'passed':True})
        context.close()
        browser.close()
    print(json.dumps(receipts))


if __name__ == '__main__':
    main()
