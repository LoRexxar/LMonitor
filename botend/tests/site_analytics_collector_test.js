const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto').webcrypto;
const source = fs.readFileSync(path.join(__dirname, '../../static/analytics/collector.js'), 'utf8');

function browser({storage = new Map(), blocked = false, frame = false} = {}) {
    let clock = 100000000;
    const requests = [], listeners = {};
    const window = {};
    window.top = window;
    window.self = frame ? {} : window;
    const location = {pathname: '/portal/news/', search: '?secret=abc'};
    const navigate = url => {
        const next = new URL(url, 'https://example.org');
        location.pathname = next.pathname; location.search = next.search;
    };
    const context = vm.createContext({window, location, crypto, URL, URLSearchParams,
        Date: class extends Date {static now() {return clock;}},
        document: {readyState: 'complete', visibilityState: 'visible', referrer: 'https://search.example/query?secret=abc'},
        history: {pushState: (state, title, url) => navigate(url), replaceState: (state, title, url) => navigate(url)},
        localStorage: {getItem: key => {if (blocked) throw new Error('存储被禁用'); return storage.get(key);}, setItem: (key, value) => {if (blocked) throw new Error('存储被禁用'); storage.set(key, value);}},
        fetch: (url, options) => {requests.push({url, body: JSON.parse(options.body)}); return Promise.resolve({ok: true});},
        queueMicrotask: fn => fn(), addEventListener: (name, fn) => {listeners[name] = fn;},
    });
    vm.runInContext(source, context);
    return {context, requests, listeners, advance: milliseconds => {clock += milliseconds;}, rerun: () => vm.runInContext(source, context)};
}

test('首屏仅上报一次，来源和路径不包含敏感参数', () => {
    const view = browser(); view.rerun();
    assert.equal(view.requests.length, 1);
    assert.equal(view.requests[0].body.path, '/portal/news/');
    assert.equal(view.requests[0].body.referrer, 'https://search.example');
});

test('查询参数和锚点变化不重复计数，后台栏目变化计数', () => {
    const view = browser();
    view.context.history.replaceState({}, '', '/portal/news/?token=xyz#top');
    assert.equal(view.requests.length, 1);
    view.context.history.pushState({}, '', '/dashboard/?section=site-analytics&token=xyz');
    view.context.history.pushState({}, '', '/dashboard/?section=user-management');
    assert.equal(view.requests.length, 3);
    assert.equal(view.requests[1].body.path, '/dashboard/?section=site-analytics');
    assert.notEqual(view.requests[0].body.event_id, view.requests[1].body.event_id);
});

test('会话在三十分钟无页面访问后重建，访客标识保持稳定', () => {
    const view = browser(); const first = view.requests[0].body;
    view.advance(29 * 60000); view.context.history.pushState({}, '', '/one/');
    assert.equal(view.requests[1].body.session_id, first.session_id);
    view.advance(31 * 60000); view.context.history.pushState({}, '', '/two/');
    assert.notEqual(view.requests[2].body.session_id, first.session_id);
    assert.equal(view.requests[2].body.visitor_id, first.visitor_id);
});

test('刷新和同源新标签沿用访客与会话', () => {
    const storage = new Map(); const first = browser({storage}), second = browser({storage});
    assert.equal(first.requests[0].body.visitor_id, second.requests[0].body.visitor_id);
    assert.equal(first.requests[0].body.session_id, second.requests[0].body.session_id);
    assert.notEqual(first.requests[0].body.event_id, second.requests[0].body.event_id);
});

test('禁用本地存储时仍可上报并在当前页面保持标识', () => {
    const view = browser({blocked: true}); view.context.history.pushState({}, '', '/one/');
    assert.equal(view.requests.length, 2);
    assert.equal(view.requests[0].body.visitor_id, view.requests[1].body.visitor_id);
    assert.equal(view.requests[0].body.session_id, view.requests[1].body.session_id);
});

test('浏览器缓存恢复计为新访问，普通 pageshow 不重复计数', () => {
    const view = browser(); view.listeners.pageshow({persisted: false});
    assert.equal(view.requests.length, 1);
    view.listeners.pageshow({persisted: true});
    assert.equal(view.requests.length, 2);
});

test('嵌入页面不上报，避免父子页面重复计算', () => {
    assert.equal(browser({frame: true}).requests.length, 0);
});
