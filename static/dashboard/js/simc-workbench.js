/* SimC 十模型内联工作台：专用 API、事件委托和安全结果预览。version: 20260715j */
(() => {
    'use strict';
    const apiRoot = '/api/simc-workbench/';
    const state = { activePanel: '', taskResource: 'tasks', taskPage: 1, taskFetchInFlight: false, taskRequestSerial: 0, taskPollTimer: null, taskAbortController: null, templateType: '', rows: Object.create(null) };
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
    async function loadTasks(resource = state.taskResource, page = 1) {
        state.taskResource = resource === 'batches' ? 'batches' : 'tasks';
        state.taskPage = Number.isSafeInteger(Number(page)) && Number(page) > 0 ? Number(page) : 1;
        const requestedResource = state.taskResource;
        const requestedPage = state.taskPage;
        const requestSerial = ++state.taskRequestSerial;
        const host = document.getElementById('simc-wb-task-list');
        if (!host) return;
        if (state.taskAbortController) state.taskAbortController.abort();
        const controller = new AbortController();
        state.taskAbortController = controller;
        state.taskFetchInFlight = true;
        let data;
        try {
            data = await json(`${resourceUrl(requestedResource)}?page=${requestedPage}&page_size=20`, { signal: controller.signal });
        } catch (error) {
            if (error.name === 'AbortError') return;
            throw error;
        } finally {
            if (requestSerial === state.taskRequestSerial) {
                state.taskFetchInFlight = false;
                if (state.taskAbortController === controller) state.taskAbortController = null;
            }
        }
        if (requestSerial !== state.taskRequestSerial || state.activePanel !== 'tasks') return;
        state.rows[requestedResource] = data.data || [];

        if (requestedResource === 'tasks') {
            host.innerHTML = data.data.length ? data.data.map(row => {
                const hasProgress = row.progress !== null && row.progress !== '' && Number.isFinite(Number(row.progress));
                const progressBar = hasProgress ? `<div class="mt-1 flex items-center gap-2"><div class="h-1.5 flex-1 rounded-full bg-gray-200"><div class="h-1.5 rounded-full bg-blue-600" style="width:${Number(row.progress)}%"></div></div><span class="text-xs text-gray-500">${Number(row.progress)}%</span></div>` : '';
                return `<article class="flex flex-wrap justify-between gap-3 border-b p-3"><div class="flex-1 min-w-0"><b>${esc(row.name || `#${idOf(row.id)}`)}</b><div class="text-xs text-gray-500">${esc(row.status_label)} · ${esc(row.created_at)}</div>${progressBar}</div><div class="flex gap-3">${buttons(state.taskResource, row)}${[2, 3].includes(Number(row.status)) ? `<button data-wb-action="rerun" data-resource="tasks" data-id="${idOf(row.id)}" class="text-emerald-700">重跑</button>` : ''}</div></article>`;
            }).join('') : empty('暂无记录');
        } else {
            host.innerHTML = data.data.length ? data.data.map(row => {
                const progressBar = row.total > 0 ? `<div class="w-full bg-gray-200 rounded-full h-1.5 mt-1"><div class="bg-blue-600 h-1.5 rounded-full" style="width:${row.percent || 0}%"></div></div>` : '';
                const statusText = `${row.succeeded}/${row.total} 成功 · ${row.failed > 0 ? `${row.failed} 失败 · ` : ''}${row.pending} 待运行`;
                const compareButton = row.report_url ? `<button data-wb-action="compare" data-resource="batches" data-id="${idOf(row.id)}" class="text-purple-700">内联比较</button>` : '';
                return `<article class="flex flex-wrap justify-between gap-3 border-b p-3"><div class="flex-1 min-w-0"><b>${esc(row.name || `#${idOf(row.id)}`)}</b><div class="text-xs text-gray-500">${statusText} · ${esc(row.created_at)}</div>${progressBar}</div><div class="flex gap-3">${compareButton}${buttons(state.taskResource, row)}</div></article>`;
            }).join('') : empty('暂无记录');
        }

        renderPagination(data.pagination || {}, requestedPage, requestedResource);
        const hasActive = requestedResource === 'tasks'
            ? data.data.some(row => [0, 1, 4].includes(Number(row.status)))
            : data.data.some(row => Number(row.pending || 0) > 0 || [0, 1, 4].includes(Number(row.status)));
        scheduleTaskRefresh(hasActive);
    }

    function scheduleTaskRefresh(hasActive) {
        if (state.taskPollTimer) {
            clearTimeout(state.taskPollTimer);
            state.taskPollTimer = null;
        }
        if (!hasActive || state.activePanel !== 'tasks') return;
        const resource = state.taskResource;
        const page = state.taskPage;
        state.taskPollTimer = setTimeout(() => {
            state.taskPollTimer = null;
            if (state.activePanel !== 'tasks' || resource !== state.taskResource || page !== state.taskPage) return;
            loadTasks(resource, page).catch(() => scheduleTaskRefresh(true));
        }, 3000);
    }

    function renderPagination(pagination, currentPage, resource) {
        const paginationHost = document.getElementById('simc-wb-task-pagination');
        if (!paginationHost) return;

        const { total = 0, total_pages = 1, page = 1, page_size = 20 } = pagination;
        if (total === 0 || total_pages <= 1) {
            paginationHost.innerHTML = '';
            return;
        }

        const buttons = [];
        if (page > 1) {
            buttons.push(`<button data-pagination-page="${page - 1}" data-pagination-resource="${resource}" class="px-3 py-1 border rounded hover:bg-gray-100">上一页</button>`);
        }

        const start = Math.max(1, page - 2);
        const end = Math.min(total_pages, page + 2);
        for (let i = start; i <= end; i++) {
            const active = i === page ? 'bg-blue-600 text-white' : 'border hover:bg-gray-100';
            buttons.push(`<button data-pagination-page="${i}" data-pagination-resource="${resource}" class="px-3 py-1 rounded ${active}">${i}</button>`);
        }

        if (page < total_pages) {
            buttons.push(`<button data-pagination-page="${page + 1}" data-pagination-resource="${resource}" class="px-3 py-1 border rounded hover:bg-gray-100">下一页</button>`);
        }

        paginationHost.innerHTML = `<div class="flex items-center justify-between mt-3 text-sm"><div class="text-gray-600">共 ${total} 条记录，第 ${page}/${total_pages} 页</div><div class="flex gap-2">${buttons.join('')}</div></div>`;
    }
    async function showTaskDetail(resource, id) {
        const data = await json(resourceUrl(resource, id));
        const row = data.data || {};
        const host = document.getElementById('simc-wb-task-detail');
        host.classList.remove('hidden');
        const status = row.status_label || ({ 0: '待运行', 1: '运行中', 2: '成功', 3: '失败' }[Number(row.status)] || '未知');
        host.innerHTML = `<div class="flex justify-between"><h4 class="font-bold">${esc(row.name || `#${id}`)}</h4><button data-wb-close-detail>关闭</button></div><dl class="mt-3 grid gap-2 text-sm md:grid-cols-3"><div>状态：${esc(status)}</div><div>类型：${esc(row.task_type || row.batch_type)}</div><div>更新时间：${esc(row.updated_at)}</div></dl>${(row.artifacts || []).map(a => `<button data-artifact-preview="${idOf(a.id)}" data-preview-url="${esc(a.preview_url)}" data-title="${esc(a.file_name)}" class="mt-3 mr-2 rounded bg-blue-600 px-3 py-2 text-white">预览 ${esc(a.file_name)}</button>`).join('')}`;
    }
    async function showBatchComparison(id) {
        const data = await json(`/api/simc-regular-compare/?batch_id=${id}&summary=1`);
        const rows = Array.isArray(data.data?.tasks) ? data.data.tasks : [];
        const host = document.getElementById('simc-wb-task-detail');
        host.classList.remove('hidden');
        const tableRows = rows.map(row => {
            const dps = row.dps == null ? '-' : Math.round(Number(row.dps)).toLocaleString();
            const delta = row.delta_dps == null ? '-' : `${Number(row.delta_dps) >= 0 ? '+' : ''}${Math.round(Number(row.delta_dps)).toLocaleString()}`;
            const percent = row.delta_percent == null ? '-' : `${Number(row.delta_percent) >= 0 ? '+' : ''}${row.delta_percent}%`;
            return `<tr class="border-t"><td class="p-2">${esc(row.rank || '-')}</td><td class="p-2">${esc(row.label || row.name)}</td><td class="p-2 text-right">${esc(dps)}</td><td class="p-2 text-right">${esc(delta)}</td><td class="p-2 text-right">${esc(percent)}</td></tr>`;
        }).join('');
        host.innerHTML = `<div class="flex justify-between gap-3"><div><h4 class="font-bold">结果比较</h4><p class="text-xs text-gray-500">仅展示已解析的安全结果摘要</p></div><button data-wb-close-detail>关闭</button></div><div class="mt-3 overflow-x-auto"><table class="w-full min-w-[560px] text-sm"><thead><tr class="text-left text-gray-500"><th class="p-2">排名</th><th class="p-2">方案</th><th class="p-2 text-right">DPS</th><th class="p-2 text-right">差值</th><th class="p-2 text-right">差值%</th></tr></thead><tbody>${tableRows}</tbody></table></div>`;
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
        state.canWriteTemplates = data.can_write === true;
        const rows = state.templateType ? data.data.filter(row => row.template_type === state.templateType) : data.data;
        host.innerHTML = rows.length ? rows.map(row => {
            const active = row.is_active !== false;
            const readOnly = row.read_only === true;
            return `<article class="flex justify-between border-b p-3"><div><b>${esc(row.name)}</b><div class="text-xs text-gray-500">${esc(row.type_label)} · ${esc(row.spec)} · ${readOnly ? '只读' : '可编辑'} · ${active ? '启用' : '已停用'}</div></div><div class="flex gap-2">${state.canWriteTemplates && !readOnly ? `<button data-wb-action="template-edit" data-resource="templates" data-id="${idOf(row.id)}" class="text-blue-700">编辑</button><button data-wb-action="${active ? 'archive' : 'restore'}" data-resource="templates" data-id="${idOf(row.id)}" class="text-amber-700">${active ? '停用' : '恢复'}</button>` : ''}<button data-wb-action="template-detail" data-resource="templates" data-id="${idOf(row.id)}" class="text-slate-700">详情</button></div></article>`;
        }).join('') : empty('此类型暂无模板');
        document.querySelector('[data-inline-create="templates"]')?.classList.toggle('hidden', !state.canWriteTemplates);
    }
    function renderTemplateForm(row = null) {
        const host = document.getElementById('simc-wb-template-form');
        if (!host) return;
        host.classList.remove('hidden');
        const typeOptions = [
            { value: 'base_template', label: '基础模板' },
            { value: 'default_apl', label: '默认 APL' },
            { value: 'custom_apl', label: '自定义 APL' },
            { value: 'default_player', label: '默认玩家装备模板' },
            { value: 'custom_player', label: '用户自定义装备' },
        ].map(opt => `<option value="${esc(opt.value)}" ${row?.template_type === opt.value ? 'selected' : ''}>${esc(opt.label)}</option>`).join('');
        host.innerHTML = `<form data-template-form class="rounded-xl border border-blue-200 bg-blue-50 p-4">
            <input type="hidden" name="id" value="${idOf(row?.id)}">
            <label class="block text-sm font-medium text-gray-700">名称<input name="name" required maxlength="200" value="${esc(row?.name)}" class="mt-1 w-full rounded-lg border bg-white p-2"></label>
            <label class="mt-3 block text-sm font-medium text-gray-700">类型<select name="template_type" ${row ? 'disabled' : ''} required class="mt-1 w-full rounded-lg border bg-white p-2">${typeOptions}</select></label>
            <label class="mt-3 block text-sm font-medium text-gray-700">专精标识<input name="spec" maxlength="100" value="${esc(row?.spec || 'default')}" class="mt-1 w-full rounded-lg border bg-white p-2"></label>
            <label class="mt-3 block text-sm font-medium text-gray-700">职业<input name="class_name" maxlength="50" value="${esc(row?.class_name)}" class="mt-1 w-full rounded-lg border bg-white p-2"></label>
            <label class="mt-3 block text-sm font-medium text-gray-700">内容<textarea name="content" required rows="12" class="mt-1 w-full rounded-lg border bg-white p-3 font-mono text-xs">${esc(row?.content)}</textarea></label>
            <div class="mt-3 flex gap-2"><button type="submit" class="rounded-lg bg-blue-600 px-4 py-2 text-white">保存</button><button type="button" data-template-action="cancel" class="rounded-lg border bg-white px-4 py-2">取消</button></div>
        </form>`;
    }
    function closeTemplateForm() {
        const host = document.getElementById('simc-wb-template-form');
        if (!host) return;
        host.classList.add('hidden');
        host.replaceChildren();
    }
    function closeTemplateDetail() {
        const host = document.getElementById('simc-wb-template-detail');
        if (!host) return;
        host.classList.add('hidden');
        host.replaceChildren();
    }
    async function saveTemplate(form) {
        const formData = new FormData(form);
        const id = idOf(formData.get('id'));
        const payload = {
            name: String(formData.get('name') || '').trim(),
            spec: String(formData.get('spec') || 'default').trim(),
            class_name: String(formData.get('class_name') || '').trim(),
            content: String(formData.get('content') || '').trim(),
        };
        if (!id) {
            payload.template_type = String(formData.get('template_type') || '').trim();
        }
        await json(resourceUrl('templates', id), {
            method: id ? 'PUT' : 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': window.getCSRFToken() },
            body: JSON.stringify(payload),
        });
        closeTemplateForm();
        await loadTemplates();
        window.showMessage(id ? '模板已更新' : '模板已创建', 'success');
    }
    async function showTemplateDetail(id) {
        const data = await json(resourceUrl('templates', id));
        const row = data.data || {};
        const host = document.getElementById('simc-wb-template-detail');
        host.classList.remove('hidden');
        host.innerHTML = `<div class="flex justify-between mb-3"><h4 class="font-bold">${esc(row.name)}</h4><button data-template-action="close-detail" class="text-slate-500">关闭</button></div><dl class="grid gap-2 text-sm"><div>类型：${esc(row.type_label)}</div><div>专精：${esc(row.spec)}</div><div>职业：${esc(row.class_name || '-')}</div><div>来源：${esc(row.source === 'simc_upstream' ? 'SimC上游' : '用户维护')}</div><div>状态：${row.is_active ? '启用' : '已停用'}</div></dl><div class="mt-3"><label class="text-sm font-medium text-gray-700">内容</label><pre class="mt-1 rounded border bg-slate-50 p-3 text-xs overflow-auto max-h-96">${esc(row.content)}</pre></div>`;
    }
    function renderAplKeywordForm(row = null) {
        const host = document.getElementById('simc-wb-apl-keyword-form');
        if (!host) return;
        host.classList.remove('hidden');
        host.innerHTML = `<form data-apl-keyword-form class="rounded-xl border border-blue-200 bg-blue-50 p-4">
            <input type="hidden" name="id" value="${idOf(row?.id)}">
            <label class="block text-sm font-medium text-gray-700">APL 关键词<input name="apl_keyword" ${row ? 'readonly' : ''} required maxlength="100" value="${esc(row?.apl_keyword)}" class="mt-1 w-full rounded-lg border bg-white p-2"></label>
            <label class="mt-3 block text-sm font-medium text-gray-700">中文关键词<input name="cn_keyword" required maxlength="100" value="${esc(row?.cn_keyword)}" class="mt-1 w-full rounded-lg border bg-white p-2"></label>
            <label class="mt-3 block text-sm font-medium text-gray-700">描述<input name="description" maxlength="500" value="${esc(row?.description)}" class="mt-1 w-full rounded-lg border bg-white p-2"></label>
            <div class="mt-3 flex gap-2"><button type="submit" class="rounded-lg bg-blue-600 px-4 py-2 text-white">保存</button><button type="button" data-apl-keyword-action="cancel" class="rounded-lg border bg-white px-4 py-2">取消</button></div>
        </form>`;
    }
    function closeAplKeywordForm() {
        const host = document.getElementById('simc-wb-apl-keyword-form');
        if (!host) return;
        host.classList.add('hidden');
        host.replaceChildren();
    }
    async function saveAplKeyword(form) {
        const formData = new FormData(form);
        const id = idOf(formData.get('id'));
        const payload = {
            apl_keyword: String(formData.get('apl_keyword') || '').trim(),
            cn_keyword: String(formData.get('cn_keyword') || '').trim(),
            description: String(formData.get('description') || '').trim(),
        };
        await json(resourceUrl('apl-keywords', id), {
            method: id ? 'PUT' : 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': window.getCSRFToken() },
            body: JSON.stringify(payload),
        });
        closeAplKeywordForm();
        await loadApl('apl-keywords', 'simc-wb-apl-keyword-list');
        window.showMessage(id ? '关键词已更新' : '关键词已创建', 'success');
    }
    function renderAplStorageForm(row = null) {
        const host = document.getElementById('simc-wb-apl-storage-form');
        if (!host) return;
        host.classList.remove('hidden');
        host.innerHTML = `<form data-apl-storage-form class="rounded-xl border border-blue-200 bg-blue-50 p-4">
            <input type="hidden" name="id" value="${idOf(row?.id)}">
            <label class="block text-sm font-medium text-gray-700">标题<input name="title" required maxlength="255" value="${esc(row?.title)}" class="mt-1 w-full rounded-lg border bg-white p-2"></label>
            <label class="mt-3 block text-sm font-medium text-gray-700">APL 内容<textarea name="apl_code" required rows="12" class="mt-1 w-full rounded-lg border bg-white p-3 font-mono text-xs">${esc(row?.apl_code)}</textarea></label>
            <div class="mt-3 flex gap-2"><button type="submit" class="rounded-lg bg-blue-600 px-4 py-2 text-white">保存</button><button type="button" data-apl-action="cancel" class="rounded-lg border bg-white px-4 py-2">取消</button></div>
        </form>`;
    }
    function closeAplStorageForm() {
        const host = document.getElementById('simc-wb-apl-storage-form');
        if (!host) return;
        host.classList.add('hidden');
        host.replaceChildren();
    }
    async function loadApl(resource, hostId) {
        const host = document.getElementById(hostId);
        const data = await json(resourceUrl(resource));
        state.rows[resource] = data.data || [];
        const canWrite = resource === 'apl-storage' || data.can_write === true;
        document.querySelector(`[data-inline-create="${resource}"]`)?.classList.toggle('hidden', !canWrite);
        host.innerHTML = data.data.length ? data.data.map(row => {
            if (resource === 'apl-storage') {
                const active = row.is_active !== false;
                return `<article class="flex flex-wrap items-center justify-between gap-3 border-b p-3"><div><b>${esc(row.title)}</b><div class="text-xs text-gray-500">${active ? '启用中' : '已停用'}</div></div><div class="flex flex-wrap gap-3">${active ? `<button data-apl-action="use" data-id="${idOf(row.id)}" class="text-emerald-700">用于模拟</button><button data-apl-action="edit" data-id="${idOf(row.id)}" class="text-blue-700">编辑</button><button data-apl-action="archive" data-id="${idOf(row.id)}" class="text-amber-700">停用</button>` : `<button data-apl-action="restore" data-id="${idOf(row.id)}" class="text-emerald-700">恢复</button>`}</div></article>`;
            }
            return `<article class="flex justify-between border-b p-3"><div><b>${esc(row.title || row.apl_keyword)}</b><div class="text-xs text-gray-500">${esc(row.cn_keyword || '')} ${esc(row.description || '')}</div></div>${canWrite ? buttons(resource, row) : '<span class="text-xs text-gray-400">只读</span>'}</article>`;
        }).join('') : empty('暂无数据');
    }
    async function fetchAplStorageDetail(id) {
        return (await json(`/api/apl-storage/${id}/`)).data;
    }
    async function saveAplStorage(form) {
        const formData = new FormData(form);
        const id = idOf(formData.get('id'));
        const payload = { title: String(formData.get('title') || '').trim(), apl_code: String(formData.get('apl_code') || '').trim() };
        if (id) payload.id = id;
        await json('/api/apl-storage/', {
            method: id ? 'PUT' : 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': window.getCSRFToken() },
            body: JSON.stringify(payload),
        });
        closeAplStorageForm();
        await loadApl('apl-storage', 'simc-wb-apl-storage-list');
        window.showMessage(id ? 'APL 已更新' : 'APL 已新增', 'success');
    }
    async function useAplForSimulation(id) {
        const row = await fetchAplStorageDetail(id);
        const editor = document.getElementById('apl-override');
        if (!editor) throw new Error('模拟工作流的 APL 编辑区不存在');
        editor.value = row.apl_code || '';
        document.querySelectorAll('input[name="simc-sim-apl"]').forEach(input => { input.checked = false; });
        document.querySelector('.simc-l1-tab[data-simc-l1-tab="workflow"]')?.click();
        editor.scrollIntoView({ behavior: 'smooth', block: 'center' });
        window.showMessage(`已加载“${row.title}”用于本次模拟`, 'success');
    }
    window.loadSimcWorkbenchApl = () => loadApl('apl-storage', 'simc-wb-apl-storage-list').catch(notify);
    async function loadBackend() {
        const host = document.getElementById('simc-wb-backend-status');
        const actions = document.getElementById('simc-wb-backend-actions');
        const data = await json('/api/simc-backend-binary/');
        const info = data.data || {};
        const availableLabel = info.available ? '可用' : '不可用';
        const updateLabel = info.need_update ? '有更新' : '已同步';
        const progress = Number.isFinite(Number(info.update_progress)) ? Math.max(0, Math.min(100, Number(info.update_progress))) : 0;
        host.innerHTML = `<dl class="grid gap-3 md:grid-cols-3">
            <div class="rounded bg-slate-50 p-3">平台<br><b>${esc(info.platform)}</b></div>
            <div class="rounded bg-slate-50 p-3">本地源码版本<br><b>${esc(info.current_version || '-')}</b></div>
            <div class="rounded bg-slate-50 p-3">上游源码版本<br><b>${esc(info.latest_version || '-')}</b></div>
            <div class="rounded bg-slate-50 p-3">本地二进制<br><b>${esc(info.binary_name || '-')} · ${esc(availableLabel)}</b></div>
            <div class="rounded bg-slate-50 p-3">源码状态<br><b>${esc(updateLabel)}</b></div>
            <div class="rounded bg-slate-50 p-3">执行状态<br><b>${esc(info.update_status || '未初始化')}</b></div>
        </dl>
        <div class="mt-3 h-2 overflow-hidden rounded bg-slate-200"><div class="h-full bg-blue-600" style="width:${progress}%"></div></div>
        <div class="mt-2 text-xs text-gray-500">进度 ${progress}%${info.is_updating ? ' · 正在执行' : ''}${info.has_error ? ' · 最近一次操作失败，请检查服务端日志' : ''}</div>`;
        if (info.can_write !== true) {
            actions.innerHTML = '<span class="text-sm text-gray-500">只读权限</span>';
            return;
        }
        const disabled = info.is_updating ? 'disabled aria-disabled="true"' : '';
        actions.innerHTML = `<label class="flex items-center gap-2 text-sm"><input type="checkbox" data-backend-auto-update ${info.auto_update ? 'checked' : ''} ${disabled}>自动更新</label>
            <button data-backend-action="check" ${disabled} class="rounded border bg-white px-4 py-2 text-slate-700 disabled:opacity-50">检查版本</button>
            <button data-backend-action="update" ${disabled} class="rounded bg-blue-600 px-4 py-2 text-white disabled:opacity-50">更新并编译</button>`;
    }
    async function runBackendAction(payload) {
        const result = await json('/api/simc-backend-binary/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': window.getCSRFToken() },
            body: JSON.stringify(payload),
        });
        window.showMessage(result.message || '后端操作已提交', 'success');
        await loadBackend();
    }
    async function lifecycle(resource, id, action) {
        await json(resourceUrl(resource, id), { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': window.getCSRFToken() }, body: JSON.stringify({ action }) });
        if (resource === 'tasks' || resource === 'batches') await loadTasks(resource);
        else if (resource === 'apl-storage') await loadApl(resource, 'simc-wb-apl-storage-list');
        else if (resource === 'apl-keywords') await loadApl(resource, 'simc-wb-apl-keyword-list');
        else if (resource === 'templates') await loadTemplates();
    }
    function activate(tab) {
        state.activePanel = tab || '';
        if (tab === 'tasks') loadTasks().catch(notify);
        if (tab === 'artifacts') loadArtifacts().catch(notify);
        if (tab === 'templates') loadTemplates().catch(notify);
        if (tab === 'apl') Promise.all([loadApl('apl-storage', 'simc-wb-apl-storage-list'), loadApl('apl-keywords', 'simc-wb-apl-keyword-list')]).catch(notify);
        if (tab === 'backend') loadBackend().catch(notify);
    }
    function deactivate(nextPanel) {
        if (nextPanel === 'tasks') return;
        state.activePanel = nextPanel || '';
        state.taskRequestSerial += 1;
        if (state.taskAbortController) state.taskAbortController.abort();
        state.taskAbortController = null;
        state.taskFetchInFlight = false;
        scheduleTaskRefresh(false);
    }
    window.simcWorkbenchLoadPanel = activate;
    window.simcWorkbenchDeactivatePanel = deactivate;
    window.simcWorkbenchLoadTaskResource = (resource, page = 1) => {
        state.activePanel = 'tasks';
        return loadTasks(resource, page).catch(notify);
    };
    const notify = error => window.showMessage(String(error.message || error), 'error');
    document.addEventListener('DOMContentLoaded', () => {
        const root = document.getElementById('simc-workbench');
        if (!root) return;
        root.addEventListener('click', event => {
            const aplCreate = event.target.closest('[data-inline-create="apl-storage"]');
            if (aplCreate) renderAplStorageForm();
            const templateCreate = event.target.closest('[data-inline-create="templates"]');
            if (templateCreate) renderTemplateForm();
            const aplKeywordCreate = event.target.closest('[data-inline-create="apl-keywords"]');
            if (aplKeywordCreate) renderAplKeywordForm();
            const aplAction = event.target.closest('[data-apl-action]');
            if (aplAction) {
                const id = idOf(aplAction.dataset.id);
                const actionName = aplAction.dataset.aplAction;
                if (actionName === 'cancel') closeAplStorageForm();
                else if (actionName === 'use' && id) useAplForSimulation(id).catch(notify);
                else if (actionName === 'edit' && id) fetchAplStorageDetail(id).then(renderAplStorageForm).catch(notify);
                else if ((actionName === 'archive' || actionName === 'restore') && id) lifecycle('apl-storage', id, actionName).catch(notify);
            }
            const aplKeywordAction = event.target.closest('[data-apl-keyword-action]');
            if (aplKeywordAction) {
                const actionName = aplKeywordAction.dataset.aplKeywordAction;
                if (actionName === 'cancel') closeAplKeywordForm();
            }
            const templateAction = event.target.closest('[data-template-action]');
            if (templateAction) {
                const actionName = templateAction.dataset.templateAction;
                if (actionName === 'cancel') closeTemplateForm();
                else if (actionName === 'close-detail') closeTemplateDetail();
            }
            const subtab = event.target.closest('[data-task-subtab]');
            if (subtab) loadTasks(subtab.dataset.taskSubtab).catch(notify);
            const paginationBtn = event.target.closest('[data-pagination-page]');
            if (paginationBtn) {
                const page = parseInt(paginationBtn.dataset.paginationPage, 10);
                const resource = paginationBtn.dataset.paginationResource;
                if (page > 0) loadTasks(resource, page).catch(notify);
            }
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
                else if (name === 'compare' && resource === 'batches') showBatchComparison(id).catch(notify);
                else if (name === 'template-detail') showTemplateDetail(id).catch(notify);
                else if (name === 'template-edit') {
                    const row = (state.rows.templates || []).find(item => idOf(item.id) === id);
                    if (row) renderTemplateForm(row);
                }
                else if (name === 'archive' || name === 'restore' || name === 'rerun') lifecycle(resource, id, name).catch(notify);
            }
            const backendAction = event.target.closest('[data-backend-action]');
            if (backendAction && !backendAction.disabled) {
                const actionName = backendAction.dataset.backendAction;
                if (actionName === 'check' || actionName === 'update') {
                    runBackendAction({ action: actionName }).catch(notify);
                }
            }
            const refresh = event.target.closest('[data-simc-refresh]');
            if (refresh) activate(refresh.dataset.simcRefresh === 'backend' ? 'backend' : refresh.dataset.simcRefresh);
        });
        root.addEventListener('change', event => {
            const autoUpdate = event.target.closest('[data-backend-auto-update]');
            if (autoUpdate && !autoUpdate.disabled) {
                runBackendAction({ action: 'set_auto_update', auto_update: autoUpdate.checked }).catch(notify);
            }
        });
        root.addEventListener('submit', event => {
            const aplStorageForm = event.target.closest('[data-apl-storage-form]');
            if (aplStorageForm) {
                event.preventDefault();
                saveAplStorage(aplStorageForm).catch(notify);
                return;
            }
            const templateForm = event.target.closest('[data-template-form]');
            if (templateForm) {
                event.preventDefault();
                saveTemplate(templateForm).catch(notify);
                return;
            }
            const aplKeywordForm = event.target.closest('[data-apl-keyword-form]');
            if (aplKeywordForm) {
                event.preventDefault();
                saveAplKeyword(aplKeywordForm).catch(notify);
                return;
            }
        });
        document.getElementById('simc-wb-convert')?.addEventListener('click', async () => {
            try { document.getElementById('simc-wb-convert-output').value = await window.convertText(document.getElementById('simc-wb-convert-input').value, document.getElementById('simc-wb-convert-mode').value); } catch (error) { notify(error); }
        });
    });
})();
