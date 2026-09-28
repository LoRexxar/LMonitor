/* 全站采集：仅记录顶层页面，不发送标题、查询参数、账号或原始 IP。 */
(() => {
    'use strict';
    if (window.__siteAnalytics || window.top !== window.self) return;
    window.__siteAnalytics = true;
    const endpoint = '/api/site-analytics/collect/';
    const memory = {};
    const uuid = () => crypto.randomUUID ? crypto.randomUUID() : '10000000-1000-4000-8000-100000000000'.replace(/[018]/g, c => (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16));
    function read(key) {
        try { return localStorage.getItem(key) || memory[key]; } catch (_) { return memory[key]; }
    }
    function write(key, value) {
        memory[key] = value;
        try { localStorage.setItem(key, value); } catch (_) { /* 禁用存储时退回当前页面内存。 */ }
    }
    const visitor = read('lm.analytics.visitor') || uuid();
    write('lm.analytics.visitor', visitor);
    let current = '';
    function path() {
        let value = location.pathname;
        const section = new URLSearchParams(location.search).get('section');
        if (value === '/dashboard/' && section && /^[a-z0-9-]{1,64}$/.test(section)) value += '?section=' + section;
        return value;
    }
    function collect(force = false) {
        if (document.visibilityState === 'prerender') return;
        const next = path();
        if (!force && next === current) return;
        current = next;
        const now = Date.now();
        let session = read('lm.analytics.session');
        if (!session || now - Number(read('lm.analytics.active') || 0) > 30 * 60 * 1000) session = uuid();
        write('lm.analytics.session', session);
        write('lm.analytics.active', String(now));
        let referrer = '';
        try { referrer = document.referrer ? new URL(document.referrer).origin : ''; } catch (_) {}
        const data = {event_id: uuid(), visitor_id: visitor, session_id: session, path: next, referrer};
        fetch(endpoint, {method: 'POST', credentials: 'same-origin', keepalive: true,
            headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)}).catch(() => {});
    }
    for (const name of ['pushState', 'replaceState']) {
        const original = history[name];
        history[name] = function (...args) {
            const result = original.apply(this, args);
            queueMicrotask(() => collect());
            return result;
        };
    }
    addEventListener('popstate', () => collect());
    addEventListener('pageshow', event => { if (event.persisted) collect(true); });
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => collect(), {once: true});
    else collect();
})();
