/* SimC 十模型内联工作台：专用 API、事件委托和安全结果预览。version: 20260716e */
(() => {
    'use strict';
    const apiRoot = '/api/simc-workbench/';
    const state = {
        activePanel: '', taskPage: 1, taskFetchInFlight: false,
        taskRequestSerial: 0, taskPollTimer: null, taskAbortController: null,
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
        specOptions: [],
        aplEditor: null, aplEditorGeneration: 0,
        aplImportGeneration: 0, aplImportAbortController: null,
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
    function setAplDialogLayout(active) {
        const body = document.getElementById('simc-dialog-body');
        const panel = document.getElementById('simc-workbench-dialog-content');
        const viewport = panel?.parentElement;
        [body, panel, viewport].forEach(node => node?.classList.toggle('is-apl-editor-layout', active));
    }
    function destroyAplEditor() {
        state.aplEditorGeneration += 1;
        state.aplImportGeneration += 1;
        state.aplImportAbortController?.abort();
        state.aplImportAbortController = null;
        if (state.aplEditor) {
            state.aplEditor.destroy();
            state.aplEditor = null;
        }
        setAplDialogLayout(false);
    }
    function closeDialog() {
        cancelDetailRequest();
        destroyAplEditor();
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
    const buttons = (resource, row) => {
        const id = idOf(row.id);
        if (!id) return '';
        const active = row.is_active !== false;
        return `<button data-wb-action="detail" data-resource="${esc(resource)}" data-id="${id}" class="text-blue-700">详情</button> <button data-wb-action="${active ? 'archive' : 'restore'}" data-resource="${esc(resource)}" data-id="${id}" class="text-amber-700">${active ? '停用' : '恢复'}</button>`;
    };
    async function loadTasks(page = 1) {
        state.taskPage = Number.isSafeInteger(Number(page)) && Number(page) > 0 ? Number(page) : 1;
        const requestedPage = state.taskPage;
        const requestSerial = ++state.taskRequestSerial;
        const host = document.getElementById('simc-wb-task-list');
        if (!host) return;
        renderState(host, 'loading', '正在加载任务…');
        if (state.taskAbortController) state.taskAbortController.abort();
        const controller = new AbortController();
        state.taskAbortController = controller;
        state.taskFetchInFlight = true;
        let data;
        try {
            data = await json(`${resourceUrl('history')}?page=${requestedPage}&page_size=20`, { signal: controller.signal });
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
        state.rows.history = data.data || [];

        host.innerHTML = data.data.length ? `<div class="simc-task-list">${data.data.map(row => {
            const status = Number(row.status);
            const isActive = [0, 1, 4].includes(status);
            const hasProgress = isActive && row.progress !== null && row.progress !== '' && Number.isFinite(Number(row.progress));
            const progress = hasProgress ? Math.max(0, Math.min(100, Number(row.progress))) : 0;
            const progressBar = hasProgress ? `<div class="simc-task-progress"><div class="simc-task-progress__track"><div class="simc-task-progress__fill" style="width:${progress}%"></div></div><span>${progress}%</span></div>` : '';
            const resource = row.detail_resource === 'batches' ? 'batches' : 'tasks';
            const statusClass = status === 2 ? 'is-success' : status === 3 ? 'is-failed' : status === 1 ? 'is-running' : 'is-pending';
            const statusIcon = status === 2 ? 'fa-check-circle' : status === 3 ? 'fa-exclamation-circle' : status === 1 ? 'fa-spinner fa-spin' : 'fa-clock';
            const typeLabel = resource === 'batches' ? '批次任务' : '普通模拟';
            const rerunButton = resource === 'tasks' && [2, 3].includes(status) ? `<button type="button" data-task-rerun="${idOf(row.id)}" class="simc-touch-action simc-task-secondary-action"><i class="fas fa-redo-alt" aria-hidden="true"></i><span>重跑</span></button>` : '';
            return `<article class="simc-task-card simc-responsive-row">
                <div class="simc-task-card__main">
                    <div class="simc-task-card__eyebrow"><span class="simc-task-type">${typeLabel}</span><span class="simc-task-id">#${idOf(row.id)}</span></div>
                    <h4 class="simc-task-card__title">${esc(row.name || `任务 #${idOf(row.id)}`)}</h4>
                    <div class="simc-task-card__meta"><span class="simc-task-status ${statusClass}"><i class="fas ${statusIcon}" aria-hidden="true"></i>${esc(row.status_label)}</span><time><i class="far fa-calendar-alt" aria-hidden="true"></i>${esc(row.created_at)}</time></div>
                    ${progressBar}
                </div>
                <div class="simc-task-card__actions"><a href="/dashboard/simc/${resource}/${idOf(row.id)}/" target="_blank" rel="noopener noreferrer" class="simc-touch-action simc-task-primary-action"><i class="fas fa-chart-line" aria-hidden="true"></i><span>查看结果</span></a>${rerunButton}</div>
            </article>`;
        }).join('')}</div>` : empty('暂无记录');

        renderPagination(data.pagination || {}, requestedPage);
        const hasActive = data.data.some(row => [0, 1, 4].includes(Number(row.status)));
        scheduleTaskRefresh(hasActive);
    }

    function scheduleTaskRefresh(hasActive) {
        if (state.taskPollTimer) {
            clearTimeout(state.taskPollTimer);
            state.taskPollTimer = null;
        }
        if (!hasActive || state.activePanel !== 'tasks') return;
        const page = state.taskPage;
        state.taskPollTimer = setTimeout(() => {
            state.taskPollTimer = null;
            if (state.activePanel !== 'tasks' || page !== state.taskPage) return;
            loadTasks(page).catch(() => scheduleTaskRefresh(true));
        }, 3000);
    }

    function renderPagination(pagination) {
        const paginationHost = document.getElementById('simc-wb-task-pagination');
        if (!paginationHost) return;

        const { total = 0, total_pages = 1, page = 1, page_size = 20 } = pagination;
        if (total === 0 || total_pages <= 1) {
            paginationHost.innerHTML = '';
            return;
        }

        const buttons = [];
        if (page > 1) {
            buttons.push(`<button data-pagination-page="${page - 1}" class="px-3 py-1 border rounded hover:bg-gray-100">上一页</button>`);
        }

        const start = Math.max(1, page - 2);
        const end = Math.min(total_pages, page + 2);
        for (let i = start; i <= end; i++) {
            const active = i === page ? 'bg-blue-600 text-white' : 'border hover:bg-gray-100';
            buttons.push(`<button data-pagination-page="${i}" class="px-3 py-1 rounded ${active}">${i}</button>`);
        }

        if (page < total_pages) {
            buttons.push(`<button data-pagination-page="${page + 1}" class="px-3 py-1 border rounded hover:bg-gray-100">下一页</button>`);
        }

        paginationHost.innerHTML = `<div class="flex items-center justify-between mt-3 text-sm"><div class="text-gray-600">共 ${total} 条记录，第 ${page}/${total_pages} 页</div><div class="flex gap-2">${buttons.join('')}</div></div>`;
    }
    function renderBatchRanking(rows) {
        const ranking = Array.isArray(rows) ? rows : [];
        if (!ranking.length) return '<p class="mt-2 text-sm text-gray-500">暂无可排名的 DPS 结果</p>';
        const best = Math.max(...ranking.map(row => Number(row.dps)).filter(Number.isFinite));
        const body = [...ranking].sort((a, b) => (Number(a.rank) || 9999) - (Number(b.rank) || 9999)).map(row => {
            const dps = Number(row.dps);
            const gap = Number.isFinite(dps) && Number.isFinite(best) ? best - dps : null;
            return `<tr class="border-t"><td class="p-2">${esc(row.rank || '-')}</td><td class="p-2">${esc(row.label || row.name || `任务 #${row.id}`)}</td><td class="p-2 text-right">${Number.isFinite(dps) ? Math.round(dps).toLocaleString() : '-'}</td><td class="p-2 text-right">${gap == null ? '-' : Math.round(gap).toLocaleString()}</td></tr>`;
        }).join('');
        return `<div class="mt-2 overflow-x-auto"><table class="w-full text-sm"><thead><tr class="text-left text-gray-500"><th class="p-2">排名</th><th class="p-2">候选任务</th><th class="p-2 text-right">DPS 排名</th><th class="p-2 text-right">距最佳</th></tr></thead><tbody>${body}</tbody></table></div>`;
    }

    function renderAttributeReport(report) {
        if (!report || typeof report !== 'object') return '<p class="mt-2 text-sm text-gray-500">本批次没有 attribute_report</p>';
        const candidates = Array.isArray(report.candidates) ? report.candidates : [];
        const candidateRows = candidates.map(candidate => `<tr class="border-t"><td class="p-2">${esc(candidate.round ?? '-')}</td><td class="p-2">${esc(candidate.label || `#${candidate.id || '-'}`)}</td><td class="p-2">${esc(JSON.stringify(candidate.ratings || {}))}</td><td class="p-2 text-right">${esc(candidate.dps ?? '-')}</td></tr>`).join('');
        const safePath = (Array.isArray(report.search_path) ? report.search_path : []).map(point => ({
            round: point?.round ?? null,
            ratings: point?.ratings && typeof point.ratings === 'object' ? point.ratings : {},
            dps: point?.dps ?? null,
        }));
        return `<div class="mt-2 rounded-lg border bg-amber-50 p-3 text-sm"><div>算法：${esc(report.algorithm || '-')} ${esc(report.algorithm_version || '')} · 步长 ${esc(report.step ?? '-')} · 容差 ${esc(report.tolerance ?? '-')}</div><div class="mt-1">轮次：${esc(report.rounds_completed ?? 0)} / ${esc(report.current_round ?? '-')} · 状态：${esc(report.stop_reason || '运行中')} · 局部最优：${report.local_optimum === true ? '是' : report.local_optimum === false ? '否' : '-'}</div><div class="mt-1">初始属性：<code>${esc(JSON.stringify(report.initial_ratings || {}))}</code> · 总评分：${esc(report.total_rating ?? '-')}</div><div class="mt-3 overflow-x-auto"><table class="w-full text-xs"><thead><tr class="text-left text-gray-500"><th class="p-2">轮次</th><th class="p-2">候选</th><th class="p-2">属性</th><th class="p-2 text-right">DPS</th></tr></thead><tbody>${candidateRows || '<tr><td colspan="4" class="p-2 text-gray-500">暂无候选摘要</td></tr>'}</tbody></table></div>${safePath.length ? `<details class="mt-2"><summary>搜索路径（安全摘要）</summary><pre class="mt-1 overflow-auto text-xs">${esc(JSON.stringify(safePath, null, 2))}</pre></details>` : ''}</div>`;
    }

    function safeRunErrorSummary(run) {
        if (!run || run.status !== 'failed') return '';
        // error_detail may contain commands, paths or stderr. The browser audit only
        // exposes the API's dedicated safe summary when present, otherwise a fixed label.
        const summary = typeof run.error_summary === 'string' ? run.error_summary.trim().slice(0, 200) : '';
        return summary || '执行失败（详细错误已隐藏）';
    }

    async function showTaskDetail(resource, id) {
        window.openSimcWorkbenchDialog(resource === 'batches' ? 'batch-detail' : 'task-detail', null);
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
        const artifacts = Array.isArray(row.artifacts) ? row.artifacts : [];
        const artifactList = artifacts.length ? artifacts.map(artifact => `<div class="mt-2 text-sm">${esc(artifact.file_name || artifact.artifact_type || '产物')}${artifact.can_preview === true ? ` <a href="${esc(artifact.preview_url)}" class="text-blue-700">打开报告</a>` : ''}</div>`).join('') : '<p class="mt-2 text-sm text-gray-500">暂无结果产物</p>';
        if (resource === 'batches') {
            const members = Array.isArray(row.tasks) ? row.tasks : [];
            const memberList = members.length ? members.map(member => `<article class="mt-2 flex flex-wrap items-center justify-between gap-2 rounded-lg border p-3"><div><b>${esc(member.name || `任务 #${member.id}`)}</b><div class="text-xs text-gray-500">${esc(member.status_label || member.status)} · ${esc(member.updated_at)}</div></div><a href="/dashboard/simc/tasks/${idOf(member.id)}/" target="_blank" rel="noopener noreferrer" class="text-blue-700">查看结果</a></article>`).join('') : empty('此批次暂无成员');
            const compareButton = row.report_url ? `<button data-wb-action="compare" data-resource="batches" data-id="${idOf(row.id || id)}" class="rounded-lg border px-3 py-2 text-purple-700">查看对比结果</button>` : '';
            host.innerHTML = `<div class="flex justify-between gap-3"><h4 class="font-bold">任务详情：${esc(row.name || `#${id}`)}</h4><div>${compareButton}<button class="ml-2" data-wb-close-detail>关闭</button></div></div>
                <section class="mt-4"><h5 class="font-semibold">批次进度</h5><div class="mt-2 h-2 rounded bg-gray-200"><div class="h-2 rounded bg-blue-600" style="width:${Number(row.percent || 0)}%"></div></div><p class="mt-1 text-xs text-gray-500">${Number(row.succeeded || 0)}/${Number(row.total || 0)} 成功 · ${Number(row.running || 0)} 运行 · ${Number(row.pending || 0)} 等待 · ${Number(row.failed || 0)} 失败</p></section>
                <section class="mt-4"><h5 class="font-semibold">DPS 排名 · 距最佳 · 候选任务</h5>${renderBatchRanking(row.ranking)}</section>
                <section class="mt-4"><h5 class="font-semibold">批次成员</h5>${memberList}</section>
                <section class="mt-4"><h5 class="font-semibold">结果产物</h5>${artifactList}</section>
                <section class="mt-4"><h5 class="font-semibold">attribute_report</h5>${renderAttributeReport(row.attribute_report)}</section>`;
            return;
        }
        const params = row.simulation_params || {};
        const editable = row.profile_id && row.template_id && row.apl_id && row.profile_version_id && row.template_version_id && row.apl_version_id;
        const rerunButton = editable ? `<button data-task-rerun="${idOf(row.id)}" class="rounded-lg border px-3 py-2 text-blue-700">编辑后重跑</button>` : '';
        const runs = Array.isArray(row.runs) ? row.runs : [];
        const runList = runs.length ? runs.map(run => {
            const errorSummary = safeRunErrorSummary(run);
            return `<article class="mt-2 rounded border p-3 text-xs"><div class="font-medium">Run #${esc(run.sequence)} · ${esc(run.status)} · DPS ${esc(run.result_summary?.dps ?? '-')}</div><dl class="mt-2 grid gap-1 md:grid-cols-2"><div>input_hash：<code class="break-all">${esc(run.input_hash || '-')}</code></div><div>开始：${esc(run.started_at || '-')}</div><div>完成：${esc(run.completed_at || '-')}</div>${errorSummary ? `<div class="text-red-700">错误摘要：${esc(errorSummary)}</div>` : ''}</dl></article>`;
        }).join('') : '<p class="mt-2 text-sm text-gray-500">暂无执行轮次</p>';
        const report = row.report_summary || null;
        const reportArtifact = artifacts.find(artifact => idOf(artifact.id) === idOf(row.report_artifact_id));
        const character = report?.character || {};
        const simulation = report?.simulation || {};
        const abilities = Array.isArray(report?.top_abilities) ? report.top_abilities : [];
        const abilityRows = abilities.length ? abilities.map(ability => `<tr class="border-t"><td class="p-2">${esc(ability.name)}</td><td class="p-2 text-right">${esc(ability.dps || '-')}</td><td class="p-2 text-right">${esc(ability.dps_percent || '-')}</td></tr>`).join('') : '<tr><td colspan="3" class="p-3 text-center text-gray-500">报告中未解析到技能明细</td></tr>';
        const nativeReportButton = reportArtifact?.can_preview === true ? `<a href="${esc(reportArtifact.preview_url)}" class="rounded border px-3 py-2 text-blue-700">查看原生报告</a>` : '';
        const analysisDocument = report ? `<section class="mt-4 rounded-lg border bg-white p-4"><div class="flex flex-wrap items-center justify-between gap-2"><div><h5 class="font-semibold">结果分析文档</h5><p class="text-xs text-gray-500">只读解析精确 Run 的原始 SimC Artifact；原始文件未做任何改动。</p></div>${nativeReportButton}</div><dl class="mt-3 grid gap-2 text-sm sm:grid-cols-2 lg:grid-cols-4"><div><span class="text-gray-500">角色</span><div class="font-medium">${esc(character.name || '-')}</div></div><div><span class="text-gray-500">职业 / 专精</span><div class="font-medium">${esc(character.class || '-')} / ${esc(character.spec || '-')}</div></div><div><span class="text-gray-500">种族 / 等级</span><div class="font-medium">${esc(character.race || '-')} / ${esc(character.level || '-')}</div></div><div><span class="text-gray-500">DPS</span><div class="font-medium">${esc(report.dps ?? '-')}</div></div><div><span class="text-gray-500">战斗模型</span><div>${esc(simulation.fight_style || '-')}</div></div><div><span class="text-gray-500">战斗时长</span><div>${esc(simulation.fight_length || '-')}</div></div><div><span class="text-gray-500">迭代次数</span><div>${esc(simulation.iterations || '-')}</div></div><div><span class="text-gray-500">时间戳</span><div>${esc(simulation.timestamp || '-')}</div></div></dl><div class="mt-4 overflow-x-auto"><h6 class="mb-2 text-sm font-semibold">主要技能</h6><table class="w-full min-w-[420px] text-sm"><thead><tr class="text-left text-gray-500"><th class="p-2">技能</th><th class="p-2 text-right">DPS</th><th class="p-2 text-right">占比</th></tr></thead><tbody>${abilityRows}</tbody></table></div></section>` : '<section class="mt-4 rounded-lg border p-4 text-sm text-gray-500">暂无可用的结果分析文档；原生 Artifact 仍可在结果产物中查看。</section>';
        host.innerHTML = `<div class="flex flex-wrap justify-between gap-2"><h4 class="font-bold">任务详情：${esc(row.name || `#${id}`)}</h4><div>${rerunButton}<button class="ml-2" data-wb-close-detail>关闭</button></div></div><dl class="mt-3 grid gap-2 text-sm md:grid-cols-3"><div>状态：${esc(row.status_label)}</div><div>DPS：${esc(row.result_summary?.dps ?? '-')}</div><div>更新时间：${esc(row.updated_at)}</div></dl>${analysisDocument}${editable ? `<div class="mt-4 rounded-lg border bg-gray-50 p-3 text-sm"><div>Profile #${esc(row.profile_id)} · v${esc(row.profile_version_id)}</div><div>模板 #${esc(row.template_id)} · v${esc(row.template_version_id)}</div><div>APL #${esc(row.apl_id)} · v${esc(row.apl_version_id)}</div><pre class="mt-2 overflow-auto text-xs">${esc(JSON.stringify(params, null, 2))}</pre></div>` : ''}<section class="mt-4"><h5 class="font-semibold">执行轮次</h5>${runList}</section><section class="mt-4"><h5 class="font-semibold">结果产物</h5>${artifactList}</section>`;
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
        const attributeReport = data.data?.attribute_report || null;
        const tableRows = rows.map(row => {
            const validDps = row.dps != null && Number.isFinite(Number(row.dps));
            const dps = validDps ? Math.round(Number(row.dps)).toLocaleString() : (row.status === 3 || row.is_valid === false ? '无效结果' : '无 DPS 数据');
            const deltaValue = row.delta ?? row.delta_dps;
            const delta = validDps && deltaValue != null && Number.isFinite(Number(deltaValue)) ? `${Number(deltaValue) >= 0 ? '+' : ''}${Math.round(Number(deltaValue)).toLocaleString()}` : '-';
            const percent = validDps && row.delta_percent != null ? `${Number(row.delta_percent) >= 0 ? '+' : ''}${row.delta_percent}%` : '-';
            return `<tr class="border-t"><td class="p-2">${esc(row.rank || '-')}</td><td class="p-2">${esc(row.label || row.name)}</td><td class="p-2 text-right">${esc(dps)}</td><td class="p-2 text-right">${esc(delta)}</td><td class="p-2 text-right">${esc(percent)}</td></tr>`;
        }).join('');
        host.innerHTML = `<div class="flex justify-between gap-3"><div><h4 class="font-bold">结果比较</h4><p class="text-xs text-gray-500">仅展示已解析的安全 JSON 摘要；不会把 HTML 报告地址当作 JSON 请求。</p></div><button data-wb-close-detail>关闭</button></div><div class="mt-3 overflow-x-auto"><table class="w-full min-w-[560px] text-sm"><thead><tr class="text-left text-gray-500"><th class="p-2">排名</th><th class="p-2">方案</th><th class="p-2 text-right">DPS</th><th class="p-2 text-right">差值</th><th class="p-2 text-right">差值%</th></tr></thead><tbody>${tableRows || `<tr><td colspan="5" class="p-5 text-center text-gray-500">无有效成员结果</td></tr>`}</tbody></table></div><section class="mt-4"><h5 class="font-semibold">attribute_report</h5>${renderAttributeReport(attributeReport)}</section>`;
    }
    async function resourceOptions(resource, selectedId) {
        const payload = await json(resourceUrl(resource));
        const originalId = idOf(selectedId);
        return (payload.data || []).filter(row => idOf(row.id) === originalId || (row.is_active !== false && row.is_selectable !== false)).map(row => `<option value="${idOf(row.id)}" ${idOf(row.id) === originalId ? 'selected' : ''}>${esc(row.name || row.title || `#${row.id}`)}</option>`).join('');
    }

    async function renderTaskRerunForm(taskId) {
        pushDialogState();
        window.openSimcWorkbenchDialog('task-rerun', null);
        const host = document.getElementById('simc-dialog-body');
        if (!host) return;
        renderState(host, 'loading', '正在加载重跑表单…');
        const task = (await json(resourceUrl('tasks', taskId))).data || {};
        const [profiles, templates, apls] = await Promise.all([
            resourceOptions('profiles', task.profile_id),
            resourceOptions('templates', task.template_id),
            resourceOptions('apls', task.apl_id),
        ]);
        const params = task.simulation_params || {};
        host.innerHTML = `<form data-task-rerun-form data-task-id="${idOf(taskId)}" class="space-y-4">
            <div><h4 class="font-bold">编辑后重跑</h4><p class="text-sm text-gray-500">提交 allowedPatch，创建新 Task，不修改旧 Task。</p></div>
            <label class="block text-sm">名称<input name="name" required value="${esc(task.name || '')} (rerun)" class="mt-1 w-full rounded border px-3 py-2"></label>
            <div class="grid gap-3 md:grid-cols-2"><label class="text-sm">iterations<input name="iterations" type="number" min="1" value="${esc(params.iterations || '')}" class="mt-1 w-full rounded border px-3 py-2"></label><label class="text-sm">fight_style<input name="fight_style" value="${esc(params.fight_style || 'Patchwerk')}" class="mt-1 w-full rounded border px-3 py-2"></label><label class="text-sm">max_time<input name="max_time" type="number" min="1" value="${esc(params.max_time || 300)}" class="mt-1 w-full rounded border px-3 py-2"></label><label class="text-sm">desired_targets<input name="desired_targets" type="number" min="1" value="${esc(params.desired_targets || 1)}" class="mt-1 w-full rounded border px-3 py-2"></label></div>
            <div class="grid gap-3 md:grid-cols-3"><label class="text-sm">Profile<select name="simc_profile_id" data-original-id="${idOf(task.profile_id)}" class="mt-1 w-full rounded border px-3 py-2">${profiles}</select><span class="block text-xs text-gray-500">未改选时复用 profile_version_id ${esc(task.profile_version_id)}</span></label><label class="text-sm">基础模板<select name="base_template_id" data-original-id="${idOf(task.template_id)}" class="mt-1 w-full rounded border px-3 py-2">${templates}</select><span class="block text-xs text-gray-500">未改选时复用 template_version_id ${esc(task.template_version_id)}</span></label><label class="text-sm">APL<select name="selected_apl_id" data-original-id="${idOf(task.apl_id)}" class="mt-1 w-full rounded border px-3 py-2">${apls}</select><span class="block text-xs text-gray-500">未改选时复用 apl_version_id ${esc(task.apl_version_id)}</span></label></div>
            <div class="flex gap-2"><button type="submit" class="rounded bg-blue-600 px-4 py-2 text-white">创建新 Task</button><button type="button" data-task-rerun-cancel class="rounded border px-4 py-2">取消</button></div>
        </form>`;
    }

    async function submitTaskRerun(form) {
        const values = new FormData(form);
        const intOrNull = name => { const value = Number.parseInt(String(values.get(name) || ''), 10); return Number.isSafeInteger(value) && value > 0 ? value : null; };
        const allowedPatch = {
            name: String(values.get('name') || '').trim(),
            simulation_params: {
                iterations: intOrNull('iterations'), fight_style: String(values.get('fight_style') || '').trim(),
                max_time: intOrNull('max_time'), desired_targets: intOrNull('desired_targets'),
            },
        };
        const changedResource = inputName => {
            const select = form.elements.namedItem(inputName);
            const selected = intOrNull(inputName);
            const original = idOf(select?.dataset.originalId);
            return selected && selected !== original ? selected : null;
        };
        const profileOverride = changedResource('simc_profile_id');
        const templateOverride = changedResource('base_template_id');
        const aplOverride = changedResource('selected_apl_id');
        if (profileOverride) allowedPatch.profile_id = profileOverride;
        if (templateOverride) allowedPatch.template_id = templateOverride;
        if (aplOverride) allowedPatch.apl_id = aplOverride;
        Object.keys(allowedPatch.simulation_params).forEach(key => { if (allowedPatch.simulation_params[key] == null || allowedPatch.simulation_params[key] === '') delete allowedPatch.simulation_params[key]; });
        const result = await json(resourceUrl('tasks', idOf(form.dataset.taskId)), {
            method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': window.getCSRFToken() },
            body: JSON.stringify({ action: 'rerun', ...allowedPatch }),
        });
        window.showMessage('已创建新的引用型任务', 'success');
        state.dialogStack = [];
        window.location.assign(`/dashboard/simc/tasks/${idOf(result.data?.id)}/`);
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
        document.querySelectorAll('[data-template-type]').forEach(button => {
            button.setAttribute('aria-pressed', String((button.dataset.templateType || '') === state.templateType));
        });
        const summary = document.querySelector('[data-template-filter-summary]');
        if (summary) summary.textContent = state.templateType ? `共 ${data.data.length} 个模板 · 当前筛选 ${rows.length} 个` : `共 ${rows.length} 个模板`;
        host.innerHTML = rows.length ? `<div class="simc-template-table-wrap"><table class="simc-template-table">
            <thead><tr><th>模板名称</th><th>类型</th><th class="simc-template-class-col">职业</th><th class="simc-template-spec-col">专精</th><th>来源</th><th>状态</th><th class="simc-template-actions-col">操作</th></tr></thead>
            <tbody>${rows.map(row => {
                const active = row.is_active !== false;
                const readOnly = row.read_only === true;
                const ownership = !readOnly ? '我的模板' : (row.source === 'simc_upstream' ? '上游同步' : '系统内置');
                const sourceClass = readOnly ? 'bg-slate-100 text-slate-600' : 'bg-blue-50 text-blue-700';
                const statusClass = active ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-100 text-slate-500';
                return `<tr>
                    <td data-label="模板名称"><div class="simc-template-name">${esc(row.name)}</div></td>
                    <td data-label="类型"><span class="simc-template-badge bg-indigo-50 text-indigo-700">${esc(row.type_label)}</span></td>
                    <td data-label="职业" class="simc-template-class-col"><span class="simc-template-scope-value">${esc(row.class_name || '通用职业')}</span></td>
                    <td data-label="专精" class="simc-template-spec-col"><span class="simc-template-scope-value">${esc(row.spec || 'default')}</span></td>
                    <td data-label="来源"><span class="simc-template-badge ${sourceClass}">${esc(ownership)}</span></td>
                    <td data-label="状态"><span class="simc-template-badge ${statusClass}">${active ? '启用' : '已停用'}</span></td>
                    <td data-label="操作" class="simc-template-actions-col"><div class="simc-template-row-actions">${!readOnly ? `<button data-wb-action="template-edit" data-resource="templates" data-id="${idOf(row.id)}" class="simc-touch-action text-blue-700 hover:bg-blue-50"><i class="fas fa-pen mr-1"></i>编辑</button><button data-wb-action="${active ? 'archive' : 'restore'}" data-resource="templates" data-id="${idOf(row.id)}" class="simc-touch-action ${active ? 'text-amber-700 hover:bg-amber-50' : 'text-emerald-700 hover:bg-emerald-50'}"><i class="fas fa-${active ? 'pause' : 'play'} mr-1"></i>${active ? '停用' : '恢复'}</button>` : ''}<button data-wb-action="template-detail" data-resource="templates" data-id="${idOf(row.id)}" class="simc-touch-action text-slate-700 hover:bg-slate-100"><i class="fas fa-code mr-1"></i>查看</button></div></td>
                </tr>`;
            }).join('')}</tbody>
        </table></div>` : empty('此类型暂无模板');
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
        const content = row?.content || '';
        host.innerHTML = `<form data-template-form class="simc-editor-form space-y-4">
            <input type="hidden" name="id" value="${idOf(row?.id)}">
            <div class="rounded-2xl bg-gradient-to-br from-slate-900 to-indigo-950 p-4 text-white"><h4 class="text-lg font-bold">${row?.id ? '编辑内容模板' : '新建内容模板'}</h4><p class="mt-1 text-xs leading-5 text-indigo-100">模板按引用和不可变版本参与新任务；修改不会回写历史任务。</p></div>
            <section class="simc-editor-section">
                <div class="simc-editor-section__heading"><div><h5 class="text-sm font-bold text-slate-900">身份与适用范围</h5><p class="mt-1 text-xs text-slate-500">明确模板名称、类型和职业专精边界。</p></div><span class="rounded-full bg-blue-50 px-2.5 py-1 text-xs font-medium text-blue-700">ContentTemplate</span></div>
                <div class="grid gap-4 p-4 sm:grid-cols-2">
                    <label class="simc-editor-label sm:col-span-2">名称<input name="name" required maxlength="200" value="${esc(row?.name)}" class="simc-editor-input" placeholder="例如：通用基础模拟模板"></label>
                    <label class="simc-editor-label">类型<select name="template_type" required class="simc-editor-input">${typeOptions}</select><span class="simc-editor-help">默认玩家配置由上游同步维护，不在此手工创建。</span></label>
                    <label class="simc-editor-label">专精标识<input name="spec" maxlength="100" value="${esc(row?.spec || 'default')}" class="simc-editor-input" placeholder="default 或 warrior_fury"></label>
                    <label class="simc-editor-label sm:col-span-2">职业<input name="class_name" maxlength="50" value="${esc(row?.class_name)}" class="simc-editor-input" placeholder="留空表示通用"></label>
                </div>
            </section>
            <section class="simc-editor-section">
                <div class="simc-editor-section__heading"><div><h5 class="text-sm font-bold text-slate-900">模板内容</h5><p class="mt-1 text-xs text-slate-500">支持 Tab 缩进；按原始换行保存，不在浏览器端重排。</p></div><span class="rounded-full bg-indigo-50 px-2.5 py-1 text-xs font-medium text-indigo-700">SimC</span></div>
                <textarea name="content" required spellcheck="false" autocomplete="off" autocapitalize="off" class="simc-code-editor" placeholder="# SimulationCraft template&#10;iterations=10000"></textarea>
                <div class="simc-code-editor-toolbar"><span data-code-editor-stats>${codeStats(content)}</span><span>Tab 缩进 · 等宽字体 · 不自动换行</span></div>
            </section>
            <div class="simc-editor-actions"><span class="mr-auto hidden text-xs text-gray-500 sm:block">保存后仅影响引用新版本的新任务。</span><button type="button" data-template-action="cancel" class="rounded-lg border bg-white px-4 py-2 text-sm text-slate-700">取消</button><button type="submit" class="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"><i class="fas fa-save mr-1"></i>保存内容模板</button></div>
        </form>`;
        const editor = host.querySelector('textarea[name="content"]');
        if (editor) editor.value = content;
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
        const active = row.is_active !== false;
        const sourceLabel = row.source === 'simc_upstream' ? 'SimC 上游' : (row.read_only ? '系统内置' : '用户维护');
        host.innerHTML = `<div class="space-y-4">
            <div class="rounded-2xl bg-gradient-to-br from-slate-900 to-indigo-950 p-4 text-white"><div class="flex flex-wrap items-start justify-between gap-3"><div><p class="text-xs font-bold uppercase tracking-wider text-indigo-200">ContentTemplate</p><h4 class="mt-1 text-lg font-bold">${esc(row.name)}</h4></div><span class="simc-template-badge ${active ? 'bg-emerald-100 text-emerald-800' : 'bg-slate-200 text-slate-700'}">${active ? '启用' : '已停用'}</span></div></div>
            <dl class="simc-template-detail-meta text-sm"><div><dt class="text-xs font-medium text-slate-500">类型</dt><dd class="mt-1 font-semibold text-slate-900">${esc(row.type_label)}</dd></div><div><dt class="text-xs font-medium text-slate-500">来源</dt><dd class="mt-1 font-semibold text-slate-900">${esc(sourceLabel)}</dd></div><div><dt class="text-xs font-medium text-slate-500">专精</dt><dd class="mt-1 font-semibold text-slate-900">${esc(row.spec || 'default')}</dd></div><div><dt class="text-xs font-medium text-slate-500">职业</dt><dd class="mt-1 font-semibold text-slate-900">${esc(row.class_name || '通用')}</dd></div></dl>
            <section class="simc-editor-section"><div class="simc-editor-section__heading"><div><h5 class="text-sm font-bold text-slate-900">模板内容</h5><p class="mt-1 text-xs text-slate-500">只读预览，保持原始换行与缩进。</p></div><span class="text-xs text-slate-500">${codeStats(row.content)}</span></div><pre class="template-code-preview">${esc(row.content)}</pre></section>
        </div>`;
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
    function codeStats(value) {
        const text = String(value || '');
        return `${text ? text.split('\n').length : 0} 行 · ${text.length} 字符`;
    }
    function renderAplStorageForm(row = null) {
        destroyAplEditor();
        window.openSimcWorkbenchDialog('apl-form', null);
        const host = document.getElementById('simc-dialog-body');
        if (!host) return;
        const specOptions = state.specOptions.length ? state.specOptions : [{ value: row?.spec || '', label: row?.spec || '请选择专精' }];
        const specHtml = specOptions.map(option => `<option value="${esc(option.value)}" ${option.value === row?.spec ? 'selected' : ''}>${esc(option.label)}</option>`).join('');
        const content = row?.apl_code || '';
        host.innerHTML = `<form data-apl-storage-form data-managed-apl="${row?.id ? '1' : '0'}" data-system-apl="${row?.is_system ? '1' : '0'}" class="simc-editor-form space-y-4">
            <input type="hidden" name="id" value="${idOf(row?.id)}">
            <div class="rounded-2xl bg-gradient-to-br from-slate-900 to-indigo-950 p-4 text-white"><h4 class="text-lg font-bold">${row?.id ? '编辑 APL' : '新建 APL'}</h4><p class="mt-1 text-xs leading-5 text-indigo-100">维护名称、适用专精和完整 action priority list。历史 Task 继续绑定原资源版本。</p></div>
            <section class="simc-editor-section">
                <div class="grid gap-4 p-4 sm:grid-cols-2">
                    <label class="simc-editor-label">名称<input name="title" required maxlength="200" value="${esc(row?.title)}" class="simc-editor-input" placeholder="例如：Fury 单目标"></label>
                    <label class="simc-editor-label">适用专精<select name="spec" required class="simc-editor-input">${specHtml}</select><span class="simc-editor-help">统一使用“职业_专精”标识，便于任务引用与筛选。</span></label>
                </div>
            </section>
            <section class="simc-editor-section">
                <div class="simc-editor-section__heading"><div><h5 class="text-sm font-bold text-slate-900">APL 内容</h5><p class="mt-1 text-xs text-slate-500">APL/中文共用一个可编辑正文；保存时会自动转换为权威 APL。</p></div><div class="simc-apl-editor-heading-actions"><div class="simc-apl-language-switch" role="group" aria-label="正文语言"><button type="button" data-apl-language="apl" aria-pressed="true">APL</button><button type="button" data-apl-language="cn" aria-pressed="false">中文</button></div><button type="button" data-apl-validate-now>立即结构检查</button></div></div>
                ${row?.id ? '' : '<div class="simc-apl-default-library"><div class="simc-apl-default-library__heading"><div><strong>默认 APL 列表</strong><span data-apl-default-summary>请选择专精</span></div><input type="search" data-apl-default-search placeholder="筛选默认 APL"></div><div class="simc-apl-default-list" data-apl-default-list></div></div>'}
                <input type="hidden" name="apl_code" value="">
                <div class="simc-apl-workspace">
                    <div class="simc-apl-editor-column"><div class="simc-apl-editor-shell"><div class="simc-apl-editor-mount" data-apl-editor-mount></div><div class="simc-apl-diagnostics" data-apl-editor-diagnostics aria-live="polite"></div></div></div>
                    <aside class="simc-apl-assistant" data-apl-assistant aria-label="技能与 Buff 助手"><div data-apl-assistant-host></div></aside>
                </div>
                <button type="button" class="simc-apl-assistant-toggle" data-apl-assistant-toggle>技能与 Buff 助手</button>
                <div class="simc-code-editor-toolbar"><span data-code-editor-stats>${codeStats(content)}</span><span data-apl-editor-status>准备检查</span></div>
            </section>
            <div class="simc-editor-actions"><span class="mr-auto hidden text-xs text-gray-500 sm:block">保存后，新任务将引用新的不可变版本。</span><button type="button" data-apl-action="cancel" class="rounded-lg border bg-white px-4 py-2 text-sm text-slate-700">取消</button><button type="submit" class="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"><i class="fas fa-save mr-1"></i>保存 APL</button></div>
        </form>`;
        setAplDialogLayout(true);
        const hiddenInput = host.querySelector('input[name="apl_code"]');
        const mount = host.querySelector('[data-apl-editor-mount]');
        const status = host.querySelector('[data-apl-editor-status]');
        const diagnosticsHost = host.querySelector('[data-apl-editor-diagnostics]');
        const assistantHost = host.querySelector('[data-apl-assistant-host]');
        const assistantPanel = host.querySelector('[data-apl-assistant]');
        const languageButtons = Array.from(host.querySelectorAll('[data-apl-language]'));
        const defaultSearch = host.querySelector('[data-apl-default-search]');
        const defaultList = host.querySelector('[data-apl-default-list]');
        const defaultSummary = host.querySelector('[data-apl-default-summary]');
        const validateButton = host.querySelector('[data-apl-validate-now]');
        let defaultAplRows = [];
        const renderDefaultChoices = () => {
            if (!defaultList) return;
            const query = String(defaultSearch?.value || '').trim().toLocaleLowerCase();
            const visible = query ? defaultAplRows.filter(item => [item.name, item.title, item.class_name, item.spec]
                .some(value => String(value || '').toLocaleLowerCase().includes(query))) : defaultAplRows;
            if (defaultSummary) defaultSummary.textContent = `共 ${defaultAplRows.length} 个${query ? ` · 匹配 ${visible.length} 个` : ''}`;
            defaultList.innerHTML = visible.length ? visible.map(item => `<button type="button" data-apl-default-choice="${idOf(item.id)}"><span><strong>${esc(item.name || item.title || '默认 APL')}</strong><small>${esc(item.class_name || '')}${item.class_name && item.spec ? ' · ' : ''}${esc(item.spec || '')}</small></span><em>选择并导入</em></button>`).join('') : '<p>当前专精没有匹配的默认 APL</p>';
        };
        const loadDefaultChoices = async () => {
            if (!defaultList) return;
            const spec = host.querySelector('select[name="spec"]')?.value || '';
            const {selectDefaultAplsForSpec} = await import(window.SIMC_APL_EDITOR_MODULE_URL);
            defaultAplRows = selectDefaultAplsForSpec(state.rows.apls || [], spec);
            renderDefaultChoices();
        };
        defaultSearch?.addEventListener('input', renderDefaultChoices);
        defaultList?.addEventListener('click', async event => {
            const choice = event.target.closest('[data-apl-default-choice]');
            if (!choice || !state.aplEditor) return;
            state.aplImportGeneration += 1;
            state.aplImportAbortController?.abort();
            const importGeneration = state.aplImportGeneration;
            const controller = new AbortController();
            state.aplImportAbortController = controller;
            const editor = state.aplEditor;
            const specSelect = host.querySelector('select[name="spec"]');
            const originalSpec = specSelect?.value || '';
            const isCurrentImport = () => importGeneration === state.aplImportGeneration
                && state.aplImportAbortController === controller
                && state.aplEditor === editor
                && host.isConnected
                && document.getElementById('simc-dialog-body') === host
                && (specSelect?.value || '') === originalSpec;
            defaultList.querySelectorAll('button').forEach(button => { button.disabled = true; });
            try {
                const detail = (await json(resourceUrl('apls', idOf(choice.dataset.aplDefaultChoice)), {signal: controller.signal})).data || {};
                if (!isCurrentImport()) return;
                editor.setValue(detail.content || '', 'apl');
                const titleInput = host.querySelector('input[name="title"]');
                if (titleInput && !titleInput.value.trim()) titleInput.value = `${detail.name || '默认 APL'}（副本）`;
                window.showMessage('已导入所选默认 APL，可继续编辑', 'success');
            } catch (error) {
                if (error.name !== 'AbortError' && isCurrentImport()) notify(error);
            } finally {
                if (state.aplImportAbortController === controller) state.aplImportAbortController = null;
                if (defaultList.isConnected) defaultList.querySelectorAll('button').forEach(button => { button.disabled = false; });
            }
        });
        host.querySelector('select[name="spec"]')?.addEventListener('change', loadDefaultChoices);
        loadDefaultChoices().catch(notify);
        const updateLanguageButtons = language => {
            languageButtons.forEach(button => button.setAttribute('aria-pressed', button.dataset.aplLanguage === language ? 'true' : 'false'));
            if (validateButton) validateButton.disabled = language !== 'apl';
        };
        host.querySelector('[data-apl-assistant-toggle]')?.addEventListener('click', () => assistantPanel?.classList.toggle('is-open'));
        languageButtons.forEach(button => button.addEventListener('click', async () => {
            if (!state.aplEditor || button.getAttribute('aria-pressed') === 'true') return;
            languageButtons.forEach(item => { item.disabled = true; });
            if (status) status.textContent = '正在转换正文…';
            try {
                await state.aplEditor.convertLanguage(button.dataset.aplLanguage);
                if (status) status.textContent = button.dataset.aplLanguage === 'cn' ? '中文编辑模式' : 'APL 编辑模式';
            } catch (error) {
                if (error.name !== 'AbortError') notify(error);
            } finally {
                languageButtons.forEach(item => { item.disabled = false; });
            }
        }));
        validateButton?.addEventListener('click', async () => {
            validateButton.disabled = true;
            try { await state.aplEditor?.validateNow(); }
            finally { validateButton.disabled = false; }
        });
        if (hiddenInput) hiddenInput.value = content;
        const editorGeneration = state.aplEditorGeneration;
        import(window.SIMC_APL_EDITOR_MODULE_URL).then(({createSimcAplEditor}) => {
            if (editorGeneration !== state.aplEditorGeneration || !mount?.isConnected || state.aplEditor) return;
            state.aplEditor = createSimcAplEditor({
                mount, status, diagnosticsHost, assistantHost, value: content,
                csrfToken: window.getCSRFToken(),
                getSpec: () => host.querySelector('select[name="spec"]')?.value || '',
                closeAssistant: () => assistantPanel?.classList.remove('is-open'),
                onLanguageChange: updateLanguageButtons,
                onChange: value => {
                    if (hiddenInput) hiddenInput.value = value;
                    const stats = host.querySelector('[data-code-editor-stats]');
                    if (stats) stats.textContent = codeStats(value);
                },
            });
        }).catch(notify);
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
        const personalRows = (state.rows.apls || []).filter(row => !row.is_system).map(row => ({ ...row, title: row.name, apl_code: '', kind: 'personal', managedViaResource: true }));
        const defaultRows = (state.rows.apls || []).filter(row => row.is_system).map(row => ({ ...row, kind: 'default', managedViaResource: true }));
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
        const summary = document.querySelector('[data-apl-list-summary]');
        if (summary) summary.textContent = query ? `共 ${allRows.length} 个 APL · 筛选后 ${filteredRows.length} 个` : `共 ${allRows.length} 个 APL`;
        const rowsHtml = filteredRows.map(row => {
            const isPersonal = row.kind === 'personal';
            const active = row.is_active !== false;
            const name = row.title || row.name || `APL #${idOf(row.id)}`;
            const sourceLabel = isPersonal ? '个人' : (row.source === 'simc_upstream' ? 'SimC 上游' : '系统默认');
            const sourceClass = isPersonal ? 'bg-blue-50 text-blue-700' : 'bg-slate-100 text-slate-600';
            const statusClass = active ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-100 text-slate-500';
            let actions;
            if (isPersonal) {
                actions = `<button data-apl-action="detail" data-id="${idOf(row.id)}" class="simc-touch-action text-slate-700 hover:bg-slate-100">查看</button>${row.read_only ? '<span class="px-2 text-xs text-slate-400">只读</span>' : `<button data-apl-action="edit" data-id="${idOf(row.id)}" class="simc-touch-action text-blue-700 hover:bg-blue-50">编辑</button><button data-apl-action="delete" data-id="${idOf(row.id)}" class="simc-touch-action text-red-700 hover:bg-red-50">删除</button>`}`;
            } else {
                const writableActions = `<button data-apl-action="edit" data-id="${idOf(row.id)}" class="simc-touch-action text-blue-700 hover:bg-blue-50">编辑</button><button data-apl-action="delete" data-id="${idOf(row.id)}" class="simc-touch-action text-red-700 hover:bg-red-50">删除</button>`;
                const copyAction = row.can_copy === true ? `<button data-default-apl-action="copy" data-id="${idOf(row.id)}" class="simc-touch-action text-blue-700 hover:bg-blue-50">复制</button>` : '';
                actions = `<button data-default-apl-action="view" data-id="${idOf(row.id)}" class="simc-touch-action text-slate-700 hover:bg-slate-100">查看</button>${copyAction}${row.read_only ? '' : writableActions}`;
            }
            return `<tr>
                <td data-label="APL 名称"><div class="simc-apl-name">${esc(name)}</div></td>
                <td data-label="职业" class="simc-apl-class-col"><span class="simc-apl-scope-value">${esc(row.class_name || '通用职业')}</span></td>
                <td data-label="专精" class="simc-apl-spec-col"><span class="simc-apl-scope-value">${esc(row.spec || '未标记')}</span></td>
                <td data-label="来源"><span class="simc-template-badge ${sourceClass}">${esc(sourceLabel)}</span></td>
                <td data-label="状态"><span class="simc-template-badge ${statusClass}">${active ? '启用' : '已停用'}</span></td>
                <td data-label="操作" class="simc-apl-actions-col"><div class="simc-apl-row-actions">${actions}</div></td>
            </tr>`;
        }).join('');
        const table = rowsHtml ? `<div class="simc-apl-table-wrap"><table class="simc-apl-table"><thead><tr><th>APL 名称</th><th class="simc-apl-class-col">职业</th><th class="simc-apl-spec-col">专精</th><th>来源</th><th>状态</th><th class="simc-apl-actions-col">操作</th></tr></thead><tbody>${rowsHtml}</tbody></table></div>` : empty(query ? '无匹配结果' : '暂无数据');
        host.innerHTML = statusParts.join('') + table;
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
        const canWrite = resource === 'apl-storage' || resource === 'apls' || data.can_write === true;
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
    async function openNewAplStorageForm() {
        const pending = [];
        if (!state.specOptions.length) pending.push(loadSpecOptions());
        if (!Array.isArray(state.rows.apls)) {
            pending.push(loadApl('apls', 'simc-unified-apl-list'));
        }
        await Promise.all(pending);
        renderAplStorageForm();
    }
    async function fetchAplStorageDetail(id) {
        return fetchManagedAplDetail(id);
    }
    async function showMyAplDetail(id) {
        const host = openDialog('apl-detail');
        if (!host) return;
        const detailRequest = beginDetailRequest(`my-apl:${id}`);
        renderState(host, 'loading', '正在加载APL详情…');
        let data;
        try {
            data = await json(resourceUrl('apls', id), { signal: detailRequest.controller.signal });
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
            data = await json(resourceUrl('apls', id), { signal: detailRequest.controller.signal });
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
            await json(resourceUrl('apls'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': window.getCSRFToken() },
                body: JSON.stringify({ copy_template_id: templateId }),
            });
            await loadApl('apls', 'simc-unified-apl-list');
            window.showMessage('已复制到我的APL', 'success');
        } finally {
            state.defaultAplCopyInFlight.delete(templateId);
            if (button) button.disabled = false;
        }
    }
    async function saveAplStorage(form) {
        const {runSingleSubmission} = await import(window.SIMC_APL_EDITOR_MODULE_URL);
        await runSingleSubmission(form, async () => {
            const aplCodeInput = form.querySelector('input[name="apl_code"]');
            const editor = state.aplEditor;
            const saveSnapshot = editor ? await editor.getSaveSnapshot() : null;
            if (aplCodeInput && saveSnapshot) aplCodeInput.value = saveSnapshot.content;
            const formData = new FormData(form);
            const id = idOf(formData.get('id'));
            const payload = {
                title: String(formData.get('title') || '').trim(),
                spec: String(formData.get('spec') || '').trim(),
                apl_code: String(formData.get('apl_code') || ''),
            };
            if (id) payload.id = id;
            const validation = await json(`${apiRoot}apl-validation/`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': window.getCSRFToken() },
                body: JSON.stringify({
                    content: payload.apl_code, spec: payload.spec,
                    mode: 'structural', document_version: saveSnapshot?.version ?? 0,
                }),
            });
            if (validation.data?.diagnostics?.some(item => item.severity === 'error')) {
                throw new Error('APL 结构检查未通过，请修复错误后再保存');
            }
            if (saveSnapshot && (state.aplEditor !== editor || !editor.isCurrentSaveSnapshot(saveSnapshot))) {
                throw new Error('保存检查期间正文已变化，请确认最新内容后重试');
            }
            await json(resourceUrl('apls', id), {
                method: id ? 'PUT' : 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': window.getCSRFToken() },
                body: JSON.stringify({ name: payload.title, spec: payload.spec, content: payload.apl_code }),
            });
            closeAplStorageForm();
            await loadApl('apls', 'simc-unified-apl-list');
            window.showMessage(id ? 'APL 已更新' : 'APL 已新增', 'success');
        });
    }
    async function useAplForSimulation(id) {
        const row = await fetchAplStorageDetail(id);
        document.querySelector('.simc-l1-tab[data-simc-l1-tab="workflow"]')?.click();
        const radio = Array.from(document.querySelectorAll('input[name="simc-sim-apl"]'))
            .find(input => idOf(input.value) === idOf(id));
        if (!radio) throw new Error('当前 Profile 专精下没有该 APL 引用');
        radio.checked = true;
        window.showMessage(`已选择“${row.title}”作为 APL 引用`, 'success');
    }
    window.loadSimcWorkbenchApl = () => loadApl('apls', 'simc-unified-apl-list').catch(notify);
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
        if (resource === 'tasks' || resource === 'batches') await loadTasks(state.taskPage);
        else if (resource === 'apl-storage') await loadApl(resource, 'simc-unified-apl-list');
        else if (resource === 'apl-keywords') await loadApl(resource, 'simc-wb-apl-keyword-list');
        else if (resource === 'templates') await loadTemplates();
    }
    async function fetchManagedAplDetail(id, options = {}) {
        const row = (await json(resourceUrl('apls', id), options)).data || {};
        return { ...row, title: row.name, apl_code: row.content };
    }
    async function showManagedAplDetail(id) {
        const host = openDialog('apl-detail');
        if (!host) return;
        const request = beginDetailRequest(`managed-apl-detail:${id}`);
        renderState(host, 'loading', '正在加载 APL 详情…');
        let row;
        try {
            row = await fetchManagedAplDetail(id, { signal: request.controller.signal });
        } catch (error) {
            if (error.name === 'AbortError') return;
            throw error;
        }
        if (!isCurrentDetailRequest(request)) return;
        host.innerHTML = `<div class="flex flex-wrap justify-between gap-2 mb-3"><h4 class="font-bold">APL 详情</h4><button class="simc-touch-action" data-apl-action="cancel">关闭</button></div><dl class="grid gap-2 text-sm"><div>名称：${esc(row.name)}</div><div>专精：${esc(row.spec)}</div><div>来源：${esc(row.is_system ? '系统默认' : '个人')}</div></dl><div class="mt-3"><label class="text-sm font-medium text-gray-700">APL 内容</label><pre class="mt-1 rounded border bg-slate-50 p-3 text-xs overflow-auto max-h-96">${esc(row.content)}</pre></div>`;
    }
    async function editManagedApl(id) {
        const host = openDialog('apl-form');
        if (!host) return;
        const request = beginDetailRequest(`managed-apl-edit:${id}`);
        renderState(host, 'loading', '正在加载 APL 编辑器…');
        let row;
        try {
            row = await fetchManagedAplDetail(id, { signal: request.controller.signal });
        } catch (error) {
            if (error.name === 'AbortError') return;
            throw error;
        }
        if (!isCurrentDetailRequest(request)) return;
        renderAplStorageForm(row);
    }
    function confirmDeleteApl(id) {
        const host = openDialog('apl-delete');
        if (!host) return;
        host.innerHTML = `<div class="rounded-xl border border-red-200 bg-red-50 p-4"><h4 class="font-bold text-red-800">确认删除 APL？</h4><p class="mt-2 text-sm text-red-700">删除后无法恢复。</p><div class="mt-4 flex gap-2"><button data-apl-action="confirm-delete" data-id="${idOf(id)}" class="rounded-lg bg-red-600 px-4 py-2 text-white">确认删除</button><button data-apl-action="cancel" class="rounded-lg border bg-white px-4 py-2">取消</button></div></div>`;
    }
    async function deleteApl(id) {
        await json(resourceUrl('apls', id), { method: 'DELETE', headers: { 'X-CSRFToken': window.getCSRFToken() } });
        await loadApl('apls', 'simc-unified-apl-list');
        await loadDefaultAplLibrary();
        window.showMessage('APL 已删除', 'success');
    }
    async function loadSpecOptions() {
        const data = await json('/api/simc-spec-options/');
        state.specOptions = data.data || [];
    }
    function activate(tab) {
        state.activePanel = tab || '';
        if (tab === 'tasks') loadTasks().catch(notify);
        if (tab === 'templates') loadTemplates().catch(notify);
        if (tab === 'apl') {
            loadApl('apls', 'simc-unified-apl-list').catch(notify);
            loadSpecOptions().catch(notify);
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
    }
    window.simcWorkbenchLoadPanel = activate;
    window.simcWorkbenchDeactivatePanel = deactivate;
    window.simcWorkbenchShowTaskDetail = (resource, id) => showTaskDetail(resource, id).catch(notify);
    window.simcWorkbenchLoadTaskResource = (_resource, page = 1) => {
        state.activePanel = 'tasks';
        return loadTasks(page).catch(notify);
    };
    const notify = error => window.showMessage(String(error.message || error), 'error');
    document.addEventListener('simc-dialog-closing', event => {
        if (event.detail?.reason === 'close') state.dialogStack = [];
        cancelDetailRequest();
        destroyAplEditor();
    });
    document.addEventListener('simc-dialog-replace', () => {
        cancelDetailRequest();
        destroyAplEditor();
    });
    document.addEventListener('DOMContentLoaded', () => {
        const root = document.getElementById('simc-workbench');
        if (!root) return;
        document.addEventListener('click', async event => {
            const aplCreate = event.target.closest('[data-inline-create="apl-storage"]');
            if (aplCreate) openNewAplStorageForm().catch(notify);
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
                else if (actionName === 'detail' && id) showManagedAplDetail(id).catch(notify);
                else if (actionName === 'edit' && id) editManagedApl(id).catch(notify);
                else if (actionName === 'delete' && id) confirmDeleteApl(id);
                else if (actionName === 'confirm-delete' && id) deleteApl(id).then(closeDialog).catch(notify);
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
            const paginationBtn = event.target.closest('[data-pagination-page]');
            if (paginationBtn) {
                const page = parseInt(paginationBtn.dataset.paginationPage, 10);
                if (page > 0) loadTasks(page).catch(notify);
            }
            if (event.target.closest('[data-wb-close-detail]')) {
                if (!restoreDialogState()) closeDialog();
            }
            const type = event.target.closest('[data-template-type]');
            if (type) { state.templateType = type.dataset.templateType || ''; loadTemplates().catch(notify); }
            const rerunAction = event.target.closest('[data-task-rerun]');
            if (rerunAction) {
                renderTaskRerunForm(rerunAction.dataset.taskRerun);
                return;
            }
            if (event.target.closest('[data-task-rerun-cancel]')) {
                if (!restoreDialogState()) closeDialog();
                return;
            }
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
                if (target === 'tasks') loadTasks(state.taskPage);
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
            const codeEditor = event.target.closest('textarea.simc-code-editor');
            if (codeEditor) {
                const stats = codeEditor.closest('.simc-editor-section')?.querySelector('[data-code-editor-stats]');
                if (stats) stats.textContent = codeStats(codeEditor.value);
            }
            const converterInput = event.target.closest('#simc-converter-input, #simc-converter-output');
            if (converterInput) updateConverterStats();
        });
        document.addEventListener('keydown', event => {
            const editor = event.target.closest('textarea.simc-code-editor');
            if (!editor || event.key !== 'Tab') return;
            event.preventDefault();
            const start = editor.selectionStart;
            const end = editor.selectionEnd;
            editor.setRangeText('    ', start, end, 'end');
            editor.dispatchEvent(new Event('input', { bubbles: true }));
        });
        document.addEventListener('change', event => {
            const autoUpdate = event.target.closest('[data-backend-auto-update]');
            if (autoUpdate && !autoUpdate.disabled) {
                runBackendAction({ action: 'set_auto_update', auto_update: autoUpdate.checked }).catch(notify);
            }
            const converterMode = event.target.closest('#simc-converter-mode');
            if (converterMode) state.converterMode = converterMode.value;
            const aplSpec = event.target.closest('[data-apl-storage-form] select[name="spec"]');
            if (aplSpec && state.aplEditor) state.aplEditor.revalidate();
        });
        document.addEventListener('submit', event => {
            const taskRerunForm = event.target.closest('[data-task-rerun-form]');
            if (taskRerunForm) {
                event.preventDefault();
                submitTaskRerun(taskRerunForm).catch(notify);
                return;
            }
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
