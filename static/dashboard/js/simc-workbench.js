/* SimC 十模型内联工作台：专用 API、事件委托和安全结果预览。 */
(() => {
    'use strict';
    const apiRoot = '/api/simc-workbench/';
    const state = { taskResource: 'tasks', templateType: '', rows: Object.create(null) };
    const esc = value => window.escapeHtml(String(value == null ? '' : value));
    const idOf = value => { const id = Number.parseInt(String(value), 10); return Number.isSafeInteger(id) && id > 0 ? id : 0; };
    const resourceUrl = (resource, id) => `${apiRoot}${resource}/${id ? `${id}/` : ''}`;
    const json = async (url, options = {}) => {
        if (!url.startsWith('/') || url.startsWith('//')) throw new Error('拒绝非同源请求');
        const response = await fetch(url, options);
        const payload = await response.json();
        if (!response.ok || !payload.success) throw new Error(payload.error || '请求失败');
        return payload;
    };
    const empty = text => `<div class="rounded-xl border border-dashed p-6 text-center text-gray-500">${esc(text)}</div>`;
    const buttons = (resource, row) => {
        const id = idOf(row.id);
        if (!id) return '';
        const active = row.is_active !== false;
        return `<button data-wb-action="detail" data-resource="${esc(resource)}" data-id="${id}" class="text-blue-700">详情</button> <button data-wb-action="${active ? 'archive' : 'restore'}" data-resource="${esc(resource)}" data-id="${id}" class="text-amber-700">${active ? '停用' : '恢复'}</button>`;
    };
    async function loadTasks(resource = state.taskResource) {
        state.taskResource = resource === 'batches' ? 'batches' : 'tasks';
        const host = document.getElementById('simc-wb-task-list');
        if (!host) return;
        const data = await json(resourceUrl(state.taskResource));
        state.rows[state.taskResource] = data.data || [];
        host.innerHTML = data.data.length ? data.data.map(row => `<article class="flex flex-wrap justify-between gap-3 border-b p-3"><div><b>${esc(row.name || `#${idOf(row.id)}`)}</b><div class="text-xs text-gray-500">状态 ${esc(row.status)} · ${esc(row.created_at)}</div></div><div class="flex gap-3">${buttons(state.taskResource, row)}${state.taskResource === 'tasks' && [2, 3].includes(Number(row.status)) ? `<button data-wb-action="rerun" data-resource="tasks" data-id="${idOf(row.id)}" class="text-emerald-700">重跑</button>` : ''}</div></article>`).join('') : empty('暂无记录');
    }
    async function showTaskDetail(resource, id) {
        const data = await json(resourceUrl(resource, id));
        const row = data.data || {};
        const host = document.getElementById('simc-wb-task-detail');
        host.classList.remove('hidden');
        host.innerHTML = `<div class="flex justify-between"><h4 class="font-bold">${esc(row.name || `#${id}`)}</h4><button data-wb-close-detail>关闭</button></div><dl class="mt-3 grid gap-2 text-sm md:grid-cols-3"><div>状态：${esc(row.status)}</div><div>类型：${esc(row.task_type || row.batch_type)}</div><div>更新时间：${esc(row.updated_at)}</div></dl>${(row.artifacts || []).map(a => `<button data-artifact-preview="${idOf(a.id)}" data-preview-url="${esc(a.preview_url)}" data-title="${esc(a.file_name)}" class="mt-3 mr-2 rounded bg-blue-600 px-3 py-2 text-white">预览 ${esc(a.file_name)}</button>`).join('')}`;
    }
    async function loadArtifacts() {
        const host = document.getElementById('simc-wb-artifact-list');
        if (!host) return;
        const data = await json(resourceUrl('artifacts'));
        host.innerHTML = data.data.length ? data.data.map(row => `<article class="flex justify-between gap-3 border-b p-3"><div><b>${esc(row.file_name)}</b><div class="text-xs text-gray-500">${esc(row.task_name)} · ${esc(row.artifact_type)} · ${esc(row.file_size)} bytes</div></div><button data-artifact-preview="${idOf(row.id)}" data-preview-url="${esc(row.preview_url)}" data-title="${esc(row.file_name)}" class="text-blue-700">安全预览</button></article>`).join('') : empty('暂无结果产物');
    }
    function previewArtifact(button) {
        const id = idOf(button.dataset.artifactPreview);
        const url = String(button.dataset.previewUrl || '');
        if (!id || url !== resourceUrl('artifacts', id) + 'preview/') return;
        const host = document.getElementById('simc-wb-artifact-detail');
        host.classList.remove('hidden');
        host.innerHTML = window.renderSimcArtifactFrame(url, button.dataset.title || 'SimC 结果预览');
    }
    async function loadTemplates() {
        const host = document.getElementById('simc-wb-template-list');
        const data = await json(resourceUrl('templates'));
        state.rows.templates = data.data || [];
        const rows = state.templateType ? data.data.filter(row => row.template_type === state.templateType) : data.data;
        host.innerHTML = rows.length ? rows.map(row => `<article class="flex justify-between border-b p-3"><div><b>${esc(row.name)}</b><div class="text-xs text-gray-500">${esc(row.type_label)} · ${esc(row.spec)} · ${row.read_only ? '只读' : '可编辑'}</div></div><button data-wb-action="template-detail" data-resource="templates" data-id="${idOf(row.id)}" class="text-blue-700">行内详情</button></article>`).join('') : empty('此类型暂无模板');
    }
    async function loadApl(resource, hostId) {
        const host = document.getElementById(hostId);
        const data = await json(resourceUrl(resource));
        state.rows[resource] = data.data || [];
        const canWrite = resource === 'apl-storage' || data.can_write === true;
        document.querySelector(`[data-inline-create="${resource}"]`)?.classList.toggle('hidden', !canWrite);
        host.innerHTML = data.data.length ? data.data.map(row => `<article class="flex justify-between border-b p-3"><div><b>${esc(row.title || row.apl_keyword)}</b><div class="text-xs text-gray-500">${esc(row.cn_keyword || '')} ${esc(row.description || '')}</div></div>${canWrite ? buttons(resource, row) : '<span class="text-xs text-gray-400">只读</span>'}</article>`).join('') : empty('暂无数据');
    }
    async function loadBackend() {
        const host = document.getElementById('simc-wb-backend-status');
        const actions = document.getElementById('simc-wb-backend-actions');
        const data = await json('/api/simc-backend-binary/');
        const info = data.data || data;
        host.innerHTML = `<dl class="grid gap-3 md:grid-cols-3"><div class="rounded bg-slate-50 p-3">平台<br><b>${esc(info.platform)}</b></div><div class="rounded bg-slate-50 p-3">当前版本<br><b>${esc(info.current_version)}</b></div><div class="rounded bg-slate-50 p-3">最新版本<br><b>${esc(info.latest_version)}</b></div></dl>`;
        actions.innerHTML = info.can_write === true ? '<button data-backend-action="update" class="rounded bg-blue-600 px-4 py-2 text-white">安全更新实例</button>' : '<span class="text-sm text-gray-500">只读权限</span>';
    }
    async function lifecycle(resource, id, action) {
        await json(resourceUrl(resource, id), { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': window.getCSRFToken() }, body: JSON.stringify({ action }) });
        if (resource === 'tasks' || resource === 'batches') await loadTasks(resource);
        else if (resource === 'apl-storage') await loadApl(resource, 'simc-wb-apl-storage-list');
        else if (resource === 'apl-keywords') await loadApl(resource, 'simc-wb-apl-keyword-list');
    }
    function activate(tab) {
        window.switchSimcWorkbenchTab(tab || 'tasks');
        if (tab === 'tasks') loadTasks().catch(notify);
        if (tab === 'artifacts') loadArtifacts().catch(notify);
        if (tab === 'templates') loadTemplates().catch(notify);
        if (tab === 'apl') Promise.all([loadApl('apl-storage', 'simc-wb-apl-storage-list'), loadApl('apl-keywords', 'simc-wb-apl-keyword-list')]).catch(notify);
        if (tab === 'backend') loadBackend().catch(notify);
    }
    const notify = error => window.showMessage(String(error.message || error), 'error');
    document.addEventListener('DOMContentLoaded', () => {
        const root = document.getElementById('simc-workbench');
        if (!root) return;
        root.addEventListener('click', event => {
            const tab = event.target.closest('[data-simc-tab]');
            if (tab) { const name = tab.dataset.simcTab || 'tasks'; activate(name); if (tab.dataset.simcModel === 'batches') loadTasks('batches').catch(notify); }
            const subtab = event.target.closest('[data-task-subtab]');
            if (subtab) loadTasks(subtab.dataset.taskSubtab).catch(notify);
            const preview = event.target.closest('[data-artifact-preview]');
            if (preview) previewArtifact(preview);
            if (event.target.closest('[data-wb-close-detail]')) document.getElementById('simc-wb-task-detail').classList.add('hidden');
            const type = event.target.closest('[data-template-type]');
            if (type) { state.templateType = type.dataset.templateType || ''; loadTemplates().catch(notify); }
            const action = event.target.closest('[data-wb-action]');
            if (action) {
                const id = idOf(action.dataset.id), resource = action.dataset.resource, name = action.dataset.wbAction;
                if (!id) return;
                if (name === 'detail') showTaskDetail(resource, id).catch(notify);
                else if (name === 'template-detail') { const row = (state.rows.templates || []).find(item => idOf(item.id) === id); const host = document.getElementById('simc-wb-template-detail'); host.classList.remove('hidden'); host.innerHTML = `<h4 class="font-bold">${esc(row?.name)}</h4><pre class="mt-3 max-h-96 overflow-auto whitespace-pre-wrap text-xs">${esc(row?.content)}</pre>`; }
                else lifecycle(resource, id, name).catch(notify);
            }
            const refresh = event.target.closest('[data-simc-refresh]');
            if (refresh) activate(refresh.dataset.simcRefresh === 'backend' ? 'backend' : refresh.dataset.simcRefresh);
        });
        document.getElementById('simc-wb-convert')?.addEventListener('click', async () => {
            try { document.getElementById('simc-wb-convert-output').value = await window.convertText(document.getElementById('simc-wb-convert-input').value, document.getElementById('simc-wb-convert-mode').value); } catch (error) { notify(error); }
        });
        activate('tasks');
    });
})();
