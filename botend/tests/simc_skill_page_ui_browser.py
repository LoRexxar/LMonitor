"""Actual template + production panel/query JS in Chromium; controlled API fixtures.

This is local DOM regression, not authenticated production QA. Normal cases use
real browser fetch intercepted at the transport boundary; the race case alone
uses a deliberately abort-insensitive deferred fetch to prove the generation
fence independently of the browser's AbortController implementation.
Install/run instructions: requirements-simc-ui-test.txt (repository root).
"""
import copy
import json
import os
import re
import subprocess
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
INDEX = {
    'id': 52, 'identity': {}, 'spec_count': 32, 'action_count': 152211,
    'actor_index': [{'id': 471, 'class_name': 'warrior', 'specialization': 'fury',
                     'hero_talent_trees': [{'id': 60, 'name_zh': '屠戮者'},
                                           {'id': 61, 'name_zh': '山丘领主'}]}],
    'actors': [],
}


def snapshot(names=('技能10', '技能2'), more=False, matched=None, cursor='signed-next'):
    data = copy.deepcopy(INDEX)
    data['actors'] = [{
        'class': 'warrior', 'specialization': 'fury',
        'hero_talent_trees': data['actor_index'][0]['hero_talent_trees'],
        'actions': [{'display_name': name, 'spell_id': i + 1, 'variant': {},
                     'product': {'final_normalized_damage': i}}
                    for i, name in enumerate(names)],
        'global_skill_effects': [{'display_name': '全局激怒', 'effect_details': []}],
    }]
    data['actor_page'] = {
        'counts': {'stored_actions': 152211, 'scope_candidates': 100,
                   'matched': len(names) if matched is None else matched},
        'facets': {'talents': [{'key': 'talent:10', 'label': '远页天赋',
                               'description': '所有页的条件'}], 'buffs': []},
        'page': {'returned': len(names), 'has_more': more,
                 'next_cursor': cursor if more else None},
    }
    return data


