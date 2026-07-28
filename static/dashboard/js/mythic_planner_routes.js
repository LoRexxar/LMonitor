(() => {
    'use strict';

    const $ = (selector, root = document) => root.querySelector(selector);
    const escapeHtml = (value) => String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');

    const state = {
        snapshot: null,
        routeDetail: null,
        toastTimer: null,
    };

    const els = {};

    function toast(message, isError = false) {
        window.clearTimeout(state.toastTimer);
        els.toast.textContent = message;
        els.toast.classList.toggle('is-error', isError);
        els.toast.hidden = false;
        state.toastTimer = window.setTimeout(() => { els.toast.hidden = true; }, 3500);
    }

    async function request(url, options = {}) {
        const response = await fetch(url, {
            credentials: 'same-origin',
            headers: {'Content-Type': 'application/json', ...(options.headers || {})},
            ...options,
        });
        let payload;
        try {
            payload = await response.json();
        } catch (_error) {
            throw new Error(`服务器返回了无法解析的响应（${response.status}）。`);
        }
        if (!response.ok || payload.success === false) {
            throw new Error(payload.message || `请求失败（${response.status}）。`);
        }
        return payload;
    }

    function formatDateTime(value) {
        if (!value) return '—';
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return escapeHtml(value);
        return new Intl.DateTimeFormat('zh-CN', {
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
            hour12: false,
        }).format(date);
    }

    function ownerLabel(route) {
        return route.owner_display_name
            || route.owner_username
            || `已删除账号 #${route.owner_user_id || '—'}`;
    }

    function routePublicLink(route) {
        if (!route?.share_id) return '';
        return `${location.origin}/portal/mythic-planner/?share=${encodeURIComponent(route.share_id)}`;
    }

    function allRoutes() {
        return state.snapshot?.routes || [];
    }

    function renderFilters() {
        const selectedOwner = els.ownerFilter.value;
        const selectedDungeon = els.dungeonFilter.value;
        const owners = [...new Map(allRoutes().map((route) => [
            String(route.owner_user_id ?? ''),
            route,
        ])).values()].sort((left, right) => (
            ownerLabel(left).localeCompare(ownerLabel(right), 'zh-CN')
        ));
        els.ownerFilter.innerHTML = '<option value="">全部账号</option>' + owners
            .map((route) => `<option value="${escapeHtml(route.owner_user_id ?? '')}">${escapeHtml(ownerLabel(route))}</option>`)
            .join('');
        if (owners.some((route) => String(route.owner_user_id ?? '') === selectedOwner)) {
            els.ownerFilter.value = selectedOwner;
        }

        const dungeonIds = new Set(allRoutes().map((route) => Number(route.dungeon_id)));
        const dungeons = (state.snapshot?.dungeons || []).filter((dungeon) => dungeonIds.has(Number(dungeon.id)));
        els.dungeonFilter.innerHTML = '<option value="">全部地下城</option>' + dungeons
            .map((dungeon) => `<option value="${dungeon.id}">${escapeHtml(dungeon.display_name || dungeon.name_zh || dungeon.name || dungeon.key)}</option>`)
            .join('');
        if (dungeons.some((dungeon) => String(dungeon.id) === selectedDungeon)) {
            els.dungeonFilter.value = selectedDungeon;
        }
    }

    function filteredRoutes() {
        const ownerId = els.ownerFilter.value;
        const dungeonId = els.dungeonFilter.value;
        const shareStatus = els.shareFilter.value;
        const search = els.search.value.trim().toLowerCase();
        const showInactive = els.showInactive.checked;
        return allRoutes().filter((route) => {
            if (!showInactive && route.is_active === false) return false;
            if (ownerId && String(route.owner_user_id ?? '') !== ownerId) return false;
            if (dungeonId && String(route.dungeon_id) !== dungeonId) return false;
            if (shareStatus && route.is_public !== (shareStatus === 'public')) return false;
            if (search) {
                const haystack = [
                    route.name,
                    route.owner_display_name,
                    route.owner_username,
                    route.owner_email,
                    route.dungeon_name,
                    route.share_id,
                    route.id,
                ].filter(Boolean).join(' ').toLowerCase();
                if (!haystack.includes(search)) return false;
            }
            return true;
        });
    }

    function renderSummary(rows) {
        const routes = allRoutes();
        const activeRoutes = routes.filter((route) => route.is_active !== false);
        const items = [
            ['当前显示', rows.length],
            ['全部路线', routes.length],
            ['正常路线', activeRoutes.length],
            ['公开路线', activeRoutes.filter((route) => route.is_public).length],
            ['账号数量', new Set(routes.filter((route) => route.owner_user_id != null).map((route) => route.owner_user_id)).size],
        ];
        els.summary.innerHTML = items
            .map(([label, value]) => `<span class="mp-admin-chip">${label}<strong>${value}</strong></span>`)
            .join('');
    }

    function renderActions(route) {
        return `
            <div class="mp-admin-row-actions mp-admin-route-actions">
                <button type="button" data-route-detail="${route.id}">详情</button>
                <button type="button" data-route-public="${route.id}">${route.is_public ? '设为私有' : '公开分享'}</button>
                <button type="button" data-route-active="${route.id}" class="${route.is_active ? 'is-danger' : 'is-success'}">${route.is_active ? '停用' : '恢复'}</button>
            </div>
        `;
    }

    function renderTable() {
        const routes = filteredRoutes();
        renderSummary(routes);
        if (!routes.length) {
            els.tableBody.innerHTML = '<tr><td colspan="9" class="mp-admin-empty">没有符合当前筛选条件的账号路线。</td></tr>';
            return;
        }
        els.tableBody.innerHTML = routes.map((route) => `
            <tr>
                <td><div class="mp-admin-name"><strong>${escapeHtml(route.name || '未命名路线')}</strong><span>#${route.id} · ${escapeHtml(String(route.share_id || '').slice(0, 8))}</span></div></td>
                <td><div class="mp-admin-name"><strong>${escapeHtml(ownerLabel(route))}</strong><span>${escapeHtml(route.owner_email || `用户 ID：${route.owner_user_id ?? '—'}`)}</span></div></td>
                <td>${escapeHtml(route.dungeon_name || '—')} · +${Number(route.dungeon_level || 0)}</td>
                <td>${Number(route.pull_count || 0)} 波 · ${Number(route.spawn_count || 0)} 个怪 · ${Number(route.annotation_count || 0)} 条标注</td>
                <td><span class="mp-admin-status ${route.is_public ? 'is-public' : ''}">${route.is_public ? '公开' : '私有'}</span></td>
                <td>${Number(route.revision || 1)}</td>
                <td>${formatDateTime(route.updated_at)}</td>
                <td><span class="mp-admin-status ${route.is_active ? 'is-active' : ''}">${route.is_active ? '正常' : '已停用'}</span></td>
                <td>${renderActions(route)}</td>
            </tr>
        `).join('');
    }

    function renderRouteDetail(route) {
        state.routeDetail = route;
        const shareLink = routePublicLink(route);
        els.detailTitle.textContent = route.name || '未命名路线';
        els.detailSubtitle.textContent = `路线 ID：${route.id} · 修订版本 ${route.revision}`;
        els.detailBody.innerHTML = `
            <section class="mp-route-detail-hero">
                <div>
                    <span class="mp-admin-status ${route.is_active ? 'is-active' : ''}">${route.is_active ? '正常' : '已停用'}</span>
                    <span class="mp-admin-status ${route.is_public ? 'is-public' : ''}">${route.is_public ? '公开分享' : '私有路线'}</span>
                </div>
                <strong>${escapeHtml(route.name || '未命名路线')}</strong>
                <p>${escapeHtml(route.dungeon_name || '—')} · ${Number(route.dungeon_level || 0)} 层</p>
            </section>
            <dl class="mp-route-detail-grid">
                <div><dt>所属账号</dt><dd>${escapeHtml(ownerLabel(route))}</dd></div>
                <div><dt>用户 ID</dt><dd>${escapeHtml(route.owner_user_id ?? '—')}</dd></div>
                <div><dt>账号邮箱</dt><dd>${escapeHtml(route.owner_email || '—')}</dd></div>
                <div><dt>数据版本</dt><dd>${escapeHtml(route.version_label || '—')}</dd></div>
                <div><dt>拉怪波次</dt><dd>${Number(route.pull_count || 0)} 波</dd></div>
                <div><dt>怪物选择</dt><dd>${Number(route.spawn_count || 0)} 个</dd></div>
                <div><dt>地图标注</dt><dd>${Number(route.annotation_count || 0)} 条</dd></div>
                <div><dt>更新时间</dt><dd>${formatDateTime(route.updated_at)}</dd></div>
            </dl>
            <section class="mp-route-detail-section">
                <header><strong>公开分享地址</strong><span>${route.is_public ? '当前链接可访问' : '路线为私有，链接不可访问'}</span></header>
                <div class="mp-route-copy-row">
                    <input type="text" readonly value="${escapeHtml(shareLink)}">
                    <button type="button" data-copy-route-link ${route.is_public ? '' : 'disabled'}>复制链接</button>
                </div>
            </section>
            <section class="mp-route-detail-section">
                <header><strong>MDT 导入分享字符串</strong><span>可复制到规划器导入或用于数据排查</span></header>
                <textarea readonly spellcheck="false">${escapeHtml(route.share_code || '')}</textarea>
                <button type="button" data-copy-route-code>复制 MDT 字符串</button>
            </section>
            <details class="mp-route-detail-json">
                <summary>查看原始路线数据（JSON）</summary>
                <pre>${escapeHtml(JSON.stringify(route.route_data || {}, null, 2))}</pre>
            </details>
        `;
        els.detailActions.innerHTML = `
            <button type="button" data-close-route-detail>关闭</button>
            <span class="mp-admin-spacer"></span>
            <button type="button" data-detail-route-public="${route.id}">${route.is_public ? '设为私有' : '开启公开分享'}</button>
            <button type="button" data-detail-route-active="${route.id}" class="${route.is_active ? 'is-danger' : 'is-success'}">${route.is_active ? '停用路线' : '恢复路线'}</button>
        `;
    }

    async function openRouteDetail(routeId) {
        els.detailModal.hidden = false;
        els.detailBody.innerHTML = '<div class="mp-route-detail-loading">正在加载路线内容和 MDT 字符串…</div>';
        try {
            const payload = await request(`/api/mythic-planner/manage/${routeId}/?resource=routes`);
            renderRouteDetail(payload.data);
        } catch (error) {
            els.detailBody.innerHTML = `<div class="mp-route-detail-loading is-error">${escapeHtml(error.message)}</div>`;
            toast(error.message, true);
        }
    }

    function closeRouteDetail() {
        els.detailModal.hidden = true;
        state.routeDetail = null;
    }

    async function copyText(value, successMessage) {
        try {
            if (navigator.clipboard?.writeText) {
                await navigator.clipboard.writeText(value);
            } else {
                const input = document.createElement('textarea');
                input.value = value;
                input.style.position = 'fixed';
                input.style.opacity = '0';
                document.body.appendChild(input);
                input.select();
                document.execCommand('copy');
                input.remove();
            }
            toast(successMessage);
        } catch (_error) {
            toast('复制失败，请在详情中手动复制。', true);
        }
    }

    async function updateRoute(routeId, data, successMessage, reopenDetail = false) {
        try {
            const payload = await request(`/api/mythic-planner/manage/${routeId}/`, {
                method: 'PATCH',
                body: JSON.stringify({resource: 'routes', data}),
            });
            state.snapshot = payload.snapshot;
            renderFilters();
            renderTable();
            if (reopenDetail) await openRouteDetail(routeId);
            toast(successMessage);
        } catch (error) {
            toast(error.message, true);
        }
    }

    async function toggleRoutePublic(routeId, reopenDetail = false) {
        const route = allRoutes().find((row) => Number(row.id) === Number(routeId));
        if (!route) return;
        await updateRoute(
            routeId,
            {is_public: !route.is_public},
            route.is_public ? '路线已设为私有。' : '路线已开启公开分享。',
            reopenDetail,
        );
    }

    async function toggleRouteActive(routeId, reopenDetail = false) {
        const route = allRoutes().find((row) => Number(row.id) === Number(routeId));
        if (!route) return;
        if (route.is_active) {
            if (!window.confirm(`确认停用路线“${route.name || '未命名路线'}”？账号将无法继续读取该记录。`)) return;
            try {
                const payload = await request(`/api/mythic-planner/manage/${routeId}/`, {
                    method: 'DELETE',
                    body: JSON.stringify({resource: 'routes'}),
                });
                state.snapshot = payload.snapshot;
                renderFilters();
                renderTable();
                if (reopenDetail) await openRouteDetail(routeId);
                toast('路线已停用。');
            } catch (error) {
                toast(error.message, true);
            }
            return;
        }
        await updateRoute(routeId, {is_active: true}, '路线已恢复。', reopenDetail);
    }

    async function loadRoutes({quiet = false} = {}) {
        if (!quiet) {
            els.tableBody.innerHTML = '<tr><td colspan="9" class="mp-admin-loading">正在刷新账号路线…</td></tr>';
        }
        try {
            const payload = await request('/api/mythic-planner/manage/');
            state.snapshot = payload.data;
            renderFilters();
            renderTable();
            if (!quiet) toast('账号路线已刷新。');
        } catch (error) {
            els.tableBody.innerHTML = `<tr><td colspan="9" class="mp-admin-empty">${escapeHtml(error.message)}</td></tr>`;
            toast(error.message, true);
        }
    }

    function bindElements() {
        Object.assign(els, {
            ownerFilter: $('#route-owner-filter'),
            dungeonFilter: $('#route-dungeon-filter'),
            shareFilter: $('#route-share-filter'),
            search: $('#route-search'),
            showInactive: $('#route-show-inactive'),
            summary: $('#route-summary'),
            tableBody: $('#route-table-body'),
            detailModal: $('#route-detail-modal'),
            detailTitle: $('#route-detail-title'),
            detailSubtitle: $('#route-detail-subtitle'),
            detailBody: $('#route-detail-body'),
            detailActions: $('#route-detail-actions'),
            toast: $('#admin-toast'),
        });
    }

    function bindEvents() {
        $('#refresh-routes').addEventListener('click', () => loadRoutes());
        els.ownerFilter.addEventListener('change', renderTable);
        els.dungeonFilter.addEventListener('change', renderTable);
        els.shareFilter.addEventListener('change', renderTable);
        els.search.addEventListener('input', renderTable);
        els.showInactive.addEventListener('change', renderTable);
        els.tableBody.addEventListener('click', (event) => {
            const detail = event.target.closest('[data-route-detail]');
            const routePublic = event.target.closest('[data-route-public]');
            const routeActive = event.target.closest('[data-route-active]');
            if (detail) openRouteDetail(Number(detail.dataset.routeDetail));
            if (routePublic) toggleRoutePublic(Number(routePublic.dataset.routePublic));
            if (routeActive) toggleRouteActive(Number(routeActive.dataset.routeActive));
        });
        els.detailModal.addEventListener('click', (event) => {
            if (event.target.closest('[data-close-route-detail]')) {
                closeRouteDetail();
                return;
            }
            const routePublic = event.target.closest('[data-detail-route-public]');
            const routeActive = event.target.closest('[data-detail-route-active]');
            if (routePublic) toggleRoutePublic(Number(routePublic.dataset.detailRoutePublic), true);
            if (routeActive) toggleRouteActive(Number(routeActive.dataset.detailRouteActive), true);
            if (event.target.closest('[data-copy-route-link]') && state.routeDetail?.is_public) {
                copyText(routePublicLink(state.routeDetail), '公开分享链接已复制。');
            }
            if (event.target.closest('[data-copy-route-code]') && state.routeDetail?.share_code) {
                copyText(state.routeDetail.share_code, 'MDT 字符串已复制。');
            }
        });
        window.addEventListener('keydown', (event) => {
            if (event.key === 'Escape' && !els.detailModal.hidden) closeRouteDetail();
        });
    }

    function init() {
        bindElements();
        bindEvents();
        loadRoutes({quiet: true});
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init, {once: true});
    } else {
        init();
    }
})();
