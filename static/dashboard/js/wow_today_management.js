(function () {
    'use strict';

    const state = {
        records: [],
        snapshot: null,
        loaded: false,
        loading: false,
        dirty: false,
        search: '',
        expanded: new Set(),
    };

    function root() {
        return document.getElementById('wow-today-settings');
    }

    function escapeHtml(value) {
        return String(value ?? '')
            .replaceAll('&', '&amp;')
            .replaceAll('<', '&lt;')
            .replaceAll('>', '&gt;')
            .replaceAll('"', '&quot;')
            .replaceAll("'", '&#039;');
    }

    function csrfToken() {
        return document.querySelector('#csrf-form input[name="csrfmiddlewaretoken"]')?.value || '';
    }

    function safeHref(value) {
        try {
            const url = new URL(String(value || ''), window.location.origin);
            return ['http:', 'https:'].includes(url.protocol) ? url.href : '';
        } catch (_) {
            return '';
        }
    }

    function setMessage(message, tone) {
        const node = document.getElementById('wow-today-settings-message');
        if (!node) return;
        node.textContent = message || '';
        node.className = 'hidden border-b px-5 py-3 text-sm';
        if (!message) return;
        node.classList.remove('hidden');
        if (tone === 'error') node.classList.add('border-red-100', 'bg-red-50', 'text-red-700');
        else if (tone === 'success') node.classList.add('border-emerald-100', 'bg-emerald-50', 'text-emerald-700');
        else node.classList.add('border-blue-100', 'bg-blue-50', 'text-blue-700');
    }

    function setBusy(isBusy) {
        state.loading = isBusy;
        ['wow-today-settings-save', 'wow-today-settings-refresh', 'wow-today-settings-reset']
            .forEach(id => {
                const button = document.getElementById(id);
                if (button) button.disabled = isBusy;
            });
    }

    function renderMeta() {
        const summary = document.getElementById('wow-today-settings-summary');
        const snapshot = document.getElementById('wow-today-settings-snapshot');
        const cardTotal = state.records.reduce((total, section) => total + section.cards.length, 0);
        const effectiveVisible = state.records.reduce((total, section) => (
            total + (section.is_visible ? section.cards.filter(card => card.is_visible).length : 0)
        ), 0);
        if (summary) {
            const suffix = state.dirty ? ' · 有未保存更改' : '';
            summary.textContent = `${state.records.length} 个板块 · ${cardTotal} 张卡片 · ${effectiveVisible} 张生效${suffix}`;
        }
        if (snapshot) {
            snapshot.textContent = state.snapshot
                ? `${state.snapshot.region_name} · ${state.snapshot.game_version_name} · ${state.snapshot.expansion_name} · 快照 ${state.snapshot.snapshot_date}`
                : '尚无抓取快照';
        }
    }

    function itemMatches(value, query) {
        return String(value || '').toLocaleLowerCase('zh-CN').includes(query);
    }

    function cardMatches(card, query) {
        if (!query) return true;
        return [card.source_name, card.display_name, card.effective_name, card.key, card.kind_label]
            .some(value => itemMatches(value, query))
            || (card.preview_items || []).some(value => itemMatches(value, query));
    }

    function cardTemplate(section, card) {
        const index = section.cards.indexOf(card);
        const displayName = card.display_name || '';
        const effectiveName = displayName || card.source_name;
        const preview = (card.preview_items || []).join(' · ');
        const sourceUrl = safeHref(card.source_url);
        const sourceHeading = sourceUrl
            ? `<a href="${escapeHtml(sourceUrl)}" target="_blank" rel="noopener noreferrer" class="font-semibold text-gray-900 hover:text-blue-700 hover:underline">${escapeHtml(effectiveName)}</a>`
            : `<strong class="font-semibold text-gray-900">${escapeHtml(effectiveName)}</strong>`;
        return `
            <article class="px-4 py-3 sm:px-5" data-wow-today-card-key="${escapeHtml(card.key)}">
                <div class="grid min-w-0 gap-3 md:grid-cols-[minmax(0,1fr)_minmax(13rem,18rem)_auto] md:items-center">
                    <div class="min-w-0">
                        <div class="flex min-w-0 flex-wrap items-center gap-2">
                            ${sourceHeading}
                            <span class="rounded-md bg-slate-100 px-2 py-0.5 text-sm text-slate-600 md:text-xs">${escapeHtml(card.kind_label)}</span>
                            <span class="text-sm text-gray-500 md:text-xs">${card.item_count} 条</span>
                        </div>
                        ${preview ? `<p class="mt-1 truncate text-sm text-gray-600" title="${escapeHtml(preview)}">${escapeHtml(preview)}</p>` : ''}
                        <p class="mt-1 break-all font-mono text-sm text-gray-500 md:text-xs">${escapeHtml(card.key)}</p>
                    </div>
                    <label class="block min-w-0">
                        <span class="mb-1 block text-sm font-medium text-gray-600 md:text-xs">卡片显示名称</span>
                        <input
                            type="text"
                            maxlength="150"
                            value="${escapeHtml(displayName)}"
                            placeholder="${escapeHtml(card.source_name)}"
                            data-wow-today-card-name
                            class="min-h-11 w-full min-w-0 rounded-lg border border-gray-300 px-3 py-2 text-sm text-gray-900 placeholder:text-gray-500 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-100"
                        >
                    </label>
                    <div class="flex items-center justify-between gap-2 md:justify-end">
                        <label class="inline-flex min-h-11 cursor-pointer items-center gap-2 px-1 text-sm font-medium text-gray-700">
                            <input type="checkbox" data-wow-today-card-visible class="h-5 w-5 rounded border-gray-300 text-blue-600 focus:ring-blue-500" ${card.is_visible ? 'checked' : ''}>
                            <span>${card.is_visible ? '显示' : '隐藏'}</span>
                        </label>
                        <div class="inline-flex overflow-hidden rounded-lg border border-gray-300 bg-white">
                            <button type="button" data-wow-today-card-move="up" class="h-11 w-11 text-gray-600 hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-inset focus:ring-blue-200 disabled:cursor-not-allowed disabled:text-gray-300" aria-label="上移卡片 ${escapeHtml(effectiveName)}" ${state.search || index === 0 ? 'disabled' : ''}><i class="fas fa-arrow-up"></i></button>
                            <button type="button" data-wow-today-card-move="down" class="h-11 w-11 border-l border-gray-300 text-gray-600 hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-inset focus:ring-blue-200 disabled:cursor-not-allowed disabled:text-gray-300" aria-label="下移卡片 ${escapeHtml(effectiveName)}" ${state.search || index === section.cards.length - 1 ? 'disabled' : ''}><i class="fas fa-arrow-down"></i></button>
                        </div>
                    </div>
                </div>
            </article>`;
    }

    function sectionTemplate(section, visibleCards) {
        const index = state.records.indexOf(section);
        const displayName = section.display_name || '';
        const effectiveName = displayName || section.source_name;
        const expanded = state.expanded.has(section.key) || Boolean(state.search);
        const hiddenNote = section.is_visible ? '' : `
            <div class="border-b border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-900 sm:px-5">
                板块当前隐藏；下方卡片配置会保留，重新开启板块后生效。
            </div>`;
        return `
            <section class="overflow-hidden rounded-xl border border-gray-200 bg-white" data-wow-today-section-key="${escapeHtml(section.key)}">
                <div class="grid min-w-0 gap-3 px-4 py-3 sm:px-5 md:grid-cols-[minmax(0,1fr)_minmax(13rem,18rem)_auto] md:items-center">
                    <button type="button" data-wow-today-toggle-section class="flex min-h-11 min-w-0 items-center gap-3 rounded-lg text-left focus:outline-none focus:ring-2 focus:ring-blue-200" aria-expanded="${expanded ? 'true' : 'false'}">
                        <span class="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-blue-50 text-blue-700"><i class="fas fa-chevron-${expanded ? 'down' : 'right'}" aria-hidden="true"></i></span>
                        <span class="min-w-0">
                            <strong class="block truncate text-base text-gray-900">${escapeHtml(effectiveName)}</strong>
                            <span class="block text-sm text-gray-600">${section.card_count} 张卡片 · ${section.visible_card_count} 张开启</span>
                        </span>
                    </button>
                    <label class="block min-w-0">
                        <span class="mb-1 block text-sm font-medium text-gray-600 md:text-xs">板块显示名称</span>
                        <input type="text" maxlength="150" value="${escapeHtml(displayName)}" placeholder="${escapeHtml(section.source_name)}" data-wow-today-section-name class="min-h-11 w-full min-w-0 rounded-lg border border-gray-300 px-3 py-2 text-sm text-gray-900 placeholder:text-gray-500 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-100">
                    </label>
                    <div class="flex items-center justify-between gap-2 md:justify-end">
                        <label class="inline-flex min-h-11 cursor-pointer items-center gap-2 px-1 text-sm font-medium text-gray-700">
                            <input type="checkbox" data-wow-today-section-visible class="h-5 w-5 rounded border-gray-300 text-blue-600 focus:ring-blue-500" ${section.is_visible ? 'checked' : ''}>
                            <span>${section.is_visible ? '显示板块' : '隐藏板块'}</span>
                        </label>
                        <div class="inline-flex overflow-hidden rounded-lg border border-gray-300 bg-white">
                            <button type="button" data-wow-today-section-move="up" class="h-11 w-11 text-gray-600 hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-inset focus:ring-blue-200 disabled:cursor-not-allowed disabled:text-gray-300" aria-label="上移板块 ${escapeHtml(effectiveName)}" ${state.search || index === 0 ? 'disabled' : ''}><i class="fas fa-arrow-up"></i></button>
                            <button type="button" data-wow-today-section-move="down" class="h-11 w-11 border-l border-gray-300 text-gray-600 hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-inset focus:ring-blue-200 disabled:cursor-not-allowed disabled:text-gray-300" aria-label="下移板块 ${escapeHtml(effectiveName)}" ${state.search || index === state.records.length - 1 ? 'disabled' : ''}><i class="fas fa-arrow-down"></i></button>
                        </div>
                    </div>
                </div>
                <div data-wow-today-section-body ${expanded ? '' : 'hidden'}>
                    ${hiddenNote}
                    <div class="grid grid-cols-[minmax(0,1fr)_auto] gap-3 border-y border-gray-200 bg-slate-50 px-4 py-2 text-sm font-medium text-gray-600 sm:px-5 md:text-xs">
                        <span>卡片内容与显示名称</span><span>显示 / 排序</span>
                    </div>
                    <div class="divide-y divide-gray-200">${visibleCards.map(card => cardTemplate(section, card)).join('')}</div>
                    ${visibleCards.length ? '' : '<div class="px-5 py-8 text-center text-sm text-gray-600">此板块中没有匹配的卡片。</div>'}
                </div>
            </section>`;
    }

    function render() {
        const list = document.getElementById('wow-today-settings-list');
        const empty = document.getElementById('wow-today-settings-empty');
        if (!list || !empty) return;
        const query = state.search.trim().toLocaleLowerCase('zh-CN');
        const groups = state.records.map(section => {
            const sectionMatch = [section.source_name, section.display_name, section.effective_name, section.key]
                .some(value => itemMatches(value, query));
            const cards = sectionMatch || !query
                ? section.cards
                : section.cards.filter(card => cardMatches(card, query));
            return { section, cards, visible: sectionMatch || cards.length > 0 };
        }).filter(group => group.visible);
        list.innerHTML = groups.map(group => sectionTemplate(group.section, group.cards)).join('');
        list.classList.toggle('hidden', groups.length === 0);
        empty.classList.toggle('hidden', groups.length > 0);
        renderMeta();
        const expand = document.getElementById('wow-today-settings-expand');
        if (expand) {
            const allExpanded = state.records.length > 0 && state.records.every(section => state.expanded.has(section.key));
            expand.querySelector('span').textContent = allExpanded ? '收起全部' : '展开全部';
            const icon = expand.querySelector('i');
            if (icon) icon.className = `fas ${allExpanded ? 'fa-angles-up' : 'fa-angles-down'} mr-2`;
        }
    }

    async function readResponse(response) {
        let data = null;
        try {
            data = await response.json();
        } catch (_) {
            throw new Error(`服务器返回了无法读取的响应（${response.status}）`);
        }
        if (!response.ok || !data.success) {
            throw new Error(data.error || data.message || `请求失败（${response.status}）`);
        }
        return data;
    }

    function acceptPayload(data) {
        const firstLoad = !state.loaded;
        state.records = Array.isArray(data.records)
            ? data.records.map(section => ({
                ...section,
                cards: Array.isArray(section.cards) ? section.cards.map(card => ({ ...card })) : [],
            }))
            : [];
        state.snapshot = data.snapshot || null;
        state.loaded = true;
        state.dirty = false;
        if (firstLoad) {
            state.expanded = new Set(state.records.filter(section => section.is_visible).map(section => section.key));
        } else {
            state.expanded = new Set(
                state.records.filter(section => state.expanded.has(section.key)).map(section => section.key)
            );
        }
        render();
    }

    async function load(force) {
        const section = root();
        if (!section || state.loading || (state.loaded && !force)) return;
        setBusy(true);
        setMessage('正在读取板块与卡片配置…', 'info');
        try {
            const response = await fetch(section.dataset.apiUrl, {
                credentials: 'same-origin',
                headers: { Accept: 'application/json' },
            });
            acceptPayload(await readResponse(response));
            setMessage('', 'info');
        } catch (error) {
            setMessage(error.message || '读取内容配置失败', 'error');
        } finally {
            setBusy(false);
        }
    }

    async function save() {
        const section = root();
        if (!section || state.loading || !state.records.length) return;
        setBusy(true);
        setMessage('正在保存板块与卡片配置…', 'info');
        try {
            const response = await fetch(section.dataset.apiUrl, {
                method: 'PATCH',
                credentials: 'same-origin',
                headers: {
                    Accept: 'application/json',
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken(),
                },
                body: JSON.stringify({
                    sections: state.records.map(sectionItem => ({
                        key: sectionItem.key,
                        display_name: sectionItem.display_name || '',
                        is_visible: Boolean(sectionItem.is_visible),
                        cards: sectionItem.cards.map(card => ({
                            key: card.key,
                            display_name: card.display_name || '',
                            is_visible: Boolean(card.is_visible),
                        })),
                    })),
                }),
            });
            acceptPayload(await readResponse(response));
            setMessage('内容编排已保存，Portal 下次读取时立即生效。', 'success');
        } catch (error) {
            setMessage(error.message || '保存内容配置失败', 'error');
        } finally {
            setBusy(false);
        }
    }

    function markDirty() {
        state.dirty = true;
        renderMeta();
    }

    function sectionFor(element) {
        const row = element.closest('[data-wow-today-section-key]');
        if (!row) return null;
        return state.records.find(item => item.key === row.dataset.wowTodaySectionKey) || null;
    }

    function cardFor(element, section) {
        const row = element.closest('[data-wow-today-card-key]');
        if (!row || !section) return null;
        return section.cards.find(item => item.key === row.dataset.wowTodayCardKey) || null;
    }

    function bindEvents() {
        const section = root();
        if (!section || section.dataset.bound === '1') return;
        section.dataset.bound = '1';

        document.getElementById('wow-today-settings-save')?.addEventListener('click', save);
        document.getElementById('wow-today-settings-refresh')?.addEventListener('click', function () {
            if (state.dirty && !window.confirm('刷新会丢弃尚未保存的更改，确定继续吗？')) return;
            state.loaded = false;
            load(true);
        });
        document.getElementById('wow-today-settings-reset')?.addEventListener('click', function () {
            state.records.sort((left, right) => left.source_index - right.source_index);
            state.records.forEach(sectionItem => {
                sectionItem.display_name = '';
                sectionItem.is_visible = Boolean(sectionItem.default_visible);
                sectionItem.cards.sort((left, right) => left.source_index - right.source_index);
                sectionItem.cards.forEach(card => {
                    card.display_name = '';
                    card.is_visible = Boolean(card.default_visible);
                });
            });
            state.expanded = new Set(state.records.filter(sectionItem => sectionItem.is_visible).map(sectionItem => sectionItem.key));
            markDirty();
            render();
            setMessage('板块和卡片均已恢复推荐状态，保存后才会应用到 Portal。', 'info');
        });
        document.getElementById('wow-today-settings-search')?.addEventListener('input', function (event) {
            state.search = event.target.value || '';
            render();
        });
        document.getElementById('wow-today-settings-expand')?.addEventListener('click', function () {
            const allExpanded = state.records.length > 0 && state.records.every(sectionItem => state.expanded.has(sectionItem.key));
            state.expanded = allExpanded
                ? new Set()
                : new Set(state.records.map(sectionItem => sectionItem.key));
            render();
        });

        section.addEventListener('input', function (event) {
            const sectionItem = sectionFor(event.target);
            if (!sectionItem) return;
            if (event.target.matches('[data-wow-today-section-name]')) {
                sectionItem.display_name = event.target.value;
            } else if (event.target.matches('[data-wow-today-card-name]')) {
                const card = cardFor(event.target, sectionItem);
                if (!card) return;
                card.display_name = event.target.value;
            } else {
                return;
            }
            markDirty();
        });
        section.addEventListener('change', function (event) {
            const sectionItem = sectionFor(event.target);
            if (!sectionItem) return;
            if (event.target.matches('[data-wow-today-section-visible]')) {
                sectionItem.is_visible = event.target.checked;
            } else if (event.target.matches('[data-wow-today-card-visible]')) {
                const card = cardFor(event.target, sectionItem);
                if (!card) return;
                card.is_visible = event.target.checked;
            } else {
                return;
            }
            markDirty();
            render();
        });
        section.addEventListener('click', function (event) {
            const toggle = event.target.closest('[data-wow-today-toggle-section]');
            if (toggle) {
                const sectionItem = sectionFor(toggle);
                if (!sectionItem) return;
                if (state.expanded.has(sectionItem.key)) state.expanded.delete(sectionItem.key);
                else state.expanded.add(sectionItem.key);
                render();
                return;
            }
            const sectionMove = event.target.closest('[data-wow-today-section-move]');
            if (sectionMove) {
                const sectionItem = sectionFor(sectionMove);
                const index = state.records.indexOf(sectionItem);
                const nextIndex = sectionMove.dataset.wowTodaySectionMove === 'up' ? index - 1 : index + 1;
                if (index < 0 || nextIndex < 0 || nextIndex >= state.records.length) return;
                [state.records[index], state.records[nextIndex]] = [state.records[nextIndex], state.records[index]];
                markDirty();
                render();
                return;
            }
            const cardMove = event.target.closest('[data-wow-today-card-move]');
            if (cardMove) {
                const sectionItem = sectionFor(cardMove);
                const card = cardFor(cardMove, sectionItem);
                const index = sectionItem?.cards.indexOf(card) ?? -1;
                const nextIndex = cardMove.dataset.wowTodayCardMove === 'up' ? index - 1 : index + 1;
                if (index < 0 || nextIndex < 0 || nextIndex >= sectionItem.cards.length) return;
                [sectionItem.cards[index], sectionItem.cards[nextIndex]] = [sectionItem.cards[nextIndex], sectionItem.cards[index]];
                markDirty();
                render();
            }
        });
    }

    window.loadWowTodaySectionSettings = function () {
        bindEvents();
        return load(false);
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bindEvents, { once: true });
    } else {
        bindEvents();
    }
})();
