(() => {
    'use strict';

    const root = document.getElementById('gear-builder-management');
    if (!root) return;

    const elements = {
        body: document.getElementById('gear-builder-management-table-body'),
        search: document.getElementById('gear-builder-management-search'),
        status: document.getElementById('gear-builder-management-status'),
        pageSize: document.getElementById('gear-builder-management-page-size'),
        refresh: document.getElementById('gear-builder-management-refresh'),
        message: document.getElementById('gear-builder-management-message'),
        pageInfo: document.getElementById('gear-builder-management-page-info'),
        pagination: document.getElementById('gear-builder-management-pagination'),
        loadoutCount: document.getElementById('gear-builder-management-loadout-count'),
        shareCount: document.getElementById('gear-builder-management-share-count'),
        activeShareCount: document.getElementById('gear-builder-management-active-share-count'),
        detail: document.getElementById('gear-builder-management-detail'),
        detailTitle: document.getElementById('gear-builder-management-detail-title'),
        detailSummary: document.getElementById('gear-builder-management-detail-summary'),
        detailMeta: document.getElementById('gear-builder-management-detail-meta'),
        detailCode: document.getElementById('gear-builder-management-detail-code'),
        copyCode: document.getElementById('gear-builder-management-copy-code'),
    };
    const endpoints = {
        loadouts: root.dataset.loadoutsUrl,
        shares: root.dataset.sharesUrl,
    };
    const state = {resource: 'loadouts', page: 1, pages: 1, loading: false, code: ''};
    let searchTimer = null;

    function escapeHtml(value) {
        return String(value ?? '').replace(/[&<>'"]/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'}[char]));
    }

    function csrfToken() {
        return document.querySelector('#csrf-form input[name="csrfmiddlewaretoken"]')?.value || '';
    }

    async function requestJson(url, options = {}) {
        const response = await fetch(url, {
            ...options,
            headers: {'Accept': 'application/json', ...(options.headers || {})},
        });
        let payload = {};
        try { payload = await response.json(); } catch (_) { /* 统一错误 */ }
        if (!response.ok || payload.success === false) throw new Error(payload.error || payload.message || `请求失败（${response.status}）`);
        return payload;
    }

    function formatDate(value) {
        if (!value) return '—';
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return String(value);
        return new Intl.DateTimeFormat('zh-CN', {dateStyle: 'medium', timeStyle: 'short'}).format(date);
    }

    function showMessage(message, isError = false) {
        elements.message.textContent = message || '';
        elements.message.className = message
            ? `border-b px-4 py-3 text-sm ${isError ? 'bg-red-50 text-red-700' : 'bg-blue-50 text-blue-700'}`
            : 'hidden border-b px-4 py-3 text-sm';
    }

    function updateTabs() {
        root.querySelectorAll('[data-gear-builder-resource]').forEach(button => {
            const active = button.dataset.gearBuilderResource === state.resource;
            button.setAttribute('aria-selected', String(active));
            button.classList.toggle('bg-white', active);
            button.classList.toggle('text-blue-700', active);
            button.classList.toggle('shadow-sm', active);
            button.classList.toggle('text-gray-600', !active);
        });
        elements.status.classList.toggle('hidden', state.resource !== 'shares');
    }

    function renderSummary(summary = {}) {
        elements.loadoutCount.textContent = Number(summary.loadouts || 0).toLocaleString('zh-CN');
        elements.shareCount.textContent = Number(summary.shares || 0).toLocaleString('zh-CN');
        elements.activeShareCount.textContent = Number(summary.active_shares || 0).toLocaleString('zh-CN');
    }

    function rowTitle(record) {
        if (state.resource === 'loadouts') {
            return `<strong class="text-gray-900">${escapeHtml(record.name)}</strong><small class="mt-1 block text-xs text-gray-500">#${Number(record.id)}</small>`;
        }
        const status = record.is_active
            ? '<span class="rounded-full bg-emerald-50 px-2 py-0.5 text-xs text-emerald-700">有效</span>'
            : '<span class="rounded-full bg-gray-100 px-2 py-0.5 text-xs text-gray-500">已停用</span>';
        return `<div class="flex items-center gap-2"><code class="font-semibold text-gray-900">${escapeHtml(record.token)}</code>${status}</div><a class="mt-1 block text-xs text-blue-600 hover:underline" href="${escapeHtml(record.short_path)}" target="_blank" rel="noopener noreferrer">${escapeHtml(record.short_path)}</a>`;
    }

    function renderRows(records) {
        if (!records.length) {
            elements.body.innerHTML = '<tr><td colspan="7" class="px-4 py-14 text-center text-sm text-gray-500">没有符合条件的记录。</td></tr>';
            return;
        }
        elements.body.innerHTML = records.map(record => {
            const statusText = state.resource === 'loadouts'
                ? '<span class="text-gray-500">账号线上保存</span>'
                : `<span class="font-medium text-gray-800">${Number(record.access_count || 0).toLocaleString('zh-CN')} 次访问</span><small class="mt-1 block text-xs text-gray-500">最后访问 ${escapeHtml(formatDate(record.last_accessed_at))}</small>`;
            const removeButton = state.resource === 'loadouts'
                ? `<button type="button" data-gear-builder-delete="${Number(record.id)}" class="rounded-md border border-red-200 px-2.5 py-1.5 text-xs text-red-700 hover:bg-red-50">删除</button>`
                : (record.is_active ? `<button type="button" data-gear-builder-delete="${Number(record.id)}" class="rounded-md border border-amber-200 px-2.5 py-1.5 text-xs text-amber-700 hover:bg-amber-50">停用</button>` : '');
            return `<tr class="align-top hover:bg-gray-50">
                <td class="px-4 py-3 text-sm">${rowTitle(record)}</td>
                <td class="px-4 py-3 text-sm"><strong class="text-gray-800">${escapeHtml(record.user?.username || '—')}</strong><small class="mt-1 block text-xs text-gray-500">用户 #${Number(record.user?.id || 0)}</small></td>
                <td class="px-4 py-3 text-sm text-gray-700">${escapeHtml(record.class_name)}<small class="mt-1 block text-xs text-gray-500">${escapeHtml(record.spec_name)}</small></td>
                <td class="max-w-56 px-4 py-3 text-xs text-gray-600"><span class="break-all">${escapeHtml(record.batch_key)}</span></td>
                <td class="px-4 py-3 text-sm">${statusText}</td>
                <td class="px-4 py-3 text-xs text-gray-600">${escapeHtml(formatDate(record.updated_at))}<small class="mt-1 block text-gray-400">创建 ${escapeHtml(formatDate(record.created_at))}</small></td>
                <td class="px-4 py-3"><div class="flex justify-end gap-2"><button type="button" data-gear-builder-detail="${Number(record.id)}" class="rounded-md border border-blue-200 px-2.5 py-1.5 text-xs text-blue-700 hover:bg-blue-50">详情</button>${removeButton}</div></td>
            </tr>`;
        }).join('');
    }

    function renderPagination(pagination = {}) {
        state.page = Number(pagination.page || 1);
        state.pages = Number(pagination.pages || 1);
        elements.pageInfo.textContent = `第 ${state.page}/${state.pages} 页，共 ${Number(pagination.total || 0).toLocaleString('zh-CN')} 条`;
        const buttons = [];
        const add = (label, page, disabled = false, active = false) => buttons.push(`<button type="button" data-gear-builder-page="${page}" ${disabled ? 'disabled' : ''} class="min-w-9 rounded-md border px-3 py-1.5 text-sm ${active ? 'border-blue-600 bg-blue-600 text-white' : 'border-gray-300 bg-white text-gray-700'} disabled:cursor-not-allowed disabled:opacity-40">${label}</button>`);
        add('上一页', Math.max(1, state.page - 1), state.page <= 1);
        const start = Math.max(1, state.page - 2);
        const end = Math.min(state.pages, start + 4);
        for (let page = start; page <= end; page += 1) add(String(page), page, false, page === state.page);
        add('下一页', Math.min(state.pages, state.page + 1), state.page >= state.pages);
        elements.pagination.innerHTML = buttons.join('');
    }

    async function loadRecords() {
        if (state.loading) return;
        state.loading = true;
        elements.body.innerHTML = '<tr><td colspan="7" class="px-4 py-14 text-center text-sm text-gray-500">正在读取管理数据…</td></tr>';
        showMessage('');
        const params = new URLSearchParams({
            page: String(state.page),
            page_size: elements.pageSize.value,
            q: elements.search.value.trim(),
        });
        if (state.resource === 'shares') params.set('active', elements.status.value);
        try {
            const payload = await requestJson(`${endpoints[state.resource]}?${params}`);
            renderRows(payload.records || []);
            renderPagination(payload.pagination || {});
            renderSummary(payload.summary || {});
        } catch (error) {
            elements.body.innerHTML = '<tr><td colspan="7" class="px-4 py-14 text-center text-sm text-red-600">管理数据加载失败。</td></tr>';
            showMessage(error.message, true);
        } finally {
            state.loading = false;
        }
    }

    function metaEntry(label, value) {
        return `<div class="rounded-lg bg-gray-50 p-3"><dt class="text-xs text-gray-500">${escapeHtml(label)}</dt><dd class="mt-1 break-all font-medium text-gray-800">${escapeHtml(value || '—')}</dd></div>`;
    }

    async function openDetail(id) {
        showMessage('');
        try {
            const payload = await requestJson(`${endpoints[state.resource]}${Number(id)}/`);
            const record = payload.record;
            state.code = record.code || '';
            elements.detailTitle.textContent = state.resource === 'loadouts' ? record.name : `短链接 ${record.token}`;
            elements.detailSummary.textContent = `${record.user?.username || '—'} · ${record.class_name || '—'} / ${record.spec_name || '—'}`;
            elements.detailMeta.innerHTML = [
                metaEntry('记录 ID', record.id),
                metaEntry('用户', `${record.user?.username || '—'}（#${record.user?.id || 0}）`),
                metaEntry('职业专精', `${record.class_name || '—'} / ${record.spec_name || '—'}`),
                metaEntry('装备批次', record.batch_key),
                metaEntry('状态哈希', record.state_hash),
                metaEntry('更新时间', formatDate(record.updated_at)),
                state.resource === 'shares' ? metaEntry('短链接', record.short_path) : '',
                state.resource === 'shares' ? metaEntry('访问统计', `${record.access_count || 0} 次；最后 ${formatDate(record.last_accessed_at)}`) : '',
            ].join('');
            elements.detailCode.textContent = state.code;
            elements.detail.classList.remove('hidden');
            elements.detail.classList.add('flex');
        } catch (error) {
            showMessage(error.message, true);
        }
    }

    function closeDetail() {
        state.code = '';
        elements.detail.classList.add('hidden');
        elements.detail.classList.remove('flex');
    }

    async function removeRecord(id) {
        const action = state.resource === 'loadouts' ? '永久删除这套线上配装' : '停用这个公开短链接';
        if (!window.confirm(`${action}？`)) return;
        try {
            await requestJson(`${endpoints[state.resource]}${Number(id)}/`, {
                method: 'DELETE',
                headers: {'X-CSRFToken': csrfToken()},
            });
            showMessage(state.resource === 'loadouts' ? '线上配装已删除。' : '短链接已停用。');
            await loadRecords();
        } catch (error) {
            showMessage(error.message, true);
        }
    }

    root.addEventListener('click', event => {
        const resourceButton = event.target.closest('[data-gear-builder-resource]');
        if (resourceButton) {
            state.resource = resourceButton.dataset.gearBuilderResource;
            state.page = 1;
            updateTabs();
            loadRecords();
            return;
        }
        const pageButton = event.target.closest('[data-gear-builder-page]');
        if (pageButton && !pageButton.disabled) {
            state.page = Number(pageButton.dataset.gearBuilderPage || 1);
            loadRecords();
            return;
        }
        const detailButton = event.target.closest('[data-gear-builder-detail]');
        if (detailButton) openDetail(detailButton.dataset.gearBuilderDetail);
        const deleteButton = event.target.closest('[data-gear-builder-delete]');
        if (deleteButton) removeRecord(deleteButton.dataset.gearBuilderDelete);
    });
    elements.search.addEventListener('input', () => {
        window.clearTimeout(searchTimer);
        searchTimer = window.setTimeout(() => { state.page = 1; loadRecords(); }, 280);
    });
    elements.status.addEventListener('change', () => { state.page = 1; loadRecords(); });
    elements.pageSize.addEventListener('change', () => { state.page = 1; loadRecords(); });
    elements.refresh.addEventListener('click', loadRecords);
    elements.detail.addEventListener('click', event => {
        if (event.target === elements.detail || event.target.closest('[data-gear-builder-detail-close]')) closeDetail();
    });
    elements.copyCode.addEventListener('click', async () => {
        if (!state.code) return;
        await navigator.clipboard.writeText(state.code);
        elements.copyCode.textContent = '已复制';
        window.setTimeout(() => { elements.copyCode.textContent = '复制编码'; }, 1200);
    });

    updateTabs();
    window.loadGearBuilderManagement = loadRecords;
})();
