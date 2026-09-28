(() => {
    'use strict';
    const init = () => {
        const root = document.getElementById('site-analytics');
        if (!root) return;
        const el = id => document.getElementById('sa-' + id);
        let data = null, sequence = 0, loaded = false, settingsDirty = false;
        const format = value => Number(value || 0).toLocaleString('zh-CN');
        // 初始日期由服务端按站点时区返回。
        function table(target, rows, columns) {
            const host = el(target);
            host.replaceChildren();
            if (!rows.length) { const empty = document.createElement('p'); empty.className = 'sa-empty'; empty.textContent = '所选范围暂无访问数据'; host.append(empty); return; }
            const scroll = document.createElement('div'); scroll.className = 'sa-scroll';
            const node = document.createElement('table'); node.className = 'sa-table';
            const head = node.createTHead().insertRow();
            columns.forEach(([label]) => { const th = document.createElement('th'); th.textContent = label; th.scope = 'col'; head.append(th); });
            const body = node.createTBody();
            rows.forEach(row => { const tr = body.insertRow(); columns.forEach(([, key], index) => { tr.insertCell().textContent = index ? format(row[key]) : String(row[key]); }); });
            scroll.append(node); host.append(scroll);
        }
        function chart(target, rows, metric) {
            const host = el(target); host.replaceChildren();
            if (!rows.some(row => row[metric])) { host.classList.add('sa-empty'); host.textContent = '所选范围暂无访问数据'; return; }
            host.classList.remove('sa-empty');
            const bars = document.createElement('div'); bars.className = 'sa-bars';
            const max = Math.max(...rows.map(row => row[metric]), 1);
            rows.forEach((row, index) => {
                const cell = document.createElement('div'); cell.className = 'sa-bar-cell'; cell.tabIndex = 0;
                cell.title = `${row.label}：${format(row[metric])}`; cell.setAttribute('aria-label', cell.title);
                const bar = document.createElement('div'); bar.className = 'sa-bar'; bar.style.height = `${row[metric] / max * 85}%`;
                const label = document.createElement('span'); label.textContent = rows.length <= 10 || index % Math.ceil(rows.length / 8) === 0 ? row.label.slice(-5) : '';
                cell.append(bar, label); bars.append(cell);
            });
            host.append(bars);
        }
        function render() {
            el('metrics').replaceChildren();
            [['浏览量 PV', 'pv'], ['访客 UV', 'uv'], ['独立 IP', 'ips'], ['会话数', 'sessions'], ['人均浏览页数', 'pv_per_visitor'], ['每次会话页数', 'pv_per_session'], ['多次浏览访客', 'repeat_visitors'], ['最近 15 分钟访客', 'active_visitors']].forEach(([label, key]) => {
                const card = document.createElement('div'); card.className = 'sa-metric';
                const title = document.createElement('span'); title.textContent = label;
                const number = document.createElement('strong'); number.textContent = format(data.summary[key]);
                card.append(title, number); el('metrics').append(card);
            });
            const columns = [['页面 / 分组', 'label'], ['PV', 'pv'], ['UV', 'uv'], ['IP', 'ips'], ['会话', 'sessions']];
            table('daily', data.daily, [['日期', 'label'], ...columns.slice(1)]);
            for (const key of ['pages', 'sources', 'devices', 'browsers', 'systems', 'areas', 'authentication']) {
                const rows = data[key].map(row => ({...row, label: key === 'sources' ? row.label || '直接访问 / 来源未知' : key === 'authentication' ? row.label ? '已登录' : '未登录' : row.label}));
                table(key, rows, columns);
            }
            for (const key of ['visitor', 'ip']) table(key + '-frequency', data[key + '_frequency'], [['访问次数', 'label'], [key === 'ip' ? 'IP 数' : '访客数', 'count']]);
            chart('trend', data.daily, el('chart-metric').value);
            chart('hourly', data.hourly, 'pv');
            if (!settingsDirty) {
                el('enabled').checked = data.config.enabled;
                el('excluded').value = data.config.excluded_prefixes.join('\n');
                el('proxies').value = data.config.trusted_proxy_cidrs.join('\n');
            }
            el('status').textContent = `${data.config.enabled ? '采集中' : '采集已暂停'} · ${data.filters.start} 至 ${data.filters.end}`;
        }
        async function load() {
            const requestId = ++sequence;
            const params = new URLSearchParams();
            for (const key of ['start', 'end', 'area', 'path']) if (el(key).value) params.set(key, el(key).value);
            el('results').setAttribute('aria-busy', 'true'); el('error').hidden = true;
            try {
                const response = await fetch(root.dataset.apiUrl + '?' + params, {credentials: 'same-origin', cache: 'no-store'});
                const result = await response.json();
                if (requestId !== sequence) return;
                if (!response.ok) throw new Error(result.message || '读取统计失败');
                data = result; loaded = true;
                root.dataset.timezone = result.timezone;
                el('start').value = result.filters.start; el('end').value = result.filters.end;
                render();
            } catch (error) {
                if (requestId !== sequence) return;
                el('error').textContent = error.message; el('error').hidden = false;
                el('status').textContent = '查询失败，当前数据未更新';
            } finally { if (requestId === sequence) el('results').setAttribute('aria-busy', 'false'); }
        }
        el('filters').addEventListener('submit', event => { event.preventDefault(); load(); });
        root.querySelectorAll('[data-days]').forEach(button => button.addEventListener('click', () => {
            // 采用站点时区的今日日期，避免客户端时区改变查询范围。
            const today = new Intl.DateTimeFormat('sv-SE', {timeZone: root.dataset.timezone || 'Asia/Shanghai'}).format(new Date());
            const start = new Date(today + 'T12:00:00Z'); start.setUTCDate(start.getUTCDate() - Number(button.dataset.days) + 1);
            el('end').value = today; el('start').value = start.toISOString().slice(0, 10); load();
        }));
        el('chart-metric').addEventListener('change', () => { if (data) chart('trend', data.daily, el('chart-metric').value); });
        el('config').addEventListener('input', () => { settingsDirty = true; });
        el('config').addEventListener('submit', async event => {
            event.preventDefault(); el('save').disabled = true; el('save-status').textContent = '保存中…';
            const lines = key => el(key).value.split('\n').map(value => value.trim()).filter(Boolean);
            try {
                const response = await fetch(root.dataset.apiUrl, {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-CSRFToken': root.querySelector('[name=csrfmiddlewaretoken]').value}, body: JSON.stringify({enabled: el('enabled').checked, excluded_prefixes: lines('excluded'), trusted_proxy_cidrs: lines('proxies')})});
                const result = await response.json(); if (!response.ok) throw new Error(result.message || '保存失败');
                settingsDirty = false; el('save-status').textContent = result.message; await load();
            } catch (error) { el('save-status').textContent = error.message; }
            finally { el('save').disabled = false; }
        });
        const show = () => { if (!loaded && (root.classList.contains('active') || root.style.display === 'block')) { loaded = true; load(); } };
        new MutationObserver(show).observe(root, {attributes: true, attributeFilter: ['class', 'style']});
        show();
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
