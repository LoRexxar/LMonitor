/* SimC 十模型内联工作台：专用 API、事件委托和安全结果预览。version: 20260716e */
(() => {
    'use strict';
    const apiRoot = '/api/simc-workbench/';
    const state = {
        activePanel: '', taskResource: 'tasks', taskPage: 1, taskFetchInFlight: false,
        taskRequestSerial: 0, taskPollTimer: null, taskAbortController: null,
        artifactPage: 1, artifactPageSize: 20, artifactTaskId: '', artifactType: '',
        artifactRequestSerial: 0, artifactAbortController: null,
        detailRequestSerial: 0, detailAbortController: null, detailRequestKey: '',
        dialogStack: [],
        templateType: '', rows: Object.create(null),
        aplKeywordQuery: '', aplKeywordCanWrite: false,
        aplQuery: '',
        aplLoadState: {
            personal: { loading: false, error: '' },
            default: { loading: false, error: '' },
        },
        converterMode: 'apl_to_cn', converterRequestSerial: 0,
        defaultAplCopyInFlight: new Set(),
        resourceAbortControllers: Object.create(null),
        resourceRequestSerials: Object.create(null),
    };
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
    function renderState(host, kind, text, retry) {
        if (!host) return;
        const icon = kind === 'loading' ? '<i class="fas fa-spinner fa-spin mr-2"></i>' : '';
        const retryButton = retry ? `<button type="button" data-wb-retry="${esc(retry)}" class="simc-touch-action mt-3 rounded-lg border bg-white px-3 py-2 text-blue-700">原位重试</button>` : '';
        host.innerHTML = `<div class="rounded-xl border border-dashed p-5 text-center text-gray-500">${icon}${esc(text)}${retryButton}</div>`;
    }
    function captureDialogState() {
        const dialog = document.getElementById('simc-workbench-dialog');
        const body = document.getElementById('simc-dialog-body');
        if (!dialog || dialog.classList.contains('hidden') || !body || !body.innerHTML.trim()) return null;
        return {
            html: body.innerHTML,
            title: document.getElementById('simc-dialog-title')?.textContent || '',
            scrollTop: document.getElementById('simc-workbench-dialog-content')?.scrollTop || 0,
        };
    }
    function pushDialogState() {
        const snapshot = captureDialogState();
        if (snapshot) state.dialogStack.push(snapshot);
    }
    function restoreDialogState() {
        const snapshot = state.dialogStack.pop();
        if (!snapshot) return false;
        cancelDetailRequest();
        const body = document.getElementById('simc-dialog-body');
        const title = document.getElementById('simc-dialog-title');
        const panel = document.getElementById('simc-workbench-dialog-content');
        if (body) body.innerHTML = snapshot.html;
        if (title) title.textContent = snapshot.title;
        if (panel) panel.scrollTop = snapshot.scrollTop;
        return true;
    }
    function openDialog(type) {
        if (typeof window.openSimcWorkbenchDialog !== 'function') throw new Error('统一详情对话框不可用');
        state.dialogStack = [];
        window.openSimcWorkbenchDialog(type, null);
        return document.getElementById('simc-dialog-body');
    }
    function closeDialog() {
        cancelDetailRequest();
        if (typeof window.closeSimcWorkbenchDialog === 'function') window.closeSimcWorkbenchDialog();
        state.dialogStack = [];
    }
    function beginResourceRequest(resource) {
        const key = String(resource);
        state.resourceRequestSerials[key] = (state.resourceRequestSerials[key] || 0) + 1;
        if (state.resourceAbortControllers[key]) state.resourceAbortControllers[key].abort();
        const controller = new AbortController();
        state.resourceAbortControllers[key] = controller;
        return { serial: state.resourceRequestSerials[key], key, controller };
    }
    function isCurrentResourceRequest(request) {
        return request.serial === state.resourceRequestSerials[request.key]
            && request.controller === state.resourceAbortControllers[request.key];
    }
    function cancelDetailRequest() {
        state.detailRequestSerial += 1;
        if (state.detailAbortController) state.detailAbortController.abort();
        state.detailAbortController = null;
        state.detailRequestKey = '';
    }
    function beginDetailRequest(key) {
        cancelDetailRequest();
        const controller = new AbortController();
        state.detailAbortController = controller;
        state.detailRequestKey = String(key);
        return { serial: state.detailRequestSerial, key: state.detailRequestKey, controller };
    }
    function isCurrentDetailRequest(request) {
        return request.serial === state.detailRequestSerial
            && request.key === state.detailRequestKey
            && request.controller === state.detailAbortController;
    }
    function syncTaskSubtabs(resource) {
        const normalized = resource === 'batches' ? 'batches' : 'tasks';
        document.querySelectorAll('[data-task-subtab]').forEach(button => {
            const selected = button.dataset.taskSubtab === normalized;
            button.setAttribute('aria-selected', String(selected));
            button.classList.toggle('active', selected);
            button.classList.toggle('bg-blue-600', selected);
            button.classList.toggle('text-white', selected);
            button.classList.toggle('border', !selected);
        });
        document.querySelectorAll('.simc-model-entry[data-simc-model="tasks"], .simc-model-entry[data-simc-model="batches"]').forEach(button => {
            const selected = button.dataset.simcModel === normalized;
            button.setAttribute('aria-selected', String(selected));
            button.classList.toggle('active', selected);
        });
    }
    const buttons = (resource, row) => {
        const id = idOf(row.id);
        if (!id) return '';
        const active = row.is_active !== false;
        return `<button data-wb-action="detail" data-resource="${esc(resource)}" data-id="${id}" class="text-blue-700">详情</button> <button data-wb-action="${active ? 'archive' : 'restore'}" data-resource="${esc(resource)}" data-id="${id}" class="text-amber-700">${active ? '停用' : '恢复'}</button>`;
    };
    async function loadTasks(resource = state.taskResource, page = 1) {
        const normalized = resource === 'batches' ? 'batches' : 'tasks';
        state.taskResource = normalized;
        state.taskPage = Number.isSafeInteger(Number(page)) && Number(page) > 0 ? Number(page) : 1;
        const requestedResource = state.taskResource;
        const requestedPage = state.taskPage;
        const requestSerial = ++state.taskRequestSerial;
        const host = document.getElementById('simc-wb-task-list');
        if (!host) return;
        syncTaskSubtabs(requestedResource);
        renderState(host, 'loading', `正在加载${requestedResource === 'batches' ? '批次' : '任务'}…`);
        if (state.taskAbortController) state.taskAbortController.abort();
        const controller = new AbortController();
        state.taskAbortController = controller;
        state.taskFetchInFlight = true;
        let data;
        try {
            data = await json(`${resourceUrl(requestedResource)}?page=${requestedPage}&page_size=20`, { signal: controller.signal });
        } catch (error) {
            if (error.name === 'AbortError') return;
            if (requestSerial === state.taskRequestSerial && state.activePanel === 'tasks') renderState(host, 'error', '加载失败，请稍后重试', 'tasks');
            return;
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
                return `<article class="simc-responsive-row flex flex-wrap justify-between gap-3 border-b p-3"><div class="flex-1 min-w-0"><b>${esc(row.name || `#${idOf(row.id)}`)}</b><div class="text-xs text-gray-500">${esc(row.status_label)} · ${esc(row.created_at)}</div>${progressBar}</div><div class="flex gap-3"><button data-wb-action="detail" data-resource="tasks" data-id="${idOf(row.id)}" class="simc-touch-action text-blue-700">查看任务</button>${[2, 3].includes(Number(row.status)) ? `<button data-wb-action="rerun" data-resource="tasks" data-id="${idOf(row.id)}" class="simc-touch-action text-emerald-700">重跑</button>` : ''}</div></article>`;
            }).join('') : empty('暂无记录');
        } else {
            host.innerHTML = data.data.length ? data.data.map(row => {
                const progressBar = row.total > 0 ? `<div class="w-full bg-gray-200 rounded-full h-1.5 mt-1"><div class="bg-blue-600 h-1.5 rounded-full" style="width:${row.percent || 0}%"></div></div>` : '';
                const statusText = `${row.succeeded}/${row.total} 成功 · ${row.failed > 0 ? `${row.failed} 失败 · ` : ''}${row.pending} 待运行`;
                const compareButton = row.report_url ? `<button data-wb-action="compare" data-resource="batches" data-id="${idOf(row.id)}" class="text-purple-700">内联比较</button>` : '';
                return `<article class="simc-responsive-row flex flex-wrap justify-between gap-3 border-b p-3"><div class="flex-1 min-w-0"><b>${esc(row.name || `#${idOf(row.id)}`)}</b><div class="text-xs text-gray-500">${statusText} · ${esc(row.created_at)}</div>${progressBar}</div><div class="flex gap-3">${compareButton}<button data-wb-action="detail" data-resource="batches" data-id="${idOf(row.id)}" class="simc-touch-action text-blue-700">查看批次</button></div></article>`;
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
        if (resource === 'batches') window.openSimcWorkbenchDialog('batch-detail', null);
        else window.openSimcWorkbenchDialog('task-detail', null);
        const host = document.getElementById('simc-dialog-body');
        if (!host) return;
        const detailRequest = beginDetailRequest(`task:${resource}:${id}`);
        renderState(host, 'loading', '正在加载详情…');
        let data;
        try {
            data = await json(resourceUrl(resource, id), { signal: detailRequest.controller.signal });
        } catch (error) {
            if (error.name === 'AbortError') return;
            throw error;
        }
        if (!isCurrentDetailRequest(detailRequest)) return;
        const row = data.data || {};
        const status = row.status_label || ({ 0: '待运行', 1: '运行中', 2: '成功', 3: '失败' }[Number(row.status)] || '未知');
        const resultSummary = row.result_summary || {};
        const dpsNumber = Number(resultSummary.dps);
        const dps = Number.isFinite(dpsNumber) ? Math.round(dpsNumber).toLocaleString() : '无 DPS 数据';
        const members = resource === 'batches' && Array.isArray(row.tasks) ? row.tasks : [];
        const memberList = members.length ? `<div class="mt-4"><h5 class="font-semibold">批次成员</h5>${members.map(member => `<article class="simc-responsive-row mt-2 flex flex-wrap items-center justify-between gap-2 rounded-lg border bg-white p-3"><div><b>${esc(member.name || `任务 #${idOf(member.id)}`)}</b><div class="text-xs text-gray-500">${esc(member.status_label || member.status)} · ${esc(member.updated_at)}</div></div>${member.can_view !== false ? `<button data-wb-action="detail" data-resource="tasks" data-id="${idOf(member.id)}" class="simc-touch-action rounded-lg border px-3 py-2 text-blue-700">查看任务</button>` : ''}</article>`).join('')}</div>` : (resource === 'batches' ? empty('此批次暂无可查看成员') : '');
        const artifacts = Array.isArray(row.artifacts) ? row.artifacts : [];
        const report = artifacts.find(a => a.can_preview === true && String(a.preview_url || '') === resourceUrl('artifacts', idOf(a.id)) + 'preview/');
        const artifactList = artifacts.length ? `<div class="mt-4"><h5 class="font-semibold">结果产物</h5>${artifacts.map(a => `<div class="mt-2 text-sm">${esc(a.file_name || a.artifact_type || '产物')}${a.can_preview === true ? ` <button data-artifact-preview="${idOf(a.id)}" data-preview-url="${esc(a.preview_url)}" data-title="${esc(a.file_name)}" class="text-blue-700">在此预览</button>` : ''}</div>`).join('')}</div>` : '<p class="mt-4 text-sm text-gray-500">暂无结果产物</p>';
        const reportFrame = report
            ? `<div class="mt-4"><h5 class="font-semibold mb-2">模拟报告</h5>${window.renderSimcArtifactFrame(report.preview_url, report.file_name || 'SimC 报告')}</div>`
            : (resource === 'tasks' && row.has_report === true && row.report_preview_url === `${resourceUrl('tasks', id)}report-preview/`
                ? `<div class="mt-4"><h5 class="font-semibold mb-2">历史模拟报告</h5>${window.renderSimcArtifactFrame(row.report_preview_url, 'SimC 历史报告')}</div>`
                : '');
        host.innerHTML = `<div class="flex flex-wrap justify-between gap-2"><h4 class="font-bold">${resource === 'batches' ? '批次详情' : '任务详情'}：${esc(row.name || `#${id}`)}</h4><button class="simc-touch-action" data-wb-close-detail>关闭</button></div><dl class="mt-3 grid gap-2 text-sm md:grid-cols-4"><div>状态：${esc(status)}</div><div>类型：${esc(row.task_type || row.batch_type)}</div><div>DPS：${esc(dps)}</div><div>更新时间：${esc(row.updated_at)}</div></dl>${memberList}${artifactList}${reportFrame}`;
    }
    async function showBatchComparison(id) {
        window.openSimcWorkbenchDialog('batch-detail', null);
        const host = document.getElementById('simc-dialog-body');
        if (!host) return;
        renderState(host, 'loading', '正在加载批次比较…');
        const detailRequest = beginDetailRequest(`comparison:${id}`);
        let data;
        try {
            data = await json(`/api/simc-regular-compare/?batch_id=${id}&summary=1`, { signal: detailRequest.controller.signal });
        } catch (error) {
            if (error.name === 'AbortError') return;
            throw error;
        }
        if (!isCurrentDetailRequest(detailRequest)) return;
        const rows = Array.isArray(data.data?.tasks) ? data.data.tasks : [];
        const tableRows = rows.map(row => {
            const validDps = row.dps != null && Number.isFinite(Number(row.dps));
            const dps = validDps ? Math.round(Number(row.dps)).toLocaleString() : (row.status === 3 || row.is_valid === false ? '无效结果' : '无 DPS 数据');
            const deltaValue = row.delta ?? row.delta_dps;
            const delta = validDps && deltaValue != null && Number.isFinite(Number(deltaValue)) ? `${Number(deltaValue) >= 0 ? '+' : ''}${Math.round(Number(deltaValue)).toLocaleString()}` : '-';
            const percent = validDps && row.delta_percent != null ? `${Number(row.delta_percent) >= 0 ? '+' : ''}${row.delta_percent}%` : '-';
            return `<tr class="border-t"><td class="p-2">${esc(row.rank || '-')}</td><td class="p-2">${esc(row.label || row.name)}</td><td class="p-2 text-right">${esc(dps)}</td><td class="p-2 text-right">${esc(delta)}</td><td class="p-2 text-right">${esc(percent)}</td></tr>`;
        }).join('');
        host.innerHTML = `<div class="flex justify-between gap-3"><div><h4 class="font-bold">结果比较</h4><p class="text-xs text-gray-500">仅展示已解析的安全结果摘要</p></div><button data-wb-close-detail>关闭</button></div><div class="mt-3 overflow-x-auto"><table class="w-full min-w-[560px] text-sm"><thead><tr class="text-left text-gray-500"><th class="p-2">排名</th><th class="p-2">方案</th><th class="p-2 text-right">DPS</th><th class="p-2 text-right">差值</th><th class="p-2 text-right">差值%</th></tr></thead><tbody>${tableRows || `<tr><td colspan="5" class="p-5 text-center text-gray-500">无有效成员结果</td></tr>`}</tbody></table></div>`;
    }
    async function loadArtifacts(page = state.artifactPage) {
        const host = document.getElementById('simc-wb-artifact-list');
        if (!host) return;
        state.artifactPage = Math.max(1, Number.parseInt(String(page), 10) || 1);
        const requestedPage = state.artifactPage;
        const requestSerial = ++state.artifactRequestSerial;
        const query = new URLSearchParams({ page: String(requestedPage), page_size: String(state.artifactPageSize) });
        if (state.artifactTaskId) query.set('task_id', state.artifactTaskId);
        if (state.artifactType) query.set('artifact_type', state.artifactType);
        // Keep the explicit request shape visible for contract review: page_size=${state.artifactPageSize}.
        renderState(host, 'loading', '正在加载结果产物…');
        if (state.artifactAbortController) state.artifactAbortController.abort();
        const controller = new AbortController();
        state.artifactAbortController = controller;
        let data;
        try {
            data = await json(`${resourceUrl('artifacts')}?${query.toString()}`, { signal: controller.signal });
        } catch (error) {
            if (error.name === 'AbortError') return;
            if (requestSerial === state.artifactRequestSerial && state.activePanel === 'artifacts') {
                renderState(host, 'error', '结果产物加载失败', 'artifacts');
            }
            return;
        } finally {
            if (requestSerial === state.artifactRequestSerial && state.artifactAbortController === controller) {
                state.artifactAbortController = null;
            }
        }
        if (requestSerial !== state.artifactRequestSerial || state.activePanel !== 'artifacts') return;
        const rows = Array.isArray(data.data) ? data.data : [];
        host.innerHTML = rows.length ? rows.map(row => {
            const previewButton = row.can_preview === true
                ? `<button data-artifact-preview="${idOf(row.id)}" data-preview-url="${esc(row.preview_url)}" data-title="${esc(row.file_name)}" data-meta="${esc(`${row.task_name || ''} · ${row.artifact_type || ''} · ${row.created_at || ''}`)}" class="simc-touch-action text-blue-700">安全预览</button>`
                : '<span class="text-xs text-gray-400">仅供下载结果记录</span>';
            return `<article class="simc-responsive-row flex flex-wrap justify-between gap-3 border-b p-3"><div class="min-w-0"><b class="break-all">${esc(row.file_name)}</b><div class="text-xs text-gray-500">任务 ${esc(row.task_name)} · ${esc(row.artifact_type)} · ${esc(row.file_size)} bytes · ${esc(row.created_at)}</div></div>${previewButton}</article>`;
        }).join('') : empty('当前筛选条件下暂无结果产物');
        renderArtifactPagination(data.pagination || {});
    }
    function renderArtifactPagination(pagination) {
        const host = document.getElementById('simc-wb-artifact-pagination');
        if (!host) return;
        const page = Number(pagination.page || state.artifactPage);
        const pages = Number(pagination.total_pages || 0);
        host.innerHTML = pages > 1 ? `<div class="simc-pagination flex flex-wrap items-center justify-between gap-2 text-sm"><span>第 ${page}/${pages} 页 · 共 ${Number(pagination.total || 0)} 项</span><div class="flex gap-2">${page > 1 ? '<button class="simc-touch-action rounded border px-3 py-2" data-artifact-page="prev">上一页</button>' : ''}${page < pages ? '<button class="simc-touch-action rounded border px-3 py-2" data-artifact-page="next">下一页</button>' : ''}</div></div>` : '';
    }
    function previewArtifact(button) {
        const id = idOf(button.dataset.artifactPreview);
        const url = String(button.dataset.previewUrl || '');
        if (!id || url !== resourceUrl('artifacts', id) + 'preview/') return;
        const host = button.closest?.('#simc-dialog-body') || document.getElementById('simc-wb-artifact-detail');
        if (!host) return;
        host.classList.remove('hidden');
        const panel = document.getElementById('simc-workbench-dialog-content');
        if (host.id === 'simc-dialog-body') pushDialogState();
        host.dataset.previewUrl = url;
        host.dataset.previewTitle = button.dataset.title || 'SimC 结果预览';
        host.innerHTML = `<div class="flex flex-wrap items-start justify-between gap-2"><div><h4 class="font-bold">${esc(host.dataset.previewTitle)}</h4><p class="text-xs text-gray-500">${esc(button.dataset.meta)}</p></div><button class="simc-touch-action" data-artifact-preview-action="close">返回任务详情</button></div><div data-artifact-frame class="mt-3"><div class="p-5 text-center text-gray-500"><i class="fas fa-spinner fa-spin mr-2"></i>正在加载安全预览…</div>${window.renderSimcArtifactFrame(url, host.dataset.previewTitle)}</div><button class="simc-touch-action mt-3 hidden rounded border px-3 py-2 text-blue-700" data-artifact-preview-action="retry">原位重试</button>`;
        if (panel) panel.scrollTop = 0;
        const frame = host.querySelector('iframe');
        const retry = host.querySelector('[data-artifact-preview-action="retry"]');
        frame?.addEventListener('load', () => host.querySelector('[data-artifact-frame] > div')?.remove(), { once: true });
        frame?.addEventListener('error', () => { host.querySelector('[data-artifact-frame]').innerHTML = '<p class="p-5 text-center text-red-600">预览加载失败</p>'; retry?.classList.remove('hidden'); }, { once: true });
    }
    async function loadTemplates() {
        const host = document.getElementById('simc-wb-template-list');
        const request = beginResourceRequest('templates');
        renderState(host, 'loading', '正在加载模板…');
        let data;
        try { data = await json(resourceUrl('templates'), { signal: request.controller.signal }); }
        catch (error) {
            if (error.name === 'AbortError') return;
            if (isCurrentResourceRequest(request)) renderState(host, 'error', '模板加载失败', 'templates');
            return;
        }
        if (!isCurrentResourceRequest(request)) return;
        state.rows.templates = data.data || [];
        state.canWriteTemplates = data.can_write === true;
        const rows = state.templateType ? data.data.filter(row => row.template_type === state.templateType) : data.data;
        host.innerHTML = rows.length ? rows.map(row => {
            const active = row.is_active !== false;
            const readOnly = row.read_only === true;
            const ownership = !readOnly ? '我的模板可编辑' : (row.source === 'simc_upstream' ? '上游同步只读' : '系统内置只读');
            return `<article class="simc-responsive-row flex flex-wrap justify-between gap-3 border-b p-3"><div><b>${esc(row.name)}</b><div class="text-xs text-gray-500">${esc(row.type_label)} · ${esc(row.spec)} · ${ownership} · ${active ? '启用' : '已停用'}</div></div><div class="flex flex-wrap gap-2">${!readOnly ? `<button data-wb-action="template-edit" data-resource="templates" data-id="${idOf(row.id)}" class="simc-touch-action text-blue-700">编辑</button><button data-wb-action="${active ? 'archive' : 'restore'}" data-resource="templates" data-id="${idOf(row.id)}" class="simc-touch-action text-amber-700">${active ? '停用' : '恢复'}</button>` : ''}<button data-wb-action="template-detail" data-resource="templates" data-id="${idOf(row.id)}" class="simc-touch-action text-slate-700">查看</button></div></article>`;
        }).join('') : empty('此类型暂无模板');
        document.querySelector('[data-inline-create="templates"]')?.classList.toggle('hidden', !state.canWriteTemplates);
    }
    function renderTemplateForm(row = null) {
        window.openSimcWorkbenchDialog('template-form', null);
        const host = document.getElementById('simc-dialog-body');
        if (!host) return;
        const typeOptions = [
            { value: 'base_template', label: '基础模板' },
            { value: 'default_apl', label: '默认 APL' },
            { value: 'custom_apl', label: '自定义 APL' },
            { value: 'custom_player', label: '用户自定义装备' },
        ].map(opt => `<option value="${esc(opt.value)}" ${row?.template_type === opt.value ? 'selected' : ''}>${esc(opt.label)}</option>`).join('');
        host.innerHTML = `<form data-template-form class="rounded-xl border border-blue-200 bg-blue-50 p-4">
            <input type="hidden" name="id" value="${idOf(row?.id)}">
            <label class="block text-sm font-medium text-gray-700">名称<input name="name" required maxlength="200" value="${esc(row?.name)}" class="mt-1 w-full rounded-lg border bg-white p-2"></label>
            <label class="mt-3 block text-sm font-medium text-gray-700">类型<select name="template_type" required class="mt-1 w-full rounded-lg border bg-white p-2">${typeOptions}</select></label>
            <label class="mt-3 block text-sm font-medium text-gray-700">专精标识<input name="spec" maxlength="100" value="${esc(row?.spec || 'default')}" class="mt-1 w-full rounded-lg border bg-white p-2"></label>
            <label class="mt-3 block text-sm font-medium text-gray-700">职业<input name="class_name" maxlength="50" value="${esc(row?.class_name)}" class="mt-1 w-full rounded-lg border bg-white p-2"></label>
            <label class="mt-3 block text-sm font-medium text-gray-700">内容<textarea name="content" required rows="12" class="mt-1 w-full rounded-lg border bg-white p-3 font-mono text-xs">${esc(row?.content)}</textarea></label>
            <div class="mt-3 flex gap-2"><button type="submit" class="rounded-lg bg-blue-600 px-4 py-2 text-white">保存</button><button type="button" data-template-action="cancel" class="rounded-lg border bg-white px-4 py-2">取消</button></div>
        </form>`;
    }
    function closeTemplateForm() {
        closeDialog();
    }
    function closeTemplateDetail() {
        closeDialog();
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
        payload.template_type = String(formData.get('template_type') || '').trim();
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
        window.openSimcWorkbenchDialog('template-detail', null);
        const host = document.getElementById('simc-dialog-body');
        if (!host) return;
        renderState(host, 'loading', '正在加载模板…');
        const detailRequest = beginDetailRequest(`template:${id}`);
        let data;
        try {
            data = await json(resourceUrl('templates', id), { signal: detailRequest.controller.signal });
        } catch (error) {
            if (error.name === 'AbortError') return;
            throw error;
        }
        if (!isCurrentDetailRequest(detailRequest)) return;
        const row = data.data || {};
        host.innerHTML = `<div class="flex justify-between mb-3"><h4 class="font-bold">${esc(row.name)}</h4><button data-template-action="close-detail" class="text-slate-500">关闭</button></div><dl class="grid gap-2 text-sm"><div>类型：${esc(row.type_label)}</div><div>专精：${esc(row.spec)}</div><div>职业：${esc(row.class_name || '-')}</div><div>来源：${esc(row.source === 'simc_upstream' ? 'SimC上游' : '用户维护')}</div><div>状态：${row.is_active ? '启用' : '已停用'}</div></dl><div class="mt-3"><label class="text-sm font-medium text-gray-700">内容</label><pre class="mt-1 rounded border bg-slate-50 p-3 text-xs overflow-auto max-h-96">${esc(row.content)}</pre></div>`;
    }
    function renderAplKeywordForm(row = null) {
        const host = openDialog('keyword-form');
        if (!host) return;
        host.innerHTML = `<form data-apl-keyword-form class="rounded-xl border border-blue-200 bg-blue-50 p-4">
            <input type="hidden" name="id" value="${idOf(row?.id)}">
            <label class="block text-sm font-medium text-gray-700">APL 关键词<input name="apl_keyword" ${row ? 'readonly' : ''} required maxlength="100" value="${esc(row?.apl_keyword)}" class="mt-1 w-full rounded-lg border bg-white p-2"></label>
            <label class="mt-3 block text-sm font-medium text-gray-700">中文关键词<input name="cn_keyword" required maxlength="100" value="${esc(row?.cn_keyword)}" class="mt-1 w-full rounded-lg border bg-white p-2"></label>
            <label class="mt-3 block text-sm font-medium text-gray-700">描述<input name="description" maxlength="500" value="${esc(row?.description)}" class="mt-1 w-full rounded-lg border bg-white p-2"></label>
            <div class="mt-3 flex gap-2"><button type="submit" class="rounded-lg bg-blue-600 px-4 py-2 text-white">保存</button><button type="button" data-apl-keyword-action="cancel" class="rounded-lg border bg-white px-4 py-2">取消</button></div>
        </form>`;
    }
    function closeAplKeywordForm() {
        closeDialog();
    }
    async function saveAplKeyword(form) {
        const formData = new FormData(form);
        const id = idOf(formData.get('id'));
        const payload = {
            cn_keyword: String(formData.get('cn_keyword') || '').trim(),
            description: String(formData.get('description') || '').trim(),
        };
        if (!id) payload.apl_keyword = String(formData.get('apl_keyword') || '').trim();
        await json(resourceUrl('apl-keywords', id), {
            method: id ? 'PUT' : 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': window.getCSRFToken() },
            body: JSON.stringify(payload),
        });
        closeAplKeywordForm();
        await loadApl('apl-keywords', 'simc-wb-apl-keyword-list');
        window.showMessage(id ? '关键词已更新' : '关键词已创建', 'success');
    }
    async function showAplKeywordDetail(id) {
        const host = openDialog('keyword-detail');
        if (!host) return;
        const detailRequest = beginDetailRequest(`keyword:${id}`);
        renderState(host, 'loading', '正在加载关键词…');
        let data;
        try {
            data = await json(resourceUrl('apl-keywords', id), { signal: detailRequest.controller.signal });
        } catch (error) {
            if (error.name === 'AbortError') return;
            throw error;
        }
        if (!isCurrentDetailRequest(detailRequest)) return;
        const row = data.data || {};
        host.innerHTML = `<div class="flex flex-wrap justify-between gap-2"><h4 class="font-bold">规则关键词详情</h4><button class="simc-touch-action" data-apl-keyword-action="close-detail">关闭</button></div><dl class="mt-3 grid gap-2 text-sm"><div>APL 关键词：<code>${esc(row.apl_keyword)}</code></div><div>中文关键词：${esc(row.cn_keyword)}</div><div>说明：${esc(row.description || '-')}</div><div>状态：${row.is_active === false ? '已停用' : '启用'}</div></dl>`;
    }
    function renderAplStorageForm(row = null) {
        window.openSimcWorkbenchDialog('apl-form', null);
        const host = document.getElementById('simc-dialog-body');
        if (!host) return;
        host.innerHTML = `<form data-apl-storage-form class="rounded-xl border border-blue-200 bg-blue-50 p-4">
            <input type="hidden" name="id" value="${idOf(row?.id)}">
            <label class="block text-sm font-medium text-gray-700">标题<input name="title" required maxlength="200" value="${esc(row?.title)}" class="mt-1 w-full rounded-lg border bg-white p-2"></label>
            <label class="mt-3 block text-sm font-medium text-gray-700">专精标识<input name="spec" maxlength="100" placeholder="例如 warrior_fury" value="${esc(row?.spec)}" class="mt-1 w-full rounded-lg border bg-white p-2"><span class="mt-1 block text-xs text-gray-500">用于在列表中标记和筛选适用专精。</span></label>
            <label class="mt-3 block text-sm font-medium text-gray-700">APL 内容<textarea name="apl_code" required rows="12" class="mt-1 w-full rounded-lg border bg-white p-3 font-mono text-xs">${esc(row?.apl_code)}</textarea></label>
            <div class="mt-3 flex gap-2"><button type="submit" class="rounded-lg bg-blue-600 px-4 py-2 text-white">保存</button><button type="button" data-apl-action="cancel" class="rounded-lg border bg-white px-4 py-2">取消</button></div>
        </form>`;
    }
    function closeAplStorageForm() {
        closeDialog();
    }
    function renderAplKeywordTable() {
        const host = document.getElementById('simc-wb-apl-keyword-list');
        const summary = document.getElementById('simc-wb-apl-keyword-summary');
        if (!host) return;
        const rows = state.rows['apl-keywords'] || [];
        const query = state.aplKeywordQuery.trim().toLocaleLowerCase();
        const filteredRows = query ? rows.filter(row => {
            const searchable = [row.apl_keyword, row.cn_keyword, row.description]
                .map(value => String(value || '').toLocaleLowerCase())
                .join('\n');
            return searchable.includes(query);
        }) : rows;
        if (summary) {
            summary.textContent = query
                ? `共 ${rows.length} 条 · 筛选后 ${filteredRows.length} 条`
                : `共 ${rows.length} 条`;
        }
        if (!filteredRows.length) {
            host.innerHTML = empty(query ? '无匹配结果' : '暂无数据');
            return;
        }
        const body = filteredRows.map(row => {
            const active = row.is_active !== false;
            const description = row.description || '-';
            const actions = `<div class="flex flex-wrap gap-1 sm:justify-end">
                <button data-wb-action="keyword-detail" data-resource="apl-keywords" data-id="${idOf(row.id)}" class="simc-touch-action text-slate-700 hover:bg-slate-100">查看</button>
                ${state.aplKeywordCanWrite ? `<button data-wb-action="keyword-edit" data-resource="apl-keywords" data-id="${idOf(row.id)}" class="simc-touch-action text-blue-700 hover:bg-blue-50">编辑</button><button data-wb-action="${active ? 'archive' : 'restore'}" data-resource="apl-keywords" data-id="${idOf(row.id)}" class="simc-touch-action ${active ? 'text-amber-700 hover:bg-amber-50' : 'text-emerald-700 hover:bg-emerald-50'}">${active ? '停用' : '恢复'}</button>` : ''}
            </div>`;
            return `<tr class="align-top transition-colors hover:bg-slate-50">
                <td class="px-3 py-2.5"><span class="simc-table-cell-label">APL 关键词</span><code class="break-all rounded bg-slate-100 px-1.5 py-1 text-xs text-slate-800">${esc(row.apl_keyword)}</code></td>
                <td class="px-3 py-2.5 text-slate-700"><span class="simc-table-cell-label">中文关键词</span><span>${esc(row.cn_keyword || '-')}</span></td>
                <td class="max-w-0 px-3 py-2.5 text-slate-500"><span class="simc-table-cell-label">说明</span><span class="simc-apl-description block" title="${esc(description)}">${esc(description)}</span></td>
                <td class="px-3 py-2.5"><span class="simc-table-cell-label">状态</span><span class="inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${active ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-100 text-slate-500'}">${active ? '启用' : '已停用'}</span></td>
                <td class="px-3 py-1.5 text-right"><span class="simc-table-cell-label">操作</span>${actions}</td>
            </tr>`;
        }).join('');
        host.innerHTML = `<div class="overflow-hidden rounded-xl border border-slate-200">
            <table class="simc-responsive-table w-full table-fixed text-sm">
                <colgroup><col class="w-[25%]"><col class="w-[18%]"><col class="w-[27%]"><col class="w-[12%]"><col class="w-[18%]"></colgroup>
                <thead class="bg-slate-50 text-xs font-medium text-slate-500"><tr>
                    <th class="border-b px-3 py-2.5 text-left">APL 关键词</th><th class="border-b px-3 py-2.5 text-left">中文关键词</th><th class="border-b px-3 py-2.5 text-left">说明</th><th class="border-b px-3 py-2.5 text-left">状态</th><th class="border-b px-3 py-2.5 text-right">操作</th>
                </tr></thead>
                <tbody class="divide-y divide-slate-100">${body}</tbody>
            </table>
        </div>`;
    }
    function renderUnifiedAplList() {
        const host = document.getElementById('simc-unified-apl-list');
        if (!host) return;
        const personalRows = (state.rows['apl-storage'] || []).map(row => ({ ...row, kind: 'personal' }));
        const defaultRows = (state.rows['default-apl'] || []).map(row => ({ ...row, kind: 'default' }));
        const allRows = [...personalRows, ...defaultRows];
        const query = state.aplQuery.trim().toLowerCase();
        const filteredRows = query ? allRows.filter(row => {
            const searchable = [
                row.title, row.name,
                row.kind === 'personal' ? row.apl_code : '',
                row.class_name, row.spec
            ].map(v => String(v || '').toLowerCase()).join('\n');
            return searchable.includes(query);
        }) : allRows;
        const statusParts = [];
        if (state.aplLoadState.personal.loading || state.aplLoadState.default.loading) {
            statusParts.push('<div class="mb-3 rounded-lg border border-blue-100 bg-blue-50 px-3 py-2 text-sm text-blue-700">正在加载 APL 资源…</div>');
        }
        if (state.aplLoadState.personal.error) {
            statusParts.push(`<div class="mb-3 rounded-lg border border-red-100 bg-red-50 px-3 py-2 text-sm text-red-700">${esc(state.aplLoadState.personal.error)}</div>`);
        }
        if (state.aplLoadState.default.error) {
            statusParts.push(`<div class="mb-3 rounded-lg border border-amber-100 bg-amber-50 px-3 py-2 text-sm text-amber-700">${esc(state.aplLoadState.default.error)}</div>`);
        }
        const rowsHtml = filteredRows.map(row => {
            if (row.kind === 'personal') {
                const active = row.is_active !== false;
                const sourceTag = '<span class="inline-block rounded-full bg-blue-100 px-2 py-0.5 text-xs text-blue-700">来源：个人</span>';
                const specTag = `<span class="inline-block rounded-full bg-violet-50 px-2 py-0.5 text-xs text-violet-700">专精：${esc(row.spec || '未标记')}</span>`;
                return `<article class="flex flex-wrap items-center justify-between gap-3 border-b p-3"><div class="min-w-0"><b class="break-words">${esc(row.title)}</b><div class="mt-1 flex flex-wrap items-center gap-2 text-xs text-gray-500">${sourceTag} ${specTag} ${active ? '<span class="text-emerald-600">启用中</span>' : '<span class="text-gray-400">已停用</span>'}</div></div><div class="flex flex-wrap gap-2">${active ? `<button data-my-apl-action="detail" data-id="${idOf(row.id)}" class="simc-touch-action text-slate-700">详情</button><button data-my-apl-action="use" data-id="${idOf(row.id)}" class="simc-touch-action text-emerald-700">用于模拟</button><button data-my-apl-action="edit" data-id="${idOf(row.id)}" class="simc-touch-action text-blue-700">编辑</button><button data-my-apl-action="archive" data-id="${idOf(row.id)}" class="simc-touch-action text-amber-700">停用</button>` : `<button data-my-apl-action="restore" data-id="${idOf(row.id)}" class="simc-touch-action text-emerald-700">恢复</button>`}</div></article>`;
            } else {
                const sourceTag = `<span class="inline-block rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-700">来源：${row.is_system ? '系统默认' : '个人模板'}</span>`;
                const specTag = `<span class="inline-block rounded-full bg-violet-50 px-2 py-0.5 text-xs text-violet-700">专精：${esc(row.spec || '未标记')}</span>`;
                return `<article class="flex flex-wrap items-center justify-between gap-3 border-b p-3"><div class="min-w-0"><b class="break-words">${esc(row.name)}</b><div class="mt-1 flex flex-wrap items-center gap-2 text-xs">${sourceTag} ${specTag}</div></div><div class="flex flex-wrap gap-2"><button data-default-apl-action="view" data-id="${idOf(row.id)}" class="simc-touch-action text-slate-700">查看</button><button data-default-apl-action="copy" data-id="${idOf(row.id)}" class="simc-touch-action text-blue-700">复制</button></div></article>`;
            }
        }).join('');
        host.innerHTML = statusParts.join('') + (rowsHtml || empty(query ? '无匹配结果' : '暂无数据'));
    }
    function renderMyAplList() {
        renderUnifiedAplList();
    }
    function renderDefaultAplList() {
        renderUnifiedAplList();
    }
    async function loadApl(resource, hostId) {
        const host = document.getElementById(hostId);
        const request = beginResourceRequest('apl');
        const unifiedApl = resource === 'apl-storage';
        if (unifiedApl) {
            state.aplLoadState.personal = { loading: true, error: '' };
            renderUnifiedAplList();
        } else {
            renderState(host, 'loading', '正在加载规则数据…');
        }
        let data;
        try { data = await json(resourceUrl(resource), { signal: request.controller.signal }); }
        catch (error) {
            if (error.name === 'AbortError') return;
            if (isCurrentResourceRequest(request)) {
                if (unifiedApl) {
                    state.aplLoadState.personal = { loading: false, error: '个人 APL 加载失败，已保留其他可用资源。' };
                    renderUnifiedAplList();
                } else {
                    renderState(host, 'error', '规则数据加载失败', resource);
                }
            }
            return;
        }
        if (!isCurrentResourceRequest(request)) return;
        state.rows[resource] = data.data || [];
        if (unifiedApl) state.aplLoadState.personal = { loading: false, error: '' };
        const canWrite = resource === 'apl-storage' || data.can_write === true;
        document.querySelector(`[data-inline-create="${resource}"]`)?.classList.toggle('hidden', !canWrite);
        if (resource === 'apl-keywords') {
            state.aplKeywordCanWrite = canWrite;
            renderAplKeywordTable();
            return;
        }
        renderUnifiedAplList();
    }
    async function loadDefaultAplLibrary() {
        const host = document.getElementById('simc-unified-apl-list');
        const request = beginResourceRequest('default-apl');
        if (!host) return;
        state.aplLoadState.default = { loading: true, error: '' };
        renderUnifiedAplList();
        let data;
        const params = new URLSearchParams({ library: 'default_apl' });
        try {
            data = await json(`${resourceUrl('templates')}?${params.toString()}`, { signal: request.controller.signal });
        } catch (error) {
            if (error.name === 'AbortError') return;
            if (isCurrentResourceRequest(request)) {
                state.aplLoadState.default = { loading: false, error: '系统默认 APL 加载失败，已保留其他可用资源。' };
                renderUnifiedAplList();
            }
            return;
        }
        if (!isCurrentResourceRequest(request)) return;
        state.rows['default-apl'] = data.data || [];
        state.aplLoadState.default = { loading: false, error: '' };
        renderUnifiedAplList();
    }
    async function fetchAplStorageDetail(id) {
        return (await json(`/api/apl-storage/${id}/`)).data;
    }
    async function showMyAplDetail(id) {
        const host = openDialog('apl-detail');
        if (!host) return;
        const detailRequest = beginDetailRequest(`my-apl:${id}`);
        renderState(host, 'loading', '正在加载APL详情…');
        let data;
        try {
            data = await json(`/api/apl-storage/${id}/`, { signal: detailRequest.controller.signal });
        } catch (error) {
            if (error.name === 'AbortError') return;
            throw error;
        }
        if (!isCurrentDetailRequest(detailRequest)) return;
        const row = data.data || {};
        host.innerHTML = `<div class="flex flex-wrap justify-between gap-2 mb-3"><h4 class="font-bold">我的APL详情</h4><button class="simc-touch-action" data-my-apl-detail-action="close">关闭</button></div><dl class="grid gap-2 text-sm"><div>标题：${esc(row.title)}</div><div>专精：${esc(row.spec || '未标记')}</div><div>状态：${row.is_active !== false ? '启用' : '已停用'}</div></dl><div class="mt-3"><label class="text-sm font-medium text-gray-700">APL内容</label><pre class="mt-1 rounded border bg-slate-50 p-3 text-xs overflow-auto max-h-96">${esc(row.apl_code)}</pre></div>`;
    }
    async function showDefaultAplDetail(id) {
        const host = openDialog('default-apl-detail');
        if (!host) return;
        const detailRequest = beginDetailRequest(`default-apl:${id}`);
        renderState(host, 'loading', '正在加载默认APL详情…');
        let data;
        try {
            data = await json(`${resourceUrl('templates', id)}?library=default_apl`, { signal: detailRequest.controller.signal });
        } catch (error) {
            if (error.name === 'AbortError') return;
            throw error;
        }
        if (!isCurrentDetailRequest(detailRequest)) return;
        const row = data.data || {};
        host.innerHTML = `<div class="flex flex-wrap justify-between gap-2 mb-3"><h4 class="font-bold">默认APL详情</h4><button class="simc-touch-action" data-default-apl-detail-action="close">关闭</button></div><dl class="grid gap-2 text-sm"><div>名称：${esc(row.name)}</div><div>职业：${esc(row.class_name)}</div><div>专精：${esc(row.spec)}</div><div>来源：${esc(row.source === 'simc_upstream' ? 'SimC上游' : '其他')}</div></dl><div class="mt-3"><label class="text-sm font-medium text-gray-700">内容（只读）</label><pre readonly class="mt-1 rounded border bg-slate-50 p-3 text-xs overflow-auto max-h-96">${esc(row.content)}</pre></div>`;
    }
    async function copyDefaultAplToMy(templateId, button) {
        if (state.defaultAplCopyInFlight.has(templateId)) return;
        state.defaultAplCopyInFlight.add(templateId);
        if (button) button.disabled = true;
        try {
            await json('/api/apl-storage/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': window.getCSRFToken() },
                body: JSON.stringify({ copy_template_id: templateId }),
            });
            await loadApl('apl-storage', 'simc-unified-apl-list');
            window.showMessage('已复制到我的APL', 'success');
        } finally {
            state.defaultAplCopyInFlight.delete(templateId);
            if (button) button.disabled = false;
        }
    }
    async function saveAplStorage(form) {
        const formData = new FormData(form);
        const id = idOf(formData.get('id'));
        const payload = {
            title: String(formData.get('title') || '').trim(),
            spec: String(formData.get('spec') || '').trim(),
            apl_code: String(formData.get('apl_code') || '').trim(),
        };
        if (id) payload.id = id;
        await json('/api/apl-storage/', {
            method: id ? 'PUT' : 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': window.getCSRFToken() },
            body: JSON.stringify(payload),
        });
        closeAplStorageForm();
        await loadApl('apl-storage', 'simc-unified-apl-list');
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
    window.loadSimcWorkbenchApl = () => loadApl('apl-storage', 'simc-unified-apl-list').catch(notify);
    async function loadBackend() {
        const host = document.getElementById('simc-wb-backend-status');
        const actions = document.getElementById('simc-wb-backend-actions');
        const request = beginResourceRequest('backend');
        let data;
        try { data = await json('/api/simc-backend-binary/', { signal: request.controller.signal }); }
        catch (error) {
            if (error.name === 'AbortError') return;
            return;
        }
        if (!isCurrentResourceRequest(request)) return;
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
        else if (resource === 'apl-storage') await loadApl(resource, 'simc-unified-apl-list');
        else if (resource === 'apl-keywords') await loadApl(resource, 'simc-wb-apl-keyword-list');
        else if (resource === 'templates') await loadTemplates();
    }
    function activate(tab) {
        state.activePanel = tab || '';
        if (tab === 'tasks') loadTasks().catch(notify);
        if (tab === 'artifacts') loadArtifacts().catch(notify);
        if (tab === 'templates') loadTemplates().catch(notify);
        if (tab === 'apl') {
            loadApl('apl-storage', 'simc-unified-apl-list').catch(notify);
            loadDefaultAplLibrary().catch(notify);
        }
        if (tab === 'apl-keywords') loadApl('apl-keywords', 'simc-wb-apl-keyword-list').catch(notify);
        if (tab === 'backend') loadBackend().catch(notify);
    }
    function deactivate(nextPanel) {
        state.activePanel = nextPanel || '';
        cancelDetailRequest();
        if (nextPanel !== 'tasks') {
            state.taskRequestSerial += 1;
            if (state.taskAbortController) state.taskAbortController.abort();
            state.taskAbortController = null;
            state.taskFetchInFlight = false;
            scheduleTaskRefresh(false);
        }
        if (nextPanel !== 'artifacts') {
            state.artifactRequestSerial += 1;
            if (state.artifactAbortController) state.artifactAbortController.abort();
            state.artifactAbortController = null;
        }
    }
    window.simcWorkbenchLoadPanel = activate;
    window.simcWorkbenchDeactivatePanel = deactivate;
    window.simcWorkbenchLoadTaskResource = (resource, page = 1) => {
        state.activePanel = 'tasks';
        const normalized = resource === 'batches' ? 'batches' : 'tasks';
        state.taskResource = normalized;
        syncTaskSubtabs(normalized);
        return loadTasks(resource, page).catch(notify);
    };
    const notify = error => window.showMessage(String(error.message || error), 'error');
    document.addEventListener('simc-dialog-closing', event => {
        if (event.detail?.reason === 'close') state.dialogStack = [];
        cancelDetailRequest();
    });
    document.addEventListener('simc-dialog-replace', cancelDetailRequest);
    document.addEventListener('DOMContentLoaded', () => {
        const root = document.getElementById('simc-workbench');
        if (!root) return;
        document.addEventListener('click', event => {
            const aplCreate = event.target.closest('[data-inline-create="apl-storage"]');
            if (aplCreate) renderAplStorageForm();
            const templateCreate = event.target.closest('[data-inline-create="templates"]');
            if (templateCreate) renderTemplateForm();
            const aplKeywordCreate = event.target.closest('[data-inline-create="apl-keywords"]');
            if (aplKeywordCreate) renderAplKeywordForm();
            const myAplAction = event.target.closest('[data-my-apl-action]');
            if (myAplAction) {
                const id = idOf(myAplAction.dataset.id);
                const actionName = myAplAction.dataset.myAplAction;
                if (actionName === 'detail' && id) showMyAplDetail(id).catch(notify);
                else if (actionName === 'use' && id) useAplForSimulation(id).catch(notify);
                else if (actionName === 'edit' && id) fetchAplStorageDetail(id).then(renderAplStorageForm).catch(notify);
                else if ((actionName === 'archive' || actionName === 'restore') && id) lifecycle('apl-storage', id, actionName).catch(notify);
            }
            const myAplDetailAction = event.target.closest('[data-my-apl-detail-action]');
            if (myAplDetailAction && myAplDetailAction.dataset.myAplDetailAction === 'close') closeDialog();
            const defaultAplAction = event.target.closest('[data-default-apl-action]');
            if (defaultAplAction) {
                const id = idOf(defaultAplAction.dataset.id);
                const actionName = defaultAplAction.dataset.defaultAplAction;
                if (actionName === 'view' && id) showDefaultAplDetail(id).catch(notify);
                else if (actionName === 'copy' && id) copyDefaultAplToMy(id, defaultAplAction).catch(notify);
            }
            const defaultAplDetailAction = event.target.closest('[data-default-apl-detail-action]');
            if (defaultAplDetailAction && defaultAplDetailAction.dataset.defaultAplDetailAction === 'close') closeDialog();
            const converterAction = event.target.closest('[data-converter-action]');
            if (converterAction) {
                const actionName = converterAction.dataset.converterAction;
                if (actionName === 'switch') {
                    state.converterRequestSerial += 1;
                    const select = document.getElementById('simc-converter-mode');
                    if (select) {
                        select.value = select.value === 'apl_to_cn' ? 'cn_to_apl' : 'apl_to_cn';
                        state.converterMode = select.value;
                    }
                } else if (actionName === 'execute') {
                    const input = document.getElementById('simc-converter-input');
                    const output = document.getElementById('simc-converter-output');
                    const status = document.getElementById('simc-converter-status');
                    const mode = document.getElementById('simc-converter-mode')?.value || state.converterMode;
                    if (input && output && status) {
                        const requestSerial = ++state.converterRequestSerial;
                        status.textContent = '转换中…';
                        window.convertText(input.value, mode).then(result => {
                            if (requestSerial !== state.converterRequestSerial) return;
                            output.value = result;
                            status.textContent = '转换完成';
                            updateConverterStats();
                        }).catch(error => {
                            if (requestSerial !== state.converterRequestSerial) return;
                            status.textContent = '转换失败';
                            notify(error);
                        });
                    }
                } else if (actionName === 'copy-output') {
                    const output = document.getElementById('simc-converter-output');
                    if (output && output.value) {
                        navigator.clipboard.writeText(output.value).then(() => {
                            window.showMessage('已复制到剪贴板', 'success');
                        }).catch(notify);
                    }
                } else if (actionName === 'clear') {
                    state.converterRequestSerial += 1;
                    const input = document.getElementById('simc-converter-input');
                    const output = document.getElementById('simc-converter-output');
                    const status = document.getElementById('simc-converter-status');
                    if (input) input.value = '';
                    if (output) output.value = '';
                    if (status) status.textContent = '准备就绪';
                    updateConverterStats();
                }
            }
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
                if (actionName === 'close-detail') closeDialog();
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
            const artifactPage = event.target.closest('[data-artifact-page]');
            if (artifactPage) loadArtifacts(state.artifactPage + (artifactPage.dataset.artifactPage === 'prev' ? -1 : 1));
            const previewAction = event.target.closest('[data-artifact-preview-action]');
            if (previewAction) {
                const dialogHost = previewAction.closest('#simc-dialog-body');
                const host = dialogHost || document.getElementById('simc-wb-artifact-detail');
                if (!host) return;
                if (previewAction.dataset.artifactPreviewAction === 'close') {
                    if (dialogHost && restoreDialogState()) return;
                    if (dialogHost) closeDialog();
                    else { host.classList.add('hidden'); host.replaceChildren(); }
                }
                if (previewAction.dataset.artifactPreviewAction === 'retry') previewArtifact({ dataset: { artifactPreview: host.dataset.previewUrl?.match(/artifacts\/(\d+)/)?.[1], previewUrl: host.dataset.previewUrl, title: host.dataset.previewTitle, meta: '' } });
            }
            if (event.target.closest('[data-wb-close-detail]')) {
                if (!restoreDialogState()) closeDialog();
            }
            const type = event.target.closest('[data-template-type]');
            if (type) { state.templateType = type.dataset.templateType || ''; loadTemplates().catch(notify); }
            const action = event.target.closest('[data-wb-action]');
            if (action) {
                const id = idOf(action.dataset.id), resource = action.dataset.resource, name = action.dataset.wbAction;
                if (!id) return;
                if (name === 'detail') {
                    if (action.closest('#simc-dialog-body')) pushDialogState();
                    showTaskDetail(resource, id).catch(notify);
                }
                else if (name === 'compare' && resource === 'batches') showBatchComparison(id).catch(notify);
                else if (name === 'template-detail') showTemplateDetail(id).catch(notify);
                else if (name === 'keyword-detail') showAplKeywordDetail(id).catch(notify);
                else if (name === 'keyword-edit') {
                    const row = (state.rows['apl-keywords'] || []).find(item => idOf(item.id) === id);
                    if (row) renderAplKeywordForm(row);
                }
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
            const retry = event.target.closest('[data-wb-retry]');
            if (retry) {
                const target = retry.dataset.wbRetry;
                if (target === 'tasks') loadTasks(state.taskResource, state.taskPage);
                else if (target === 'artifacts') loadArtifacts(state.artifactPage);
                else if (target === 'templates') loadTemplates();
                else if (target === 'apl-keywords') loadApl(target, 'simc-wb-apl-keyword-list');
                else if (target === 'apl-storage') loadApl(target, 'simc-unified-apl-list');
            }
        });
        function updateConverterStats() {
            const input = document.getElementById('simc-converter-input');
            const output = document.getElementById('simc-converter-output');
            const inputStats = document.getElementById('simc-converter-input-stats');
            const outputStats = document.getElementById('simc-converter-output-stats');
            if (input && inputStats) {
                const chars = input.value.length;
                const lines = input.value ? input.value.split('\n').length : 0;
                inputStats.textContent = `${chars} 字符 · ${lines} 行`;
            }
            if (output && outputStats) {
                const chars = output.value.length;
                const lines = output.value ? output.value.split('\n').length : 0;
                outputStats.textContent = `${chars} 字符 · ${lines} 行`;
            }
        }
        document.addEventListener('input', event => {
            const keywordSearch = event.target.closest('#simc-wb-apl-keyword-search');
            if (keywordSearch) {
                state.aplKeywordQuery = keywordSearch.value || '';
                renderAplKeywordTable();
                return;
            }
            const aplSearch = event.target.closest('#simc-apl-search');
            if (aplSearch) {
                state.aplQuery = aplSearch.value || '';
                renderUnifiedAplList();
                return;
            }
            const converterInput = event.target.closest('#simc-converter-input, #simc-converter-output');
            if (converterInput) updateConverterStats();
        });
        document.addEventListener('change', event => {
            const artifactFilter = event.target.closest('[data-artifact-filter]');
            if (artifactFilter) {
                state.artifactTaskId = document.querySelector('[data-artifact-filter="task_id"]')?.value.trim() || '';
                state.artifactType = document.querySelector('[data-artifact-filter="artifact_type"]')?.value || '';
                loadArtifacts(1);
            }
            const autoUpdate = event.target.closest('[data-backend-auto-update]');
            if (autoUpdate && !autoUpdate.disabled) {
                runBackendAction({ action: 'set_auto_update', auto_update: autoUpdate.checked }).catch(notify);
            }
            const converterMode = event.target.closest('#simc-converter-mode');
            if (converterMode) state.converterMode = converterMode.value;
        });
        document.addEventListener('submit', event => {
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
    });
})();
