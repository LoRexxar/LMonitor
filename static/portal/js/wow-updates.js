(function () {
    'use strict';

    const search = document.getElementById('wow-updates-search');
    const clear = document.getElementById('wow-updates-clear');
    const sections = {
        states: {url: '/portal/api/wow-skill-diff/states/', node: document.getElementById('wow-skill-diff-states'), items: null},
        reports: {url: '/portal/api/wow-skill-diffs/', node: document.getElementById('wow-skill-diff-list'), items: null},
    };

    function element(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function safeUrl(raw) {
        const value = String(raw || '').trim();
        if (!value || value === '-' || value === '#') return '';
        try {
            const url = new URL(value, window.location.origin);
            return ['https:', 'http:'].includes(url.protocol) ? url.href : '';
        } catch (_) {
            return '';
        }
    }

    function filtered(items) {
        const query = search.value.trim().toLowerCase();
        return query ? items.filter(item => Object.values(item).join(' ').toLowerCase().includes(query)) : items;
    }

    function message(container, text) {
        container.replaceChildren(element('p', 'wow-updates-message', text));
    }

    function addLink(container, label, raw, external) {
        const href = safeUrl(raw);
        if (!href) return;
        const link = element('a', '', label);
        link.href = href;
        if (external) {
            link.target = '_blank';
            link.rel = 'noopener noreferrer';
        }
        container.appendChild(link);
    }

    function stateRow(item, hotfix) {
        const prefix = hotfix ? 'hotfix_' : '';
        const row = element('article', 'wow-updates-state');
        const identity = element('div', 'wow-updates-identity');
        identity.append(
            element('h3', '', hotfix ? 'Hotfix' : item.branch || ''),
            element('span', 'wow-updates-build', hotfix ? `#${item.hotfix_push_id}` : item.build || '-')
        );
        const detail = element('div', 'wow-updates-detail');
        const badges = element('div', 'wow-updates-badges');
        const runFailed = item[`${prefix}last_run_status`] === '异常';
        badges.appendChild(element('span', `wow-updates-badge ${runFailed ? 'is-error' : 'is-normal'}`, runFailed ? '异常' : '正常'));
        const event = String(item[`${prefix}last_event_status`] || '');
        if (event) {
            const hasUpdate = event.includes('有职业更新');
            badges.appendChild(element('span', `wow-updates-badge${hasUpdate ? ' is-update' : ''}`, hasUpdate ? (hotfix ? 'Hotfix 有更新' : '有职业更新') : event));
        }
        detail.appendChild(badges);
        const summary = item[`${prefix}summary_title`];
        if (summary) detail.appendChild(element('p', 'wow-updates-summary', summary));
        const times = element('div', 'wow-updates-times');
        for (const [key, label] of [['last_run_at', '心跳'], ['last_event_at', '事件时间']]) {
            const value = item[`${prefix}${key}`];
            if (value) times.appendChild(element('span', '', `${label}：${value}`));
        }
        detail.appendChild(times);
        const actions = element('div', 'wow-updates-actions');
        addLink(actions, hotfix ? 'Hotfix' : '报告', item[`${prefix}report_url`], false);
        addLink(actions, hotfix ? 'Hotfix Wago' : 'Wago', hotfix ? item.hotfix_wago_url : item.wago_diff_url, true);
        row.append(identity, detail, actions);
        return row;
    }

    function renderStates() {
        const {node, items} = sections.states;
        if (items === null) return;
        if (!items.length) return message(node, '暂无服务器监控配置');
        const rows = filtered(items);
        if (!rows.length) return message(node, '无匹配结果');
        node.replaceChildren();
        const hotfix = rows.reduce((latest, item) => Number(item.hotfix_push_id || 0) > Number(latest.hotfix_push_id || 0) ? item : latest);
        if (Number(hotfix.hotfix_push_id || 0) > 0) node.appendChild(stateRow(hotfix, true));
        rows.slice(0, 12).forEach(item => node.appendChild(stateRow(item, false)));
    }

    function renderReports() {
        const {node, items} = sections.reports;
        if (items === null) return;
        if (!items.length) return message(node, '暂无数据');
        const rows = filtered(items);
        if (!rows.length) return message(node, '无匹配结果');
        node.replaceChildren();
        rows.slice(0, 20).forEach(item => {
            const row = element('article', 'wow-updates-report');
            const url = safeUrl(item.url);
            const title = element(url ? 'a' : 'span', '', item.title || '');
            if (url) title.href = url;
            row.appendChild(title);
            const time = String(item.time || '').replaceAll('\n', ' ').trim();
            if (time) row.appendChild(element('time', '', time));
            node.appendChild(row);
        });
    }

    async function load(key) {
        const section = sections[key];
        section.items = null;
        section.node.setAttribute('aria-busy', 'true');
        message(section.node, key === 'states' ? '正在加载服务器状态…' : '正在加载更新报告…');
        try {
            const response = await fetch(section.url, {headers: {'Accept': 'application/json'}});
            if (!response.ok) throw new Error('加载失败');
            const payload = await response.json();
            if (!Array.isArray(payload.data)) throw new Error('数据格式无效');
            section.items = payload.data;
            if (key === 'states') renderStates();
            else renderReports();
        } catch (_) {
            message(section.node, key === 'states' ? '服务器状态暂时无法加载。' : '更新报告暂时无法加载。');
            const retry = element('button', '', '重试');
            retry.type = 'button';
            retry.addEventListener('click', () => load(key));
            section.node.firstElementChild.appendChild(retry);
        } finally {
            section.node.setAttribute('aria-busy', 'false');
        }
    }

    function searchChanged() {
        clear.hidden = !search.value;
        renderStates();
        renderReports();
    }
    search.addEventListener('input', searchChanged);
    clear.addEventListener('click', () => {
        search.value = '';
        searchChanged();
        search.focus();
    });
    load('states');
    load('reports');
}());
