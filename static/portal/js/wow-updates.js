(function () {
    'use strict';

    const search = document.getElementById('wow-updates-search');
    const clear = document.getElementById('wow-updates-clear');
    const hotfixSearch = document.getElementById('wow-hotfix-search');
    const hotfixBranch = document.getElementById('wow-hotfix-branch');
    const hotfixBuild = document.getElementById('wow-hotfix-build');
    const hotfixTable = document.getElementById('wow-hotfix-table');
    const hotfixMode = document.getElementById('wow-hotfix-mode');
    const hotfixList = document.getElementById('wow-hotfix-list');
    const hotfixPagination = document.getElementById('wow-hotfix-pagination');
    const statesSection = document.getElementById('wow-updates-states-section');
    const tabs = Array.from(document.querySelectorAll('[data-updates-tab]'));
    let hotfixPage = 1;
    let hotfixLoaded = false;
    let hotfixRequest = null;
    let hotfixQueryTimer = null;
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
    function hotfixMessage(text) {
        const row = element('tr');
        const cell = element('td', 'wow-updates-message', text);
        cell.colSpan = 3;
        row.append(cell);
        hotfixList.replaceChildren(row);
        return cell;
    }
    function updateHotfixOptions(select, values, emptyLabel) {
        if (!Array.isArray(values)) return;
        const selected = select.value;
        const options = [new Option(emptyLabel, '')];
        for (const value of values) {
            const text = String(value);
            if (text && !options.some(option => option.value === text)) options.push(new Option(text, text));
        }
        if (selected && !options.some(option => option.value === selected)) options.push(new Option(selected, selected));
        select.replaceChildren(...options);
        select.value = selected;
    }
    function hotfixField(field) {
        const fact = element('span', 'wow-hotfix-field');
        const key = String(field.key ?? '-');
        const label = String(field.label || key);
        const identity = element('span', 'wow-hotfix-field-key', label.includes(key) ? label : `${label} / ${key}`);
        fact.append(identity);
        const value = String(field.text || field.after || '-');
        const verifiedPair = (field.before !== null && field.before !== undefined && field.before !== '')
            || field.comparison === 'client_baseline';
        const arrow = verifiedPair ? value.indexOf(' → ') : -1;
        if (arrow >= 0) {
            fact.append(element('span', 'wow-hotfix-field-before', value.slice(0, arrow)),
                element('span', 'wow-hotfix-field-arrow', '→'),
                element('span', 'wow-hotfix-field-text', value.slice(arrow + 3)));
        } else {
            fact.append(element('span', 'wow-hotfix-field-text', value));
        }
        return fact;
    }
    function renderHotfixEntries(payload) {
        const entries = payload.data;
        const meta = payload.meta;
        updateHotfixOptions(hotfixBuild, meta.builds, '全部构建');
        updateHotfixOptions(hotfixTable, meta.tables, '全部表');
        hotfixList.replaceChildren();
        if (!entries.length) hotfixMessage('没有匹配的 Hotfix 来源记录');
        entries.forEach(item => {
            const row = element('tr');
            const subject = element('td', 'wow-hotfix-subject');
            subject.append(element('strong', '', String(item.title || '对象未解析')));
            subject.append(element('span', 'wow-hotfix-record-id',
                `${item.table || '-'} #${item.record_id ?? '-'}`));
            if (item.spell_id !== null && item.spell_id !== undefined && item.spell_id !== '') {
                subject.append(element('span', 'wow-hotfix-record-id', ` · SpellID ${item.spell_id}`));
            }
            const fields = element('td', 'wow-hotfix-changes-cell');
            const flow = element('div', 'wow-hotfix-changes');
            fields.append(flow);
            const facts = Array.isArray(item.fields) ? item.fields : [];
            if (facts.length && item.kind !== 'status') {
                facts.slice(0, 5).forEach(field => flow.append(hotfixField(field)));
                if (facts.length > 5) {
                    const more = element('details', 'wow-hotfix-more');
                    more.append(element('summary', '', `展开其余 ${facts.length - 5} 个字段`));
                    const rest = element('div', 'wow-hotfix-more-changes');
                    facts.slice(5).forEach(field => rest.append(hotfixField(field)));
                    more.append(rest);
                    flow.append(more);
                }
            }
            else flow.append(element('span', 'wow-hotfix-secondary', 'data=null；无可解码字段'));
            const source = element('td', 'wow-hotfix-source');
            const sourceTop = element('div', 'wow-hotfix-source-top');
            const fullBuild = String(item.build || '').trim();
            const shortBuild = fullBuild.includes('.') ? fullBuild.slice(fullBuild.lastIndexOf('.') + 1) : fullBuild;
            const build = element('span', 'wow-hotfix-build', `build ${shortBuild || '未知'}`);
            if (fullBuild) build.title = fullBuild;
            sourceTop.append(element('strong', '', `push ${item.push ?? '-'}`), build);
            source.append(sourceTop, element('span', 'wow-hotfix-secondary',
                `${item.branch || '-'} · ${item.region_name || '区域未核实'} / ${item.locale || '-'}`));
            if (item.time) source.append(element('time', 'wow-hotfix-secondary', String(item.time)));
            const kind = ['change', 'new_value', 'status', 'unresolved'].includes(item.kind) ? item.kind : 'unresolved';
            const labels = {change: '确证变化', new_value: '本次配置', status: '来源状态', unresolved: '未解析'};
            const actions = element('div', 'wow-hotfix-source-actions');
            actions.append(element('span', `wow-hotfix-kind is-${kind}`, String(item.status_label || labels[kind])));
            const links = element('div', 'wow-hotfix-links');
            addLink(links, '报告', item.report_url, false);
            addLink(links, 'Wago', item.source_url, true);
            const sourceDetails = element('details', 'wow-hotfix-source-details');
            sourceDetails.append(element('summary', '', '来源'), links);
            actions.append(sourceDetails);
            source.append(actions);
            row.append(subject, fields, source);
            hotfixList.append(row);
        });
        hotfixPagination.replaceChildren();
        hotfixPagination.hidden = !meta.total;
        if (meta.total) {
            const previous = element('button', '', '上一页');
            previous.type = 'button';
            previous.disabled = !meta.has_previous;
            previous.addEventListener('click', () => loadHotfixEntries(meta.page - 1));
            const next = element('button', '', '下一页');
            next.type = 'button';
            next.disabled = !meta.has_next;
            next.addEventListener('click', () => loadHotfixEntries(meta.page + 1));
            hotfixPagination.append(previous,
                element('span', '', `第 ${meta.page} / ${meta.total_pages} 页 · 共 ${meta.total} 条来源记录`), next);
        }
    }
    async function loadHotfixEntries(page) {
        if (hotfixRequest) hotfixRequest.abort();
        const controller = new AbortController();
        hotfixRequest = controller;
        const params = new URLSearchParams({mode: hotfixMode.value, page: String(page), page_size: '20'});
        if (hotfixSearch.value.trim()) params.set('q', hotfixSearch.value.trim());
        if (hotfixBranch.value) params.set('branch', hotfixBranch.value);
        if (hotfixBuild.value) params.set('build', hotfixBuild.value);
        if (hotfixTable.value) params.set('table', hotfixTable.value);
        hotfixList.setAttribute('aria-busy', 'true');
        hotfixPagination.hidden = true;
        hotfixMessage('正在查询 Hotfix 来源记录…');
        try {
            const response = await fetch(`/portal/api/hotfix-entries/?${params}`, {
                headers: {'Accept': 'application/json'}, signal: controller.signal,
            });
            if (!response.ok) throw new Error('加载失败');
            const payload = await response.json();
            if (hotfixRequest !== controller) return;
            if (!Array.isArray(payload.data) || !payload.meta) throw new Error('数据格式无效');
            hotfixPage = payload.meta.page;
            hotfixLoaded = true;
            renderHotfixEntries(payload);
        } catch (error) {
            if (error.name === 'AbortError' || hotfixRequest !== controller) return;
            hotfixMessage('Hotfix 来源记录暂时无法加载。');
            const retry = element('button', '', '重试');
            retry.type = 'button';
            retry.addEventListener('click', () => loadHotfixEntries(page));
            hotfixList.firstElementChild.firstElementChild.append(retry);
        } finally {
            if (hotfixRequest === controller) {
                hotfixRequest = null;
                hotfixList.setAttribute('aria-busy', 'false');
            }
        }
    }
    function activateTab(name) {
        if (statesSection) statesSection.hidden = name === 'hotfix';
        tabs.forEach(tab => {
            const active = tab.dataset.updatesTab === name;
            tab.setAttribute('aria-selected', String(active));
            tab.tabIndex = active ? 0 : -1;
            document.getElementById(tab.getAttribute('aria-controls')).hidden = !active;
        });
        if (name === 'hotfix' && !hotfixLoaded && !hotfixRequest) loadHotfixEntries(hotfixPage);
    }
    tabs.forEach((tab, index) => {
        tab.addEventListener('click', () => activateTab(tab.dataset.updatesTab));
        tab.addEventListener('keydown', event => {
            const offset = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
            if (!offset && event.key !== 'Home' && event.key !== 'End') return;
            event.preventDefault();
            const target = event.key === 'Home' ? tabs[0] : event.key === 'End' ? tabs[tabs.length - 1] : tabs[(index + offset + tabs.length) % tabs.length];
            activateTab(target.dataset.updatesTab);
            target.focus();
        });
    });
    function hotfixFilterChanged() {
        clearTimeout(hotfixQueryTimer);
        if (hotfixRequest) {
            hotfixRequest.abort();
            hotfixRequest = null;
        }
        hotfixQueryTimer = setTimeout(() => loadHotfixEntries(1), 280);
    }
    hotfixSearch.addEventListener('input', hotfixFilterChanged);
    [hotfixBranch, hotfixBuild, hotfixTable, hotfixMode].forEach(filter => filter.addEventListener('change', () => {
        clearTimeout(hotfixQueryTimer);
        loadHotfixEntries(1);
    }));
    load('states');
    load('reports');
    if (new URLSearchParams(window.location.search).get('tab') === 'hotfix') activateTab('hotfix');
}());