class SkillPageDOM(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.page.set_default_timeout(4000)
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.requests = []
        html = (ROOT / 'templates/dashboard/index.html').read_text()
        # Do not run unrelated dashboard scripts or external assets. The DOM is
        # the actual template, including all real selects/buttons/checkboxes.
        html = re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.S | re.I)
        html = re.sub(r'<link\b[^>]*>', '', html, flags=re.I)
        self.context.route('**/*', lambda route: route.fulfill(status=200, body=''))
        self.context.route('**/api/simc-skill-damage/**', lambda route: self.requests.append(route))
        self.page.goto('http://simc-ui.test/')
        self.page.set_content(html)
        self.page.add_style_tag(content='.hidden { display: none !important; }')
        self.page.evaluate("""() => {
            let el = document.getElementById('simc-skill-damage-panel');
            while (el) { el.classList.remove('hidden'); el.style.removeProperty('display'); el = el.parentElement; }
            window.showMessage = () => {};
            window.escapeHtml = value => String(value).replaceAll('&', '&amp;')
                .replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
        }""")
        self.page.add_script_tag(content=(ROOT / 'static/dashboard/js/simc_skill_query.js').read_text())
        source = (ROOT / 'static/dashboard/js/main.js').read_text()
        source = source[source.index('function renderSimcSkillIdentity('):
                        source.index('function initSimcBackendUploadTool(')]
        self.page.add_script_tag(content=source)

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [], 'uncaught browser errors')

    def el(self, suffix):
        return self.page.locator('#simc-skill-damage-' + suffix)

    def await_requests(self, count):
        # Playwright dispatches route callbacks while pumping browser events.
        for _ in range(80):
            if len(self.requests) >= count:
                return
            self.page.wait_for_timeout(10)
        self.fail(f'expected {count} outbound requests, got {len(self.requests)}')

    def reply(self, data, route=None, status=200):
        (route or self.requests[-1]).fulfill(status=status, content_type='application/json',
            body=json.dumps({'success': True, 'data': {'snapshot': data, 'job': None}}))
        self.page.wait_for_timeout(30)

    def start(self):
        self.page.evaluate('initSimcSkillDamagePanel()')
        self.await_requests(1)
        self.assertEqual(parse_qs(urlparse(self.requests[0].request.url).query), {'index': ['1']})
        self.reply(INDEX)
        self.el('spec').select_option('warrior:fury')
        self.page.wait_for_timeout(30)
        self.assertEqual(len(self.requests), 1, 'spec alone must not fetch actor')
        self.el('hero-tree').select_option('60')
        self.await_requests(2)

    def params(self):
        return {key: value[0] for key, value in parse_qs(
            urlparse(self.requests[-1].request.url).query, keep_blank_values=True).items()}

    def freeze_clock(self):
        # Virtualize browser timers, not fetch/results or production retry policy.
        self.page.clock.install(time=1000000)
        self.page.clock.pause_at(1000000)

    def busy(self):
        self.requests[-1].fulfill(status=503, content_type='application/json', body=json.dumps({
            'success': False, 'code': 'reader_busy', 'error': '技能快照正在读取，请稍后重试'}))
        self.page.wait_for_timeout(30)

    def test_reader_busy_latest_query_recovers_after_scan_slot_release(self):
        self.freeze_clock()
        self.page.evaluate("""() => {
            const realFetch = window.fetch.bind(window);
            window.pageSignals = [];
            window.fetch = (url, options = {}) => {
                if (String(url).includes('view=actor_page')) pageSignals.push(options.signal);
                return realFetch(url, options);
            };
        }""")
        self.start()
        # Aborting an old client does not release its server scan slot.
        self.el('target-tabs').locator('[data-target-count="2"]').click()
        self.await_requests(3)
        self.reply(snapshot(['过期目标']), route=self.requests[1])
        self.assertNotIn('过期目标', self.el('body').inner_text())
        coordinates = self.params()
        started = self.page.evaluate('Date.now()')
        self.busy()
        self.page.clock.run_for(999)
        self.assertEqual(len(self.requests), 3, 'busy must back off, not spin')
        self.page.clock.run_for(1)
        self.await_requests(4)
        self.assertEqual(self.params(), coordinates)
        self.busy()
        self.page.clock.run_for(1999)
        self.assertEqual(len(self.requests), 4, 'second wait uses backoff')
        self.page.clock.run_for(1)
        self.await_requests(5)
        self.assertEqual(self.params(), coordinates)
        self.busy()
        # Keep the original server scan busy for >40s of virtual browser time.
        # Neither the production backoff constants nor network are bypassed.
        for retry in range(2, 8):
            self.page.clock.run_for(min(1000 * 2 ** retry, 8000))
            self.await_requests(6 + retry - 2)
            self.assertEqual(self.params(), coordinates)
            if retry < 7:
                self.busy()
        self.assertGreaterEqual(self.page.evaluate('Date.now()') - started, 40000)
        self.assertTrue(self.page.evaluate('''pageSignals[0].aborted
            && pageSignals.slice(1).every(signal => signal === pageSignals[1] && !signal.aborted)'''))
        self.reply(snapshot(['槽释放后最新目标']))
        self.assertIn('槽释放后最新目标', self.el('body').inner_text())
        self.assertIn('已全部显示', self.el('list-status').inner_text())
        self.assertEqual(self.params()['target_count'], '2')
        count = len(self.requests)
        self.page.clock.run_for(120000)
        self.page.wait_for_timeout(30)
        self.assertEqual(len(self.requests), count, 'successful retry must end the loop')

    def test_reader_busy_covers_backend_worker_deadline_and_cleanup(self):
        self.freeze_clock()
        self.start()
        self.busy()
        elapsed = 0
        retry = 0
        # The real scan already took 141s; the worker can legitimately hold
        # the slot for its 300s deadline plus cleanup. The browser clock alone
        # is accelerated: the production retry policy is exercised unchanged.
        while elapsed < 310000:
            delay = min(1000 * 2 ** retry, 8000)
            self.page.clock.run_for(delay)
            self.await_requests(3 + retry)
            elapsed += delay
            retry += 1
            if elapsed < 310000:
                self.busy()
        self.reply(snapshot(['完整扫描结束后恢复']))
        self.assertIn('完整扫描结束后恢复', self.el('body').inner_text())

    def test_reader_busy_wait_is_cancelled_when_latest_scope_changes(self):
        self.freeze_clock()
        self.start()
        self.busy()
        self.assertIn('等待并自动重试', self.el('list-status').inner_text())
        self.el('hero-tree').select_option('61')
        self.await_requests(3)
        self.reply(snapshot(['最新英雄']))
        self.page.clock.run_for(120000)
        self.page.wait_for_timeout(30)
        self.assertEqual(len(self.requests), 3, 'cancelled wait must never retry stale coordinates')
        self.assertEqual(self.params()['hero_tree_id'], '61')
        self.assertIn('最新英雄', self.el('body').inner_text())
        # Also cancel without issuing a replacement request.
        self.el('hero-tree').select_option('60')
        self.await_requests(4)
        self.busy()
        self.el('spec').select_option('')
        self.page.clock.run_for(120000)
        self.page.wait_for_timeout(30)
        self.assertEqual(len(self.requests), 4)
        self.assertIn('请先选择专精', self.el('body').inner_text())

    def test_reader_busy_retries_are_bounded(self):
        self.freeze_clock()
        self.start()
        self.busy()
        for retry in range(42):
            self.page.clock.run_for(min(1000 * 2 ** retry, 8000))
            self.await_requests(3 + retry)
            self.busy()
        self.assertIn('尚未完整显示', self.el('list-status').inner_text())
        self.page.clock.run_for(120000)
        self.page.wait_for_timeout(30)
        self.assertEqual(len(self.requests), 44, 'busy exhaustion must stop automatic requests')
        self.assertEqual(self.el('page-next').count(), 0)

    def pending_index_control(self, control):
        self.page.evaluate('initSimcSkillDamagePanel()')
        self.await_requests(1)
        if control == 'search':
            self.el('search').fill('初始化搜索')
        elif control == 'sort-name':
            self.el(control).click()
        else:
            self.el(control).locator('[data-target-count="2"]').click()
        self.reply(INDEX, route=self.requests[0])
        self.assertEqual(self.el('spec').locator('option[value="warrior:fury"]').count(), 1,
                         'page invalidation must not discard pending index')
        self.el('spec').select_option('warrior:fury')
        self.el('hero-tree').select_option('60')
        self.await_requests(2)
        if control == 'search':
            self.assertEqual(self.params()['q'], '初始化搜索')
        elif control == 'sort-name':
            self.assertEqual(self.params()['sort'], 'name')
            self.assertEqual(self.params()['direction'], 'asc')
        else:
            self.assertEqual(self.params()['target_count'], '2')
        self.reply(snapshot(['初始化后可查询']))
        self.assertIn('初始化后可查询', self.el('body').inner_text())

    def test_pending_index_survives_search(self):
        self.pending_index_control('search')

    def test_pending_index_survives_sort(self):
        self.pending_index_control('sort-name')

    def test_pending_index_survives_target_change(self):
        self.pending_index_control('target-tabs')

    def test_index_refresh_still_fences_older_index_response(self):
        self.page.evaluate('initSimcSkillDamagePanel()')
        self.await_requests(1)
        self.el('refresh').click()
        self.await_requests(2)
        newer = copy.deepcopy(INDEX)
        newer['id'] = 53
        self.reply(newer)
        self.reply(INDEX, route=self.requests[0])
        self.el('spec').select_option('warrior:fury')
        self.el('hero-tree').select_option('60')
        self.await_requests(3)
        self.assertEqual(self.params()['snapshot_id'], '53')
        data = snapshot(['新索引'])
        data['id'] = 53
        self.reply(data)

    def test_controls_complete_facets_server_order_and_single_list(self):
        self.start()
        params = self.params()
        for key, value in {'view': 'actor_page', 'stream': '1', 'snapshot_id': '52', 'actor_id': '471',
                           'hero_tree_id': '60', 'target_count': '1', 'limit': '100'}.items():
            self.assertEqual(params[key], value)
        self.assertIn('加载', self.el('body').inner_text())
        self.reply(snapshot())
        text = self.el('body').inner_text()
        self.assertLess(text.index('技能10'), text.index('技能2'), 'never re-sort a server page')
        self.assertIn('远页天赋', self.el('filter-talents').inner_text())
        self.assertIn('全局激怒', self.el('global-modifiers').inner_text())
        self.assertIn('共 2 行，已全部显示', self.el('list-status').inner_text())
        self.assertEqual(self.el('page-next').count(), 0)
        self.assertEqual(self.el('page-prev').count(), 0)
        self.el('sort-name').click()
        self.await_requests(3)
        self.assertEqual(self.params()['sort'], 'name')
        self.assertEqual(self.params()['direction'], 'asc')
        self.assertNotIn('cursor', self.params())
        self.reply(snapshot())
        self.el('filter-talents').locator('input').uncheck()
        self.await_requests(4)
        self.assertEqual(json.loads(self.params()['exclude']), ['talent:10'])
        self.reply(snapshot())
        self.assertFalse(self.el('filter-talents').locator('input').is_checked())
        self.el('target-tabs').locator('[data-target-count="2"]').click()
        self.await_requests(5)
        self.assertEqual(self.params()['target_count'], '2')
        self.assertNotIn('cursor', self.params())
        self.reply(snapshot())
        self.el('search').fill('激怒')
        self.page.wait_for_timeout(100)
        self.assertEqual(len(self.requests), 5, 'query is debounced')
        self.await_requests(6)
        self.assertEqual(self.params()['q'], '激怒')
        self.assertEqual(self.params()['snapshot_id'], '52')
        self.reply(snapshot(['搜索结果']))
        self.assertIn('搜索结果', self.el('body').inner_text(), 'do not locally refilter server page')

    def test_bounded_error_and_unresolved_links(self):
        self.freeze_clock()
        self.start()
        data = snapshot(['可用技能'])
        data['actor_page']['unresolved'] = {'actor_id': 471, 'snapshot_id': 52,
                                           'availability': 'separate_document'}
        self.reply(data)
        self.assertIn('数量单独统计', self.el('unresolved').inner_text())
        params = parse_qs(urlparse(self.el('unresolved').locator('a').get_attribute('href')).query)
        self.assertEqual(params['view'], ['unresolved_fragment'])
        self.assertEqual(params['stream'], ['1'])
        self.assertEqual(params['fragment_limit'], ['16384'])
        self.el('sort-name').click()
        self.await_requests(3)
        self.requests[-1].fulfill(status=503, content_type='application/json', body=json.dumps({
            'success': False, 'error': '单条技能超过展示上限',
            'worker_error': {'source_ordinal': 123}, 'fragment_view': 'actor_fragment'}))
        self.page.wait_for_timeout(30)
        self.assertIn('原始序号 123', self.el('body').inner_text())
        params = parse_qs(urlparse(self.el('body').locator('a').get_attribute('href')).query)
        for key, value in {'view': 'actor_fragment', 'stream': '1', 'source_ordinal': '123',
                           'snapshot_id': '52', 'actor_id': '471', 'fragment_limit': '16384'}.items():
            self.assertEqual(params[key], [value])
        self.page.clock.run_for(120000)
        self.page.wait_for_timeout(30)
        self.assertEqual(len(self.requests), 3, 'non-busy 503 must not retry')

    def test_all_batches_are_automatically_displayed_together(self):
        self.page.set_viewport_size({'width': 1440, 'height': 1000})
        self.start()
        for batch,(start,end) in enumerate(((0,50),(50,100),(100,123))):
            names=[f'完整技能{i:03d}' for i in range(start,end)]
            data=snapshot(names,more=end<123,matched=123,cursor=f'batch-{batch+2}')
            data['actor_page']['source_ordinals']=list(range(start,end))
            self.reply(data)
            if end<123:
                self.await_requests(batch+3)
                self.assertEqual(self.params()['cursor'],f'batch-{batch+2}')
                self.assertIn(f'{end} / 123',self.el('list-status').inner_text())
        self.assertEqual(self.el('body').locator('tr').count(),123)
        text=self.el('body').inner_text()
        self.assertIn('完整技能000',text)
        self.assertIn('完整技能122',text)
        self.assertLess(text.index('完整技能049'),text.index('完整技能050'))
        self.assertIn('共 123 行，已全部显示',self.el('list-status').inner_text())
        self.assertEqual(self.el('page-next').count(),0)
        self.assertEqual(self.el('page-prev').count(),0)
        screenshots=os.environ.get('SIMC_UI_SCREENSHOT_DIR')
        if screenshots:
            directory=Path(screenshots)
            directory.mkdir(parents=True,exist_ok=True)
            self.page.screenshot(path=str(directory/'simc-single-page-desktop.png'))
            self.el('body').locator('tr').last.scroll_into_view_if_needed()
            last=self.el('body').locator('tr').last.bounding_box()
            self.assertGreater(last['y']+last['height'],0)
            self.assertLess(last['y'],1000)
            self.page.screenshot(path=str(directory/'simc-single-page-last-row.png'))
            self.page.set_viewport_size({'width':390,'height':844})
            self.el('list-status').scroll_into_view_if_needed()
            self.page.screenshot(path=str(directory/'simc-single-page-mobile.png'))

    def test_incomplete_or_repeated_continuation_never_claims_complete(self):
        self.start()
        self.reply(snapshot(['首批'],more=True,matched=3,cursor='repeat'))
        self.await_requests(3)
        self.reply(snapshot(['重复后续'],more=True,matched=3,cursor='repeat'))
        self.assertIn('尚未完整显示',self.el('list-status').inner_text())
        self.assertNotIn('已全部显示',self.el('list-status').inner_text())
        self.assertEqual(len(self.requests),3)

    def test_continuation_from_an_old_query_cannot_enter_the_new_list(self):
        self.page.evaluate('''() => {
            window.pending=[];
            window.fetch=(url,options={})=>new Promise(resolve=>pending.push({url,options,resolve}));
        }''')
        self.page.evaluate('initSimcSkillDamagePanel()')
        self.resolve_pending(0,INDEX)
        self.el('spec').select_option('warrior:fury')
        self.el('hero-tree').select_option('60')
        self.resolve_pending(1,snapshot(['旧查询首批'],more=True,matched=2))
        self.assertEqual(self.page.evaluate('pending.length'),3)
        self.el('hero-tree').select_option('61')
        self.assertTrue(self.page.evaluate('pending[2].options.signal.aborted'))
        self.resolve_pending(3,snapshot(['新查询完整列表']))
        self.resolve_pending(2,snapshot(['旧查询后续'],matched=2))
        self.assertIn('新查询完整列表',self.el('body').inner_text())
        self.assertNotIn('旧查询',self.el('body').inner_text())
        self.assertIn('共 1 行，已全部显示',self.el('list-status').inner_text())

    def test_browser_original_and_node_collation_agree(self):
        oracle = (ROOT / 'botend/tests/fixtures/simc_skill_query_original.cjs').read_text()
        self.page.add_script_tag(content=oracle.replace('module.exports =', 'window.originalOracle =', 1))
        names = ['技能2', '技能10', '技能02', '阿尔法', '激怒', '重击', '斩杀',
                 'beta', 'BETA', 'éclair', 'eclair', 'ＥＣＬＡＩＲ', '技能-2', '技能 2',
                 '技能1.5', '技能１', '𠮷', '龙', '龍', '']
        rows = [{'display_name': name, 'name': 'fallback', 'spell_id': i,
                 'variant': {}, 'product': {'final_normalized_damage': [None, 3, '4', False, -1][i % 5]}}
                for i, name in enumerate(names)]
        queries = [{'hero_tree_id': '60', 'target_count': '1', 'sort': sort,
                    'direction': direction, 'q': '', 'exclude': []}
                   for sort in ['name', 'final'] for direction in ['asc', 'desc']]
        script = """const fs = require('node:fs');
            const oracle = require('./botend/tests/fixtures/simc_skill_query_original.cjs');
            const {rows, queries} = JSON.parse(fs.readFileSync(0, 'utf8'));
            console.log(JSON.stringify(queries.map(query => oracle(rows, query))));"""
        node = subprocess.run([os.environ.get('SIMC_UI_NODE', 'node'), '-e', script],
            cwd=ROOT, input=json.dumps({'rows': rows, 'queries': queries}),
            text=True, capture_output=True, check=True, timeout=10)
        expected = json.loads(node.stdout)
        actual = self.page.evaluate('''({rows, queries}) => queries.map(query => originalOracle(rows, query))''',
                                    {'rows': rows, 'queries': queries})
        self.assertEqual(actual, expected, 'zh-CN numeric/base Node and browser original ordering')
        shared = self.page.evaluate('''({rows, queries}) => queries.map(query => {
            const facets = SimcSkillQuery.createFacets();
            const selected = rows.map((row, index) => {
                const candidate = SimcSkillQuery.scanCandidate(row, query, facets);
                return candidate && SimcSkillQuery.matchCandidate(candidate, query, index);
            }).filter(Boolean).sort((left, right) => SimcSkillQuery.compareRows(left, right, query));
            return {actions: selected.map(row => row.action), facets: SimcSkillQuery.serializeFacets(facets)};
        })''', {'rows': rows, 'queries': queries})
        self.assertEqual(shared, expected, 'shared browser query preserves frozen original')

    def test_scope_filters_and_snapshot_refresh_reset(self):
        self.start()
        self.reply(snapshot())
        self.el('filter-talents').locator('input').uncheck()
        self.await_requests(3)
        self.reply(snapshot())
        self.el('hero-tree').select_option('61')
        self.await_requests(4)
        self.assertEqual(self.params()['exclude'], '[]', 'filters are hero-scoped')
        self.reply(snapshot())
        self.el('hero-tree').select_option('60')
        self.await_requests(5)
        self.assertEqual(json.loads(self.params()['exclude']), ['talent:10'])
        self.reply(snapshot())
        self.assertFalse(self.el('filter-talents').locator('input').is_checked())
        self.el('refresh').click()
        self.await_requests(6)
        newer = copy.deepcopy(INDEX)
        newer['id'] = 53
        self.reply(newer)
        self.await_requests(7)
        self.assertEqual(self.params()['snapshot_id'], '53')
        self.assertEqual(self.params()['exclude'], '[]', 'new snapshot drops old exclusions')
        self.assertNotIn('cursor', self.params(), 'new snapshot cannot reuse old cursor')
        data = snapshot(['新快照'])
        data['id'] = 53
        self.reply(data)
        self.assertIn('已全部显示', self.el('list-status').inner_text())
        self.assertIn('新快照', self.el('body').inner_text())
        self.assertTrue(self.el('filter-talents').locator('input').is_checked())

    def test_generation_fence_ignores_abort_insensitive_transport_and_scope_reset(self):
        self.page.evaluate("""() => {
            window.pending = [];
            window.fetch = (url, options = {}) => new Promise(resolve =>
                window.pending.push({url, options, resolve}));
        }""")
        self.page.evaluate('initSimcSkillDamagePanel()')
        self.resolve_pending(0, INDEX)
        self.el('spec').select_option('warrior:fury')
        self.el('hero-tree').select_option('60')
        self.el('hero-tree').select_option('61')
        self.assertTrue(self.page.evaluate('pending[1].options.signal.aborted'))
        self.resolve_pending(2, snapshot(['新英雄']))
        self.resolve_pending(1, snapshot(['过期英雄']))
        self.assertIn('新英雄', self.el('body').inner_text())
        self.assertNotIn('过期英雄', self.el('body').inner_text())
        self.el('search').fill('稍后')
        before = self.page.evaluate('pending.length')
        self.el('spec').select_option('')
        self.page.wait_for_timeout(350)
        self.assertEqual(self.page.evaluate('pending.length'), before)
        self.assertIn('请先选择专精', self.el('body').inner_text())
        self.assertTrue(self.el('list-progress').is_hidden())

    def resolve_pending(self, index, data):
        self.page.evaluate("""([index, snapshot]) => pending[index].resolve({ok: true,
            json: async () => ({success: true, data: {snapshot, job: null}})})""", [index, data])
        self.page.wait_for_timeout(20)


if __name__ == '__main__':
    unittest.main(verbosity=2)
