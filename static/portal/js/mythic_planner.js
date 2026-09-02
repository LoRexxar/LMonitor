(() => {
    'use strict';

    const $ = (selector, root = document) => root.querySelector(selector);
    const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
    const clone = (value) => JSON.parse(JSON.stringify(value));
    const nowIso = () => new Date().toISOString();
    const STORAGE_KEY = 'lmonitor.mythicPlanner.routes.v1';
    const SELECTED_KEY = 'lmonitor.mythicPlanner.selectedRoute.v1';
    const PULL_AREA_PADDING_PX = 1;
    const PULL_AREA_SELECTED_RING_PX = 2;
    const PULL_AREA_CIRCLE_SEGMENTS = 16;
    const PULL_AREA_CORNER_RADIUS_PX = 7;
    const PULL_COLORS = [
        '#e879f9', '#2dd4bf', '#f87171', '#60a5fa', '#facc15',
        '#4ade80', '#fb7185', '#a78bfa', '#38bdf8', '#f97316',
        '#84cc16', '#ec4899', '#14b8a6', '#818cf8', '#eab308',
    ];
    const TOOL_HINTS = {
        select: '选择工具：左键怪物增删拉怪，右键查看详情；地图放大后可按住空白处拖动',
        pan: '拖动工具：按住鼠标移动地图',
        box: '框选工具：拖出矩形，将范围内怪物加入当前拉怪组',
        pencil: '自由绘制：在地图上按住鼠标绘制路线',
        line: '直线工具：拖动绘制直线',
        arrow: '箭头工具：拖动绘制方向箭头',
        note: '文字工具：点击地图添加文字标注；双击已有文字可编辑',
        erase: '擦除工具：点击标注附近将其删除',
    };
    const TRAIT_LABELS = {
        taunt: '嘲讽',
        stun: '昏迷',
        interrupt: '打断',
        disorient: '迷惑',
        root: '定身',
        slow: '减速',
        silence: '沉默',
        soothe: '安抚',
        banish: '放逐',
        incapacitate: '瘫痪',
        knock: '击退',
        grip: '拉拽',
        mind_control: '精神控制',
        fear: '恐惧',
        sleep_walk: '催眠',
        polymorph: '变形',
        shackle_undead: '束缚亡灵',
        sap: '闷棍',
        turn_evil: '超度邪恶',
        repentance: '忏悔',
        paralyze: '分筋错骨',
    };
    const POI_ICONS = {
        entrance: '↪',
        exit: '↩',
        portal: '↕',
        boss: '☠',
        utility: '◆',
        note: 'i',
        dungeonEntrance: '↪',
        dungeonExit: '↩',
        genericItem: '✦',
        genericAssignablePOI: '☠',
        mapLink: '↕',
    };
    const POI_TYPE_LABELS = {
        dungeonEntrance: '地下城入口',
        dungeonExit: '地下城出口',
        genericItem: '可交互物品',
        genericAssignablePOI: '可分配交互目标',
        mapLink: '楼层通道',
    };
    const state = {
        catalog: null,
        selectionGroupKey: '',
        dungeon: null,
        floorKey: '',
        enemyByUid: new Map(),
        spawnByUid: new Map(),
        route: null,
        tool: 'select',
        zoom: 1,
        panX: 0,
        panY: 0,
        history: [],
        future: [],
        interaction: null,
        detailUid: '',
        modalAction: null,
        toastTimer: null,
        pullDrag: null,
        suppressPullClick: false,
        broadcast: null,
        live: false,
        origin: randomId(),
        isApplyingRemote: false,
        modalView: '',
        routeLibraryDungeonKey: '',
    };

    const els = {};

    function randomId() {
        if (window.crypto && typeof window.crypto.randomUUID === 'function') {
            return window.crypto.randomUUID();
        }
        return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
    }

    function escapeHtml(value) {
        return String(value ?? '')
            .replaceAll('&', '&amp;')
            .replaceAll('<', '&lt;')
            .replaceAll('>', '&gt;')
            .replaceAll('"', '&quot;')
            .replaceAll("'", '&#039;');
    }

    function clamp(value, min, max) {
        return Math.min(max, Math.max(min, value));
    }

    function formatNumber(value) {
        return Number(value || 0).toLocaleString('zh-CN');
    }

    function initials(name) {
        const value = String(name || '?').trim();
        const chinese = value.match(/[\u3400-\u9fff]/g);
        if (chinese && chinese.length) return chinese[0];
        return value.split(/\s+/).map((word) => word[0]).join('').slice(0, 2).toUpperCase() || '?';
    }

    function enemyDisplayId(enemy) {
        const displayId = Math.trunc(Number(enemy?.metadata?.display_id || 0));
        return Number.isSafeInteger(displayId) && displayId > 0 ? displayId : 0;
    }

    function catalogSelectionGroups() {
        const groups = Array.isArray(state.catalog?.selection_groups)
            ? state.catalog.selection_groups.filter((group) => group?.key)
            : [];
        if (groups.length) {
            return [...groups].sort((left, right) => (
                Number(left.order || 0) - Number(right.order || 0)
            ));
        }
        return [{
            key: 'all-dungeons',
            name: 'All Dungeons',
            name_zh: '全部地下城',
            order: 0,
        }];
    }

    function dungeonSelectionGroups(dungeon) {
        return Array.isArray(dungeon?.selection_groups)
            ? dungeon.selection_groups.filter((group) => group?.key)
            : [];
    }

    function dungeonsForSelectionGroup(groupKey) {
        const dungeons = Array.isArray(state.catalog?.dungeons) ? state.catalog.dungeons : [];
        if (groupKey === 'all-dungeons') return [...dungeons];
        return dungeons
            .filter((dungeon) => (
                dungeonSelectionGroups(dungeon).some((group) => group.key === groupKey)
            ))
            .sort((left, right) => {
                const leftGroup = dungeonSelectionGroups(left).find((group) => group.key === groupKey);
                const rightGroup = dungeonSelectionGroups(right).find((group) => group.key === groupKey);
                return Number(leftGroup?.dungeon_order || 0) - Number(rightGroup?.dungeon_order || 0);
            });
    }

    function selectGroupForDungeon(dungeonKey) {
        const groups = catalogSelectionGroups();
        const dungeon = state.catalog?.dungeons?.find((item) => item.key === dungeonKey);
        const memberships = new Set(dungeonSelectionGroups(dungeon).map((group) => group.key));
        if (
            groups.some((group) => group.key === state.selectionGroupKey)
            && (state.selectionGroupKey === 'all-dungeons' || memberships.has(state.selectionGroupKey))
        ) {
            return;
        }
        state.selectionGroupKey = (
            groups.find((group) => memberships.has(group.key)) || groups[0]
        )?.key || '';
    }

    function renderCatalogSelectors(preferredDungeonKey = '') {
        const groups = catalogSelectionGroups();
        els.seasonSelect.innerHTML = groups
            .map((group) => `
                <option value="${escapeHtml(group.key)}">
                    ${escapeHtml(group.name_zh || group.name || group.key)}
                </option>
            `)
            .join('');
        els.seasonSelect.value = state.selectionGroupKey || groups[0]?.key || '';

        const dungeons = dungeonsForSelectionGroup(els.seasonSelect.value);
        els.dungeonSelect.innerHTML = dungeons
            .map((dungeon) => `<option value="${escapeHtml(dungeon.key)}">${escapeHtml(dungeon.display_name)}</option>`)
            .join('');
        const selected = dungeons.find((dungeon) => dungeon.key === preferredDungeonKey) || dungeons[0];
        els.dungeonSelect.value = selected?.key || '';
        return selected || null;
    }

    function setBusy(busy) {
        els.app?.setAttribute('aria-busy', busy ? 'true' : 'false');
    }

    function setStatus(message) {
        if (els.statusMessage) els.statusMessage.textContent = message;
    }

    function toast(message, isError = false) {
        if (!els.toast) return;
        window.clearTimeout(state.toastTimer);
        els.toast.textContent = message;
        els.toast.classList.toggle('is-error', isError);
        els.toast.hidden = false;
        state.toastTimer = window.setTimeout(() => {
            els.toast.hidden = true;
        }, 3200);
    }

    async function fetchJson(url, options = {}) {
        const headers = {'Content-Type': 'application/json', ...(options.headers || {})};
        const response = await fetch(url, {
            ...options,
            credentials: 'same-origin',
            headers,
        });
        let payload = {};
        try {
            payload = await response.json();
        } catch (_error) {
            const error = new Error(`服务器返回了无法解析的响应（${response.status}）。`);
            error.status = response.status;
            throw error;
        }
        if (!response.ok || payload.success === false) {
            const error = new Error(payload.message || `请求失败（${response.status}）。`);
            error.status = response.status;
            throw error;
        }
        return payload.data;
    }

    function defaultRoute(dungeonKey = '') {
        return {
            local_id: randomId(),
            name: '未命名路线',
            dungeon_key: dungeonKey,
            data_version_key: state.catalog?.version?.key || '',
            floor_key: state.dungeon?.floors?.[0]?.key || '',
            dungeon_level: Number(state.catalog?.config?.default_dungeon_level || 10),
            pulls: [defaultPull(0)],
            current_pull_id: '',
            annotations: [],
            created_at: nowIso(),
            updated_at: nowIso(),
        };
    }

    function catalogDefaultRoutes() {
        return Array.isArray(state.catalog?.default_routes)
            ? state.catalog.default_routes.filter((route) => route?.id && route?.dungeon_key)
            : [];
    }

    function preferredDefaultRoute(dungeonKey = '') {
        const routes = catalogDefaultRoutes().filter((route) => route.is_valid !== false);
        const candidates = dungeonKey
            ? routes.filter((route) => route.dungeon_key === dungeonKey)
            : routes;
        return candidates.find((route) => route.is_featured) || candidates[0] || null;
    }

    function defaultPull(index, color = PULL_COLORS[index % PULL_COLORS.length]) {
        const pull = {
            id: randomId(),
            name: `第 ${index + 1} 波`,
            color,
            spawn_uids: [],
        };
        return pull;
    }

    function nextPullColor(pulls) {
        const rows = Array.isArray(pulls) ? pulls : [];
        const usedColors = new Set(rows.map((pull) => String(pull.color || '').toLowerCase()));
        const lastColor = String(rows[rows.length - 1]?.color || '').toLowerCase();
        const lastColorIndex = PULL_COLORS.findIndex((color) => color.toLowerCase() === lastColor);
        const startIndex = lastColorIndex >= 0 ? lastColorIndex + 1 : rows.length;
        for (let offset = 0; offset < PULL_COLORS.length; offset += 1) {
            const color = PULL_COLORS[(startIndex + offset) % PULL_COLORS.length];
            if (!usedColors.has(color.toLowerCase())) return color;
        }
        return PULL_COLORS[startIndex % PULL_COLORS.length];
    }

    function ensureRoute() {
        if (!state.route || state.route.dungeon_key !== state.dungeon?.key) {
            state.route = defaultRoute(state.dungeon?.key || '');
            state.route.current_pull_id = state.route.pulls[0].id;
        }
        if (!Array.isArray(state.route.pulls) || !state.route.pulls.length) {
            state.route.pulls = [defaultPull(0)];
        }
        if (!state.route.pulls.some((pull) => pull.id === state.route.current_pull_id)) {
            state.route.current_pull_id = state.route.pulls[0].id;
        }
        if (!Array.isArray(state.route.annotations)) state.route.annotations = [];
    }

    function currentPull() {
        ensureRoute();
        return state.route.pulls.find((pull) => pull.id === state.route.current_pull_id) || state.route.pulls[0];
    }

    function routePayload() {
        ensureRoute();
        return {
            version: 1,
            dungeon_key: state.route.dungeon_key,
            data_version_key: state.route.data_version_key,
            name: state.route.name,
            dungeon_level: Number(state.route.dungeon_level || 10),
            current_floor_key: state.floorKey,
            pulls: state.route.pulls.map((pull) => ({
                id: pull.id,
                name: pull.name,
                color: pull.color,
                spawn_uids: Array.from(new Set(pull.spawn_uids || [])),
            })),
            annotations: clone(state.route.annotations || []),
        };
    }

    function loadStoredRoutes() {
        try {
            const rows = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
            return Array.isArray(rows) ? rows.map(stripAccountMetadata) : [];
        } catch (_error) {
            return [];
        }
    }

    function saveStoredRoutes(rows) {
        localStorage.setItem(
            STORAGE_KEY,
            JSON.stringify(rows.slice(0, 100).map(stripAccountMetadata)),
        );
    }

    function stripAccountMetadata(route) {
        const cleaned = {...(route || {})};
        delete cleaned.server_id;
        delete cleaned.server_share_id;
        delete cleaned.server_is_public;
        delete cleaned.server_revision;
        return cleaned;
    }

    function persistRoute() {
        if (!state.route?.dungeon_key) return;
        state.route.updated_at = nowIso();
        const rows = loadStoredRoutes();
        const index = rows.findIndex((row) => row.local_id === state.route.local_id);
        if (index >= 0) rows[index] = clone(state.route);
        else rows.unshift(clone(state.route));
        rows.sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
        saveStoredRoutes(rows);
        localStorage.setItem(SELECTED_KEY, state.route.local_id);
    }

    function removeStoredRoute(localId) {
        saveStoredRoutes(loadStoredRoutes().filter((row) => row.local_id !== localId));
    }

    function restoreSelectedRoute(dungeonKey) {
        const selected = localStorage.getItem(SELECTED_KEY);
        const rows = loadStoredRoutes().filter((row) => row.dungeon_key === dungeonKey);
        const route = rows.find((row) => row.local_id === selected) || rows[0];
        if (!route) return false;
        state.route = normalizeRoute(route);
        return true;
    }

    function sharedRouteRequest() {
        const params = new URLSearchParams(location.search);
        const shareToken = String(document.body.dataset.shareToken || '').trim();
        const legacyShareId = String(params.get('share') || '').trim();
        if (!shareToken && !legacyShareId) return null;
        return {
            shareToken,
            legacyShareId,
            sourceKey: shareToken
                ? `short-link:${shareToken}`
                : `legacy-share:${legacyShareId}`,
        };
    }

    function replaceSharedRouteUrl(dungeonKey) {
        const url = new URL('/portal/mythic-planner/', location.origin);
        if (dungeonKey) url.searchParams.set('dungeon', dungeonKey);
        window.history.replaceState(
            {mythicPlannerLocalImport: true},
            '',
            `${url.pathname}${url.search}`,
        );
        document.body.dataset.shareToken = '';
    }

    function syncDungeonUrl(dungeonKey) {
        if (!dungeonKey || sharedRouteRequest()) return;
        const url = new URL(window.location.href);
        if (url.searchParams.get('dungeon') === dungeonKey) return;
        url.searchParams.set('dungeon', dungeonKey);
        window.history.replaceState(
            {...(window.history.state || {}), mythicPlannerDungeon: dungeonKey},
            '',
            `${url.pathname}${url.search}${url.hash}`,
        );
    }

    function normalizeRoute(route) {
        const normalized = {...defaultRoute(route?.dungeon_key || state.dungeon?.key || ''), ...(route || {})};
        normalized.local_id = normalized.local_id || randomId();
        normalized.pulls = Array.isArray(normalized.pulls)
            ? normalized.pulls.map((pull, index) => ({
                id: pull.id || randomId(),
                name: pull.name || `第 ${index + 1} 波`,
                color: pull.color || PULL_COLORS[index % PULL_COLORS.length],
                spawn_uids: Array.isArray(pull.spawn_uids) ? Array.from(new Set(pull.spawn_uids.map(String))) : [],
            }))
            : [];
        if (!normalized.pulls.length) normalized.pulls = [defaultPull(0)];
        normalized.current_pull_id = normalized.current_pull_id || normalized.pulls[0].id;
        normalized.annotations = Array.isArray(normalized.annotations) ? normalized.annotations : [];
        return normalized;
    }

    function pushHistory() {
        if (!state.route) return;
        state.history.push(clone(state.route));
        if (state.history.length > 100) state.history.shift();
        state.future = [];
    }

    function mutateRoute(callback, {render = true, broadcast = true} = {}) {
        ensureRoute();
        pushHistory();
        callback(state.route);
        state.route.updated_at = nowIso();
        persistRoute();
        if (render) renderAll();
        if (broadcast) broadcastRoute();
    }

    function undo() {
        if (!state.history.length || !state.route) return;
        state.future.push(clone(state.route));
        state.route = state.history.pop();
        persistRoute();
        renderAll();
        broadcastRoute();
        setStatus('已撤销上一步路线修改。');
    }

    function redo() {
        if (!state.future.length || !state.route) return;
        state.history.push(clone(state.route));
        state.route = state.future.pop();
        persistRoute();
        renderAll();
        broadcastRoute();
        setStatus('已恢复路线修改。');
    }

    async function loadCatalog() {
        setBusy(true);
        try {
            state.catalog = await fetchJson('/portal/api/mythic-planner/catalog/');
            if (!state.catalog.version || !state.catalog.dungeons.length) {
                showEmptyState();
                return;
            }
            els.datasetLabel.textContent = [
                state.catalog.version.label,
                state.catalog.version.season,
            ].filter(Boolean).join(' · ');
            const params = new URLSearchParams(window.location.search);
            const requested = params.get('dungeon');
            const configured = state.catalog.config.default_dungeon_key;
            const shareRequest = sharedRouteRequest();
            const localRoutes = loadStoredRoutes();
            const globalDefaultRoute = preferredDefaultRoute();
            const requestedDungeon = state.catalog.dungeons.find((dungeon) => dungeon.key === requested);
            const defaultDungeon = (
                !shareRequest
                && !localRoutes.length
                && !requestedDungeon
                && globalDefaultRoute
            )
                ? state.catalog.dungeons.find((dungeon) => dungeon.key === globalDefaultRoute.dungeon_key)
                : null;
            const initial = requestedDungeon
                || defaultDungeon
                || state.catalog.dungeons.find((dungeon) => dungeon.key === configured)
                || state.catalog.dungeons[0];
            const autoDefaultRoute = (
                !shareRequest && !localRoutes.length
                    ? preferredDefaultRoute(initial.key)
                    : null
            );
            selectGroupForDungeon(initial.key);
            renderCatalogSelectors(initial.key);
            configureLevelSlider();
            await loadDungeon(initial.key, {
                restore: !shareRequest && localRoutes.length > 0,
                persist: !shareRequest && (localRoutes.length > 0 || !autoDefaultRoute),
            });
            await maybeLoadSharedRoute();
            if (autoDefaultRoute) {
                await applyDefaultRoute(autoDefaultRoute, {automatic: true});
            } else {
                setStatus(`已加载 ${state.catalog.version.label}。`);
            }
        } catch (error) {
            showEmptyState(error.message);
            toast(error.message, true);
        } finally {
            setBusy(false);
        }
    }

    function configureLevelSlider() {
        const config = state.catalog?.config || {};
        els.level.min = Number(config.min_dungeon_level || 2);
        els.level.max = Number(config.max_dungeon_level || 35);
        els.level.value = Number(config.default_dungeon_level || 10);
    }

    function showEmptyState(message = '') {
        els.mapEmpty.hidden = false;
        els.mapContent.hidden = true;
        els.seasonSelect.innerHTML = '<option>尚无数据</option>';
        els.dungeonSelect.innerHTML = '<option>尚无数据</option>';
        els.datasetLabel.textContent = message || '等待首次初始化';
        setStatus(message || '请先运行数据初始化命令。');
    }

    async function loadDungeon(
        dungeonKey,
        {restore = false, route = null, persist = true} = {},
    ) {
        closeEnemyDetail();
        setBusy(true);
        try {
            const dungeon = await fetchJson(`/portal/api/mythic-planner/dungeons/${encodeURIComponent(dungeonKey)}/`);
            state.dungeon = dungeon;
            state.enemyByUid = new Map();
            state.spawnByUid = new Map();
            for (const enemy of dungeon.enemies || []) {
                for (const spawn of enemy.spawns || []) {
                    state.enemyByUid.set(spawn.uid, enemy);
                    state.spawnByUid.set(spawn.uid, spawn);
                }
            }
            state.floorKey = dungeon.floors?.[0]?.key || '';
            state.zoom = 1;
            state.panX = 0;
            state.panY = 0;
            state.history = [];
            state.future = [];
            state.detailUid = '';
            if (route) {
                state.route = normalizeRoute(route);
            } else if (!(restore && restoreSelectedRoute(dungeon.key))) {
                state.route = defaultRoute(dungeon.key);
                state.route.current_pull_id = state.route.pulls[0].id;
            }
            ensureRoute();
            const routeFloor = state.route.floor_key || state.route.current_floor_key;
            if (dungeon.floors.some((floor) => floor.key === routeFloor)) state.floorKey = routeFloor;
            state.route.floor_key = state.floorKey;
            state.route.dungeon_key = dungeon.key;
            state.route.data_version_key = dungeon.data_version.key;
            selectGroupForDungeon(dungeon.key);
            renderCatalogSelectors(dungeon.key);
            syncDungeonUrl(dungeon.key);
            els.mapEmpty.hidden = true;
            els.mapContent.hidden = false;
            if (persist) persistRoute();
            renderAll();
        } catch (error) {
            toast(error.message, true);
            setStatus(error.message);
        } finally {
            setBusy(false);
        }
    }

    function renderAll() {
        if (!state.dungeon || !state.route) return;
        ensureRoute();
        renderHeader();
        renderMap();
        renderPulls();
        renderProgress();
        if (!els.enemyDetailModal.hidden) renderEnemyDetail();
        renderViewTransform();
        renderToolState();
    }

    function renderHeader() {
        els.routeNameButton.textContent = state.route.name;
        els.floorTabs.innerHTML = (state.dungeon.floors || []).map((floor) => `
            <button
                type="button"
                role="tab"
                data-floor-key="${escapeHtml(floor.key)}"
                class="${floor.key === state.floorKey ? 'is-active' : ''}"
                aria-selected="${floor.key === state.floorKey ? 'true' : 'false'}"
                title="${escapeHtml(floor.display_name)}"
            >${escapeHtml(floor.display_name)}</button>
        `).join('');
        const floor = currentFloor();
        els.mapDungeonName.textContent = state.dungeon.display_name;
        els.mapFloorName.textContent = floor?.display_name || '';
        els.level.value = Number(state.route.dungeon_level || 10);
        els.levelOutput.textContent = String(state.route.dungeon_level || 10);
    }

    function currentFloor() {
        return state.dungeon?.floors?.find((floor) => floor.key === state.floorKey) || state.dungeon?.floors?.[0] || null;
    }

    function renderMapLayout() {
        const floor = currentFloor();
        if (!floor) return;
        const mapWidth = Math.min(
            els.mapViewport.clientWidth || Number(floor.map_width || 1000),
            Number(floor.map_width || 1000),
        );
        els.mapContent.style.setProperty('--map-layout-width', `${mapWidth * state.zoom}px`);
        els.mapContent.style.setProperty('--map-zoom', String(state.zoom));
    }

    function renderMap() {
        const floor = currentFloor();
        if (!floor) return;
        renderMapLayout();
        els.mapContent.style.aspectRatio = `${Number(floor.map_width || 1000)} / ${Number(floor.map_height || 700)}`;
        els.mapContent.style.backgroundColor = floor.background_color || '#66533f';
        const texture = $('.mdt-map-texture', els.mapContent);
        if (floor.background_url) {
            texture.style.backgroundImage = `linear-gradient(#080b0f2e, #080b0f2e), url("${String(floor.background_url).replaceAll('"', '%22')}")`;
            texture.style.backgroundSize = 'cover';
            texture.style.backgroundPosition = 'center';
        } else {
            texture.style.backgroundImage = '';
            texture.style.backgroundSize = '';
            texture.style.backgroundPosition = '';
        }
        renderPatrols();
        renderPullArea();
        renderScaledMapLayers();
    }

    function renderScaledMapLayers() {
        renderPois();
        renderSpawns();
        renderAnnotations();
    }

    function floorSpawns() {
        const rows = [];
        for (const enemy of state.dungeon?.enemies || []) {
            for (const spawn of enemy.spawns || []) {
                if (spawn.floor_key === state.floorKey) rows.push({enemy, spawn});
            }
        }
        return rows;
    }

    function renderPatrols() {
        const lines = [];
        for (const {spawn} of floorSpawns()) {
            if (!Array.isArray(spawn.patrol) || spawn.patrol.length < 2) continue;
            const points = spawn.patrol
                .filter((point) => Number.isFinite(Number(point.x)) && Number.isFinite(Number(point.y)))
                .map((point) => `${Number(point.x)},${Number(point.y)}`)
                .join(' ');
            if (points) lines.push(`<polyline class="mdt-patrol-line" points="${points}"></polyline>`);
        }
        els.patrolLayer.innerHTML = lines.join('');
    }

    function spawnMarkerSize(spawn) {
        const sourceScale = Number(spawn?.scale || 1);
        const baseMarkerSize = clamp(
            13 * (Number.isFinite(sourceScale) ? sourceScale : 1),
            8,
            30,
        );
        return baseMarkerSize + 1;
    }

    function scaledSpawnMarkerSize(spawn) {
        return spawnMarkerSize(spawn) * state.zoom;
    }

    function pointDistance(left, right) {
        return Math.hypot(right.x - left.x, right.y - left.y);
    }

    function spawnOutlineRadius(spawn) {
        return (
            scaledSpawnMarkerSize(spawn) / 2
            + PULL_AREA_SELECTED_RING_PX
            + PULL_AREA_PADDING_PX
        );
    }

    function circleBoundaryPoints(center, radius) {
        return Array.from({length: PULL_AREA_CIRCLE_SEGMENTS}, (_, index) => {
            const angle = (Math.PI * 2 * index) / PULL_AREA_CIRCLE_SEGMENTS;
            return {
                x: center.x + Math.cos(angle) * radius,
                y: center.y + Math.sin(angle) * radius,
            };
        });
    }

    function convexHull(points) {
        const unique = Array.from(new Map(
            points.map((point) => [`${point.x.toFixed(3)}:${point.y.toFixed(3)}`, point]),
        ).values()).sort((left, right) => left.x - right.x || left.y - right.y);
        if (unique.length <= 2) return unique;
        const cross = (origin, left, right) => (
            (left.x - origin.x) * (right.y - origin.y)
            - (left.y - origin.y) * (right.x - origin.x)
        );
        const lower = [];
        for (const point of unique) {
            while (lower.length >= 2 && cross(lower.at(-2), lower.at(-1), point) <= 0) lower.pop();
            lower.push(point);
        }
        const upper = [];
        for (const point of [...unique].reverse()) {
            while (upper.length >= 2 && cross(upper.at(-2), upper.at(-1), point) <= 0) upper.pop();
            upper.push(point);
        }
        lower.pop();
        upper.pop();
        return lower.concat(upper);
    }

    function pointTowards(from, to, distance) {
        const length = pointDistance(from, to) || 1;
        const ratio = Math.min(distance, length / 2) / length;
        return {
            x: from.x + (to.x - from.x) * ratio,
            y: from.y + (to.y - from.y) * ratio,
        };
    }

    function roundedPolygonPath(points, radius) {
        const corners = points.map((point, index) => {
            const previous = points[(index - 1 + points.length) % points.length];
            const next = points[(index + 1) % points.length];
            return {
                point,
                before: pointTowards(point, previous, radius),
                after: pointTowards(point, next, radius),
            };
        });
        const format = (point) => `${point.x.toFixed(2)},${point.y.toFixed(2)}`;
        let path = `M ${format(corners[0].before)} Q ${format(corners[0].point)} ${format(corners[0].after)}`;
        for (const corner of corners.slice(1)) {
            path += ` L ${format(corner.before)} Q ${format(corner.point)} ${format(corner.after)}`;
        }
        return `${path} Z`;
    }

    function pullAreaLabel(points, padding) {
        if (points.length < 3) return null;
        const center = points.reduce(
            (total, point) => ({x: total.x + point.x, y: total.y + point.y}),
            {x: 0, y: 0},
        );
        center.x /= points.length;
        center.y /= points.length;
        const nearestSpawn = Math.min(...points.map((point) => pointDistance(center, point)));
        return nearestSpawn >= padding * 1.65 ? center : null;
    }

    function pullAreaMarkup(pull, pullIndex, width, height, isCurrent) {
        const spawns = (pull.spawn_uids || [])
            .map((uid) => state.spawnByUid.get(uid))
            .filter((spawn) => spawn && spawn.floor_key === state.floorKey);
        if (!spawns.length) return '';
        const points = spawns.map((spawn) => ({
            x: (Number(spawn.x) / 100) * width,
            y: (Number(spawn.y) / 100) * height,
        }));
        const outlineRadii = spawns.map((spawn) => spawnOutlineRadius(spawn));
        const hull = convexHull(spawns.flatMap((spawn, index) => (
            circleBoundaryPoints(points[index], outlineRadii[index])
        )));
        const color = escapeHtml(pull.color || PULL_COLORS[0]);
        let shape = '';
        if (spawns.length === 1) {
            shape = `
                <ellipse
                    class="mdt-pull-area-shape"
                    cx="${points[0].x.toFixed(2)}"
                    cy="${points[0].y.toFixed(2)}"
                    rx="${outlineRadii[0].toFixed(2)}"
                    ry="${outlineRadii[0].toFixed(2)}"
                    fill="${color}"
                    stroke="${color}"
                    style="color:${color}"
                ></ellipse>
            `;
        } else {
            shape = `
                <path
                    class="mdt-pull-area-shape"
                    d="${roundedPolygonPath(hull, PULL_AREA_CORNER_RADIUS_PX)}"
                    fill="${color}"
                    stroke="${color}"
                    style="color:${color}"
                ></path>
            `;
        }
        const labelPoint = pullAreaLabel(points, Math.max(...outlineRadii));
        const label = labelPoint ? `
            <text
                class="mdt-pull-area-label"
                x="${labelPoint.x.toFixed(2)}"
                y="${labelPoint.y.toFixed(2)}"
            >${pullIndex + 1}</text>
        ` : '';
        return `
            <g
                class="mdt-pull-area${isCurrent ? ' is-current' : ''}"
                data-pull-area-id="${escapeHtml(pull.id)}"
            >${shape}${label}</g>
        `;
    }

    function renderPullArea() {
        if (!state.route || !state.dungeon) {
            els.pullAreaLayer.innerHTML = '';
            return;
        }
        const floor = currentFloor();
        const width = els.mapContent.clientWidth || Number(floor?.map_width || 1000);
        const height = els.mapContent.clientHeight || Number(floor?.map_height || 700);
        els.pullAreaLayer.setAttribute('viewBox', `0 0 ${width} ${height}`);
        els.pullAreaLayer.setAttribute('preserveAspectRatio', 'none');
        if (width <= 0 || height <= 0) {
            els.pullAreaLayer.innerHTML = '';
            return;
        }
        const pullAreas = (state.route.pulls || []).map((pull, pullIndex) => ({
            pull,
            pullIndex,
            isCurrent: pull.id === state.route.current_pull_id,
        }));
        pullAreas.sort(
            (left, right) => Number(left.isCurrent) - Number(right.isCurrent),
        );
        els.pullAreaLayer.innerHTML = pullAreas.map((item) => (
            pullAreaMarkup(
                item.pull,
                item.pullIndex,
                width,
                height,
                item.isCurrent,
            )
        )).join('');
    }

    function renderPois() {
        const floor = currentFloor();
        els.poiLayer.innerHTML = (floor?.pois || []).map((poi) => {
            const poiType = String(poi.type || 'note');
            const typeClass = poiType.replace(/([a-z])([A-Z])/g, '$1-$2').toLowerCase();
            const rawSize = Number(poi.size);
            const displayScale = state.zoom;
            const size = (Number.isFinite(rawSize) ? clamp(rawSize, 12, 30) : 20) * displayScale;
            const typeLabel = POI_TYPE_LABELS[poiType] || poiType;
            const title = [poi.label || typeLabel, poi.spell_id ? `Spell ${poi.spell_id}` : '']
                .filter(Boolean)
                .join(' · ');
            const tooltipId = `poi-tooltip-${String(poi.id || poi.key || 'node')}`
                .replace(/[^a-zA-Z0-9_-]/g, '-');
            const tooltipClasses = [
                'mdt-poi-tooltip',
                Number(poi.y) < 15 ? 'is-below' : '',
                Number(poi.x) < 18 ? 'is-right-aligned' : '',
                Number(poi.x) > 82 ? 'is-left-aligned' : '',
            ].filter(Boolean).join(' ');
            const showLabel = Boolean(poi.label)
                && !['genericItem', 'genericAssignablePOI'].includes(poiType);
            const image = poi.icon_url
                ? `<img class="mdt-poi-image" src="${escapeHtml(poi.icon_url)}" alt="" loading="lazy" onerror="this.hidden=true">`
                : '';
            const description = String(poi.description || '').trim();
            return `
                <div
                    class="mdt-poi is-${escapeHtml(typeClass)}"
                    style="left:${Number(poi.x)}%;top:${Number(poi.y)}%;--poi-size:${size}px"
                    data-poi-type="${escapeHtml(poiType)}"
                    tabindex="0"
                    role="img"
                    aria-label="${escapeHtml(title)}"
                    aria-describedby="${escapeHtml(tooltipId)}"
                >
                    <span class="mdt-poi-glyph" aria-hidden="true">${POI_ICONS[poiType] || '◆'}</span>
                    ${image}
                    ${showLabel ? `<span class="mdt-poi-label">${escapeHtml(poi.label)}</span>` : ''}
                    <span class="${tooltipClasses}" id="${escapeHtml(tooltipId)}" role="tooltip">
                        <strong>${escapeHtml(poi.label || typeLabel)}</strong>
                        <small>${escapeHtml(typeLabel)}${poi.spell_id ? ` · Spell ${Number(poi.spell_id)}` : ''}</small>
                        ${description ? `<span class="mdt-poi-tooltip-description">${escapeHtml(description)}</span>` : ''}
                    </span>
                </div>
            `;
        }).join('');
    }

    function pullForUid(uid) {
        return state.route.pulls.find((pull) => (pull.spawn_uids || []).includes(uid)) || null;
    }

    function renderSpawns() {
        els.spawnLayer.innerHTML = floorSpawns().map(({enemy, spawn}) => {
            const pull = pullForUid(spawn.uid);
            const patrol = Array.isArray(spawn.patrol) && spawn.patrol.length > 0;
            const markerInitial = initials(enemy.display_name);
            const displayScale = state.zoom;
            const markerSize = spawnMarkerSize(spawn) * displayScale;
            const baseMarkerSize = markerSize - 1;
            const markerFontSize = clamp(baseMarkerSize * 0.55, 4, 13) + 1;
            const markerWideFontSize = clamp(baseMarkerSize * 0.36, 3.25, 9) + 1;
            return `
                <button
                    type="button"
                    class="mdt-spawn ${enemy.is_boss ? 'is-boss' : ''} ${patrol ? 'is-patrol' : ''} ${pull ? 'is-selected' : ''}"
                    data-spawn-uid="${escapeHtml(spawn.uid)}"
                    style="
                        left:${Number(spawn.x)}%;
                        top:${Number(spawn.y)}%;
                        --marker:${escapeHtml(enemy.marker_color || '#94a3b8')};
                        --pull-color:${escapeHtml(pull?.color || '#ffffff')};
                        --spawn-size:${markerSize.toFixed(2)}px;
                        --spawn-font-size:${markerFontSize.toFixed(2)}px;
                        --spawn-wide-font-size:${markerWideFontSize.toFixed(2)}px;
                    "
                    aria-label="${escapeHtml(enemy.display_name)}"
                    aria-haspopup="dialog"
                    aria-controls="enemy-detail-modal"
                    title=""
                >
                    <span class="mdt-spawn-initial ${markerInitial.length > 1 ? 'is-wide' : ''}" aria-hidden="true">${escapeHtml(markerInitial)}</span>
                    ${enemy.enemy_forces ? `<span class="mdt-spawn-count">${enemy.enemy_forces}</span>` : ''}
                </button>
            `;
        }).join('');
    }

    function renderAnnotations() {
        const shapes = [];
        const notes = [];
        for (const annotation of state.route.annotations || []) {
            if (annotation.floor_key !== state.floorKey) continue;
            const color = annotation.color || '#facc15';
            if (annotation.type === 'note') {
                notes.push(`
                    <div
                        class="mdt-map-note"
                        data-annotation-id="${escapeHtml(annotation.id)}"
                        style="left:${Number(annotation.x)}%;top:${Number(annotation.y)}%;--note-color:${escapeHtml(color)}"
                        tabindex="0"
                        title="选择工具下拖动调整位置；双击编辑文字"
                    >${escapeHtml(annotation.text || '')}</div>
                `);
                continue;
            }
            const points = (annotation.points || []).map((point) => `${Number(point.x)},${Number(point.y)}`).join(' ');
            if (!points) continue;
            if (annotation.type === 'line' || annotation.type === 'arrow') {
                const marker = annotation.type === 'arrow' ? ' marker-end="url(#annotation-arrow-head)"' : '';
                shapes.push(`<polyline class="mdt-annotation-shape" data-annotation-id="${escapeHtml(annotation.id)}" points="${points}" stroke="${escapeHtml(color)}"${marker}></polyline>`);
            } else {
                shapes.push(`<polyline class="mdt-annotation-shape" data-annotation-id="${escapeHtml(annotation.id)}" points="${points}" stroke="${escapeHtml(color)}"></polyline>`);
            }
        }
        els.annotationShapes.innerHTML = shapes.join('');
        els.annotationNotes.innerHTML = notes.join('');
    }

    function pullStats(pull) {
        let forces = 0;
        const counts = new Map();
        for (const uid of pull.spawn_uids || []) {
            const enemy = state.enemyByUid.get(uid);
            if (!enemy) continue;
            forces += Number(enemy.enemy_forces || 0);
            const row = counts.get(enemy.key) || {enemy, count: 0};
            row.count += 1;
            counts.set(enemy.key, row);
        }
        return {forces, counts: Array.from(counts.values())};
    }

    function scaledHealth(baseHealth, level) {
        const numericLevel = Number(level || 10);
        const multiplier = Math.pow(1.1, Math.max(0, numericLevel - 2));
        return Math.round(Number(baseHealth || 0) * multiplier);
    }

    function renderPulls() {
        els.pullList.innerHTML = state.route.pulls.map((pull, index) => {
            const stats = pullStats(pull);
            const targetForces = Number(state.dungeon.total_enemy_forces || 0);
            const forcesPercent = targetForces ? (stats.forces / targetForces) * 100 : 0;
            const isCurrent = pull.id === state.route.current_pull_id;
            const icons = stats.counts.length
                ? stats.counts.map(({enemy, count}) => `
                    <span class="mdt-pull-mini" title="${escapeHtml(enemy.display_name)} × ${count}" style="border-color:${escapeHtml(enemy.marker_color || '#fff')}">
                        ${escapeHtml(initials(enemy.display_name))}${count > 1 ? `<small>×${count}</small>` : ''}
                    </span>
                `).join('')
                : '<span class="mdt-pull-empty">点击地图怪物加入这一波</span>';
            return `
                <article
                    class="mdt-pull ${isCurrent ? 'is-active' : ''}"
                    data-pull-id="${escapeHtml(pull.id)}"
                    style="--pull-color:${escapeHtml(pull.color)}"
                    tabindex="0"
                    aria-current="${isCurrent ? 'true' : 'false'}"
                    aria-label="第 ${index + 1} 波，${stats.forces} 进度，${forcesPercent.toFixed(2)}%"
                    title="${isCurrent ? '当前波次：点击地图怪物可增加或删除' : `点击切换到第 ${index + 1} 波`}"
                >
                    <div class="mdt-pull-number" title="按住上下拖动调整顺序">${index + 1}</div>
                    <div class="mdt-pull-body">
                        <div class="mdt-pull-icons">${icons}</div>
                    </div>
                    <div class="mdt-pull-meta">
                        <strong>${stats.forces}</strong>
                        <span>进度 · ${forcesPercent.toFixed(2)}%</span>
                        <button type="button" data-pull-action="delete" title="删除这一波">×</button>
                    </div>
                </article>
            `;
        }).join('');
    }

    function renderProgress() {
        let totalForces = 0;
        let selectedCount = 0;
        for (const pull of state.route.pulls) {
            totalForces += pullStats(pull).forces;
            selectedCount += (pull.spawn_uids || []).length;
        }
        const target = Number(state.dungeon.total_enemy_forces || 0);
        const percent = target ? (totalForces / target) * 100 : 0;
        els.progressBar.style.width = `${clamp(percent, 0, 100)}%`;
        els.progressBar.style.background = percent > 100
            ? 'linear-gradient(#f87171, #b91c1c)'
            : 'linear-gradient(#55ee77, #158533)';
        els.progressText.textContent = `${totalForces} / ${target}（${percent.toFixed(2)}%）`;
        els.selectedEnemyCount.textContent = `已选择 ${selectedCount} 个怪物`;
        els.routeWarning.textContent = percent > 100 ? `超出 ${(percent - 100).toFixed(2)}%` : '';
    }

    function renderEnemyDetail() {
        const enemy = state.enemyByUid.get(state.detailUid);
        const spawn = state.spawnByUid.get(state.detailUid);
        if (!enemy || !spawn) {
            closeEnemyDetail();
            return;
        }
        const traits = Object.entries(TRAIT_LABELS)
            .filter(([key]) => Boolean(enemy.traits?.[key]))
            .map(([, label]) => `<span class="mdt-trait is-allowed">${label}</span>`)
            .join('');
        const traitSection = traits
            ? `<div class="mdt-traits">${traits}</div>`
            : '<p class="mdt-enemy-no-traits">没有已记录的可用控制特性。</p>';
        const abilities = (enemy.abilities || []).map((ability) => {
            const description = ability.description_zh || ability.description || '暂无技能说明。';
            const tags = [
                ability.interruptible ? '可打断' : '',
                ability.dispel_type ? `可驱散：${ability.dispel_type}` : '',
                `法术 ${ability.spell_id}`,
            ].filter(Boolean);
            const icon = ability.icon_url
                ? `
                    <span class="mdt-ability-icon-frame">
                        <span class="mdt-ability-icon-fallback" aria-hidden="true">?</span>
                        <img
                            class="mdt-ability-icon"
                            src="${escapeHtml(ability.icon_url)}"
                            alt=""
                            loading="lazy"
                            onerror="this.parentElement.classList.add('is-error');this.remove()"
                        >
                    </span>
                `
                : `
                    <span class="mdt-ability-icon-frame is-error">
                        <span class="mdt-ability-icon-fallback" aria-hidden="true">?</span>
                    </span>
                `;
            return `
                <div class="mdt-ability">
                    <span class="mdt-danger-${clamp(Number(ability.danger_level || 1), 1, 3)}"></span>
                    ${icon}
                    <div class="mdt-ability-copy">
                        <strong>${escapeHtml(ability.display_name)}</strong>
                        <p>${escapeHtml(description)}</p>
                        <div class="mdt-ability-tags">${tags.map((tag) => `<span>${escapeHtml(tag)}</span>`).join('')}</div>
                    </div>
                </div>
            `;
        }).join('');
        const health = scaledHealth(enemy.base_health, state.route.dungeon_level);
        const portraitInitial = escapeHtml(initials(enemy.display_name));
        const displayId = enemyDisplayId(enemy);
        const modelPreviewUrl = String(enemy.model_preview_url || '').trim();
        const wowheadPageUrl = enemy.npc_id
            ? `https://www.wowhead.com/npc=${Number(enemy.npc_id)}`
            : '';
        const modelPreview = displayId && modelPreviewUrl
            ? `
                <figure class="mdt-enemy-model-preview">
                    <div class="mdt-enemy-model">
                        <span class="mdt-enemy-model-fallback" aria-hidden="true">${portraitInitial}</span>
                        <img
                            src="${escapeHtml(modelPreviewUrl)}"
                            alt="${escapeHtml(enemy.display_name)}的模型预览"
                            decoding="async"
                            referrerpolicy="no-referrer"
                            onerror="this.parentElement.classList.add('is-error');this.remove()"
                        >
                        <span class="mdt-enemy-model-badge">模型预览</span>
                    </div>
                    <figcaption>
                        <span>Display ID ${displayId}</span>
                        ${wowheadPageUrl ? `<a href="${escapeHtml(wowheadPageUrl)}" target="_blank" rel="noopener noreferrer">Wowhead · 查看 3D</a>` : ''}
                    </figcaption>
                </figure>
            `
            : (enemy.icon_url ? `
                <div class="mdt-enemy-portrait">
                    <span class="mdt-enemy-portrait-fallback" aria-hidden="true">${portraitInitial}</span>
                    <img
                        src="${escapeHtml(enemy.icon_url)}"
                        alt="${escapeHtml(enemy.display_name)}头像"
                        loading="lazy"
                        onerror="this.parentElement.classList.add('is-error');this.remove()"
                    >
                </div>
            ` : `
                <div class="mdt-enemy-portrait is-error" aria-hidden="true">
                    <span class="mdt-enemy-portrait-fallback">${portraitInitial}</span>
                </div>
            `);
        els.enemyDetailTitle.textContent = enemy.display_name;
        els.enemyDetail.innerHTML = `
            <div class="mdt-enemy-detail-layout" style="--enemy-color:${escapeHtml(enemy.marker_color || '#94a3b8')}">
                <section class="mdt-enemy-profile">
                    ${modelPreview}
                    <div class="mdt-enemy-profile-copy">
                        <h3>${escapeHtml(enemy.display_name)}</h3>
                        <p>${escapeHtml(enemy.creature_type || '未知类型')}</p>
                        <span>${enemy.is_boss ? '首领单位' : '地下城敌人'}</span>
                    </div>
                    <div class="mdt-enemy-characteristics">
                        <h4>控制与特性</h4>
                        ${traitSection}
                    </div>
                </section>
                <section class="mdt-enemy-data">
                    <h3>基础资料</h3>
                    <dl>
                        <div><dt>NPC ID</dt><dd>${enemy.npc_id || '—'}</dd></div>
                        <div><dt>敌方部队进度</dt><dd>${enemy.enemy_forces || 0}</dd></div>
                        <div><dt>当前钥匙层数</dt><dd>+${state.route.dungeon_level}</dd></div>
                        <div><dt>预计生命值</dt><dd>${formatNumber(health)}</dd></div>
                        <div><dt>编队</dt><dd>${escapeHtml(spawn.group_key || '单独单位')}</dd></div>
                        <div><dt>移动方式</dt><dd>${Array.isArray(spawn.patrol) && spawn.patrol.length ? '巡逻' : '固定位置'}</dd></div>
                    </dl>
                </section>
                <section class="mdt-enemy-spells">
                    <header>
                        <div>
                            <h3>技能列表</h3>
                            <span>显示该怪物已记录的技能与战术标签</span>
                        </div>
                        <strong>${(enemy.abilities || []).length}</strong>
                    </header>
                    <div class="mdt-ability-list">${abilities || '<div class="mdt-enemy-empty">该怪物暂无技能记录。</div>'}</div>
                </section>
            </div>
        `;
    }

    function openEnemyDetail(uid) {
        if (!state.enemyByUid.has(uid) || !state.spawnByUid.has(uid)) return;
        state.detailUid = uid;
        els.enemyDetailModal.hidden = false;
        renderEnemyDetail();
        window.setTimeout(() => $('[data-close-enemy-detail]', els.enemyDetailModal)?.focus(), 0);
    }

    function closeEnemyDetail() {
        state.detailUid = '';
        if (els.enemyDetailModal) els.enemyDetailModal.hidden = true;
    }

    function renderViewTransform() {
        renderMapLayout();
        renderPullArea();
        els.mapContent.style.transform = `translate(-50%, -50%) translate(${state.panX}px, ${state.panY}px)`;
        els.zoomOutput.textContent = `${Math.round(state.zoom * 100)}%`;
        els.mapViewport.classList.toggle('is-zoomed', state.zoom > 1);
    }

    function setTool(tool) {
        if (!TOOL_HINTS[tool]) return;
        state.tool = tool;
        renderToolState();
    }

    function renderToolState() {
        $$('[data-tool]', els.toolbar).forEach((button) => {
            button.classList.toggle('is-active', button.dataset.tool === state.tool);
        });
        els.mapViewport.dataset.tool = state.tool;
        els.mapCursorInfo.textContent = TOOL_HINTS[state.tool] || '';
    }

    function selectionUids(spawn, useGroup) {
        if (!useGroup || !spawn.group_key) return [spawn.uid];
        return floorSpawns()
            .filter((row) => row.spawn.group_key === spawn.group_key)
            .map((row) => row.spawn.uid);
    }

    function toggleSpawn(uid, {individual = false} = {}) {
        const spawn = state.spawnByUid.get(uid);
        if (!spawn) return;
        const configGroup = state.catalog?.config?.group_selection_default !== false;
        const uids = selectionUids(spawn, configGroup && !individual);
        mutateRoute((route) => {
            const pull = route.pulls.find((row) => row.id === route.current_pull_id) || route.pulls[0];
            const selectedInCurrentPull = uids.some((item) => (pull.spawn_uids || []).includes(item));
            for (const row of route.pulls) {
                row.spawn_uids = (row.spawn_uids || []).filter((item) => !uids.includes(item));
            }
            if (!selectedInCurrentPull) {
                pull.spawn_uids.push(...uids.filter((item) => state.spawnByUid.has(item)));
                pull.spawn_uids = Array.from(new Set(pull.spawn_uids));
            }
        });
    }

    function selectBox(start, end) {
        const left = Math.min(start.x, end.x);
        const right = Math.max(start.x, end.x);
        const top = Math.min(start.y, end.y);
        const bottom = Math.max(start.y, end.y);
        const uids = floorSpawns()
            .filter(({spawn}) => spawn.x >= left && spawn.x <= right && spawn.y >= top && spawn.y <= bottom)
            .map(({spawn}) => spawn.uid);
        if (!uids.length) return;
        mutateRoute((route) => {
            for (const pull of route.pulls) {
                pull.spawn_uids = (pull.spawn_uids || []).filter((uid) => !uids.includes(uid));
            }
            const pull = route.pulls.find((row) => row.id === route.current_pull_id) || route.pulls[0];
            pull.spawn_uids.push(...uids);
            pull.spawn_uids = Array.from(new Set(pull.spawn_uids));
        });
        setStatus(`已框选 ${uids.length} 个怪物加入当前拉怪组。`);
    }

    function addPull() {
        mutateRoute((route) => {
            const pull = defaultPull(route.pulls.length, nextPullColor(route.pulls));
            route.pulls.push(pull);
            route.current_pull_id = pull.id;
        });
        els.pullList.scrollTop = els.pullList.scrollHeight;
    }

    function selectPull(pullId) {
        const index = state.route.pulls.findIndex((pull) => pull.id === pullId);
        if (index < 0) return;
        state.route.current_pull_id = pullId;
        persistRoute();
        renderPulls();
        renderPullArea();
        setStatus(`正在编辑第 ${index + 1} 波；点击地图怪物可增加、移除或转入这一波。`);
    }

    function suppressNextPullClick() {
        state.suppressPullClick = true;
        window.setTimeout(() => {
            state.suppressPullClick = false;
        }, 0);
    }

    function handlePullAction(pullId, action) {
        const index = state.route.pulls.findIndex((pull) => pull.id === pullId);
        if (index < 0) return;
        if (action === 'delete') {
            if (state.route.pulls.length === 1) {
                toast('路线至少需要保留一个拉怪组。', true);
                return;
            }
            mutateRoute((route) => {
                route.pulls.splice(index, 1);
                if (route.current_pull_id === pullId) {
                    route.current_pull_id = route.pulls[Math.min(index, route.pulls.length - 1)].id;
                }
            });
        }
    }

    function clearPullDropIndicators() {
        $$('.mdt-pull', els.pullList).forEach((row) => {
            row.classList.remove('is-dragging', 'is-drop-before', 'is-drop-after');
        });
    }

    function reorderPull(sourcePullId, targetPullId, placeAfter) {
        if (!sourcePullId || !targetPullId || sourcePullId === targetPullId) return;
        mutateRoute((route) => {
            const sourceIndex = route.pulls.findIndex((pull) => pull.id === sourcePullId);
            if (sourceIndex < 0) return;
            const [sourcePull] = route.pulls.splice(sourceIndex, 1);
            let targetIndex = route.pulls.findIndex((pull) => pull.id === targetPullId);
            if (targetIndex < 0) {
                route.pulls.splice(sourceIndex, 0, sourcePull);
                return;
            }
            if (placeAfter) targetIndex += 1;
            route.pulls.splice(targetIndex, 0, sourcePull);
        });
    }

    function onPullPointerDown(event) {
        if (event.button !== 0 || event.target.closest('button')) return;
        const article = event.target.closest('[data-pull-id]');
        if (!article) return;
        state.pullDrag = {
            pointerId: event.pointerId,
            pullId: article.dataset.pullId,
            startY: event.clientY,
            moved: false,
            targetPullId: article.dataset.pullId,
            placeAfter: false,
        };
        article.classList.add('is-dragging');
        els.pullList.setPointerCapture(event.pointerId);
    }

    function onPullPointerMove(event) {
        const drag = state.pullDrag;
        if (!drag || drag.pointerId !== event.pointerId) return;
        if (!drag.moved && Math.abs(event.clientY - drag.startY) < 4) return;
        drag.moved = true;
        const article = document.elementFromPoint(event.clientX, event.clientY)?.closest?.('[data-pull-id]');
        $$('.mdt-pull', els.pullList).forEach((row) => {
            row.classList.remove('is-drop-before', 'is-drop-after');
        });
        if (!article || article.dataset.pullId === drag.pullId) return;
        drag.targetPullId = article.dataset.pullId;
        drag.placeAfter = event.clientY > article.getBoundingClientRect().top + article.offsetHeight / 2;
        article.classList.add(drag.placeAfter ? 'is-drop-after' : 'is-drop-before');
        event.preventDefault();
    }

    function onPullPointerEnd(event, cancelled = false) {
        const drag = state.pullDrag;
        if (!drag || drag.pointerId !== event.pointerId) return;
        state.pullDrag = null;
        if (els.pullList.hasPointerCapture(event.pointerId)) {
            els.pullList.releasePointerCapture(event.pointerId);
        }
        clearPullDropIndicators();
        if (!cancelled && !drag.moved) {
            suppressNextPullClick();
            selectPull(drag.pullId);
            return;
        }
        if (!cancelled && drag.moved && drag.targetPullId !== drag.pullId) {
            suppressNextPullClick();
            reorderPull(drag.pullId, drag.targetPullId, drag.placeAfter);
        }
    }

    function eventToMapPoint(event) {
        const rect = els.mapContent.getBoundingClientRect();
        return {
            x: clamp(((event.clientX - rect.left) / rect.width) * 100, 0, 100),
            y: clamp(((event.clientY - rect.top) / rect.height) * 100, 0, 100),
        };
    }

    function eventToViewportPoint(event) {
        const rect = els.mapViewport.getBoundingClientRect();
        return {x: event.clientX - rect.left, y: event.clientY - rect.top};
    }

    function setSelectionRect(start, end) {
        const a = mapPointToViewport(start);
        const b = mapPointToViewport(end);
        els.selectionBox.hidden = false;
        els.selectionBox.style.left = `${Math.min(a.x, b.x)}px`;
        els.selectionBox.style.top = `${Math.min(a.y, b.y)}px`;
        els.selectionBox.style.width = `${Math.abs(a.x - b.x)}px`;
        els.selectionBox.style.height = `${Math.abs(a.y - b.y)}px`;
    }

    function mapPointToViewport(point) {
        const mapRect = els.mapContent.getBoundingClientRect();
        const viewportRect = els.mapViewport.getBoundingClientRect();
        return {
            x: mapRect.left - viewportRect.left + (point.x / 100) * mapRect.width,
            y: mapRect.top - viewportRect.top + (point.y / 100) * mapRect.height,
        };
    }

    function renderTempAnnotation(interaction) {
        $('#temp-annotation', els.annotationShapes)?.remove();
        if (!interaction || !['line', 'arrow', 'pencil'].includes(interaction.type)) return;
        const points = interaction.points.map((point) => `${point.x},${point.y}`).join(' ');
        const marker = interaction.type === 'arrow' ? ' marker-end="url(#annotation-arrow-head)"' : '';
        els.annotationShapes.insertAdjacentHTML(
            'beforeend',
            `<polyline id="temp-annotation" class="mdt-annotation-shape" points="${points}" stroke="${escapeHtml(interaction.color)}"${marker}></polyline>`,
        );
    }

    function editNote(annotationId) {
        const annotation = (state.route.annotations || []).find((item) => item.id === annotationId && item.type === 'note');
        if (!annotation) return;
        const text = window.prompt('编辑地图文字标注：', annotation.text || '');
        if (text === null) return;
        const nextText = text.trim().slice(0, 300);
        if (!nextText) return toast('文字标注不能为空。', true);
        if (nextText === annotation.text) return;
        mutateRoute((route) => {
            const target = route.annotations.find((item) => item.id === annotationId && item.type === 'note');
            if (target) target.text = nextText;
        });
    }

    function renderDraggedNote(annotationId, point) {
        const note = $(`[data-annotation-id="${annotationId}"]`, els.annotationNotes);
        if (!note) return;
        note.style.left = `${point.x}%`;
        note.style.top = `${point.y}%`;
    }

    function nearestAnnotation(point) {
        let best = null;
        let bestDistance = 4;
        for (const annotation of state.route.annotations || []) {
            if (annotation.floor_key !== state.floorKey) continue;
            const candidates = annotation.type === 'note'
                ? [{x: annotation.x, y: annotation.y}]
                : (annotation.points || []);
            for (const candidate of candidates) {
                const distance = Math.hypot(Number(candidate.x) - point.x, Number(candidate.y) - point.y);
                if (distance < bestDistance) {
                    best = annotation;
                    bestDistance = distance;
                }
            }
        }
        return best;
    }

    function shouldStartMapPan() {
        return state.tool === 'pan' || (state.tool === 'select' && state.zoom > 1);
    }

    function onMapPointerDown(event) {
        if (event.button !== 0) return;
        if (event.target.closest('.mdt-spawn')) return;
        const note = event.target.closest('.mdt-map-note');
        if (note) {
            const annotationId = note.dataset.annotationId;
            if (state.tool === 'note') {
                event.preventDefault();
                editNote(annotationId);
                return;
            }
            if (state.tool === 'select') {
                const start = eventToMapPoint(event);
                state.interaction = {
                    type: 'move-note',
                    annotationId,
                    start,
                    end: start,
                    moved: false,
                };
                els.mapViewport.setPointerCapture(event.pointerId);
                event.preventDefault();
                return;
            }
        }
        const mapPoint = eventToMapPoint(event);
        const viewportPoint = eventToViewportPoint(event);
        if (shouldStartMapPan()) {
            state.interaction = {
                type: 'pan',
                startClientX: event.clientX,
                startClientY: event.clientY,
                startPanX: state.panX,
                startPanY: state.panY,
            };
            els.mapViewport.classList.add('is-dragging');
        } else if (state.tool === 'box') {
            state.interaction = {type: 'box', start: mapPoint, end: mapPoint};
            setSelectionRect(mapPoint, mapPoint);
        } else if (['line', 'arrow', 'pencil'].includes(state.tool)) {
            state.interaction = {
                type: state.tool,
                points: [mapPoint, mapPoint],
                color: els.annotationColor.value,
            };
            renderTempAnnotation(state.interaction);
        } else if (state.tool === 'note') {
            const text = window.prompt('输入地图文字标注：');
            if (text?.trim()) {
                mutateRoute((route) => {
                    route.annotations.push({
                        id: randomId(),
                        type: 'note',
                        floor_key: state.floorKey,
                        x: mapPoint.x,
                        y: mapPoint.y,
                        text: text.trim().slice(0, 300),
                        color: els.annotationColor.value,
                    });
                });
            }
        } else if (state.tool === 'erase') {
            const target = nearestAnnotation(mapPoint);
            if (target) {
                mutateRoute((route) => {
                    route.annotations = route.annotations.filter((annotation) => annotation.id !== target.id);
                });
            }
        }
        if (state.interaction) {
            els.mapViewport.setPointerCapture(event.pointerId);
            event.preventDefault();
        }
        void viewportPoint;
    }

    function onMapPointerMove(event) {
        const interaction = state.interaction;
        if (!interaction) return;
        if (interaction.type === 'pan') {
            state.panX = interaction.startPanX + (event.clientX - interaction.startClientX);
            state.panY = interaction.startPanY + (event.clientY - interaction.startClientY);
            renderViewTransform();
            return;
        }
        const point = eventToMapPoint(event);
        if (interaction.type === 'move-note') {
            interaction.end = point;
            interaction.moved = interaction.moved || Math.hypot(point.x - interaction.start.x, point.y - interaction.start.y) > 0.1;
            renderDraggedNote(interaction.annotationId, point);
            return;
        }
        if (interaction.type === 'box') {
            interaction.end = point;
            setSelectionRect(interaction.start, interaction.end);
        } else if (interaction.type === 'pencil') {
            const last = interaction.points[interaction.points.length - 1];
            if (Math.hypot(last.x - point.x, last.y - point.y) > 0.25) {
                interaction.points.push(point);
                renderTempAnnotation(interaction);
            }
        } else if (interaction.type === 'line' || interaction.type === 'arrow') {
            interaction.points[1] = point;
            renderTempAnnotation(interaction);
        }
    }

    function onMapPointerUp(event) {
        const interaction = state.interaction;
        if (!interaction) return;
        state.interaction = null;
        els.mapViewport.classList.remove('is-dragging');
        if (els.mapViewport.hasPointerCapture(event.pointerId)) {
            els.mapViewport.releasePointerCapture(event.pointerId);
        }
        if (interaction.type === 'move-note') {
            if (!interaction.moved) return;
            mutateRoute((route) => {
                const note = route.annotations.find((annotation) => (
                    annotation.id === interaction.annotationId && annotation.type === 'note'
                ));
                if (note) {
                    note.x = interaction.end.x;
                    note.y = interaction.end.y;
                }
            });
        } else if (interaction.type === 'box') {
            els.selectionBox.hidden = true;
            selectBox(interaction.start, interaction.end);
        } else if (['line', 'arrow', 'pencil'].includes(interaction.type)) {
            $('#temp-annotation', els.annotationShapes)?.remove();
            const points = interaction.points;
            const first = points[0];
            const last = points[points.length - 1];
            if (points.length >= 2 && Math.hypot(first.x - last.x, first.y - last.y) > 0.35) {
                mutateRoute((route) => {
                    route.annotations.push({
                        id: randomId(),
                        type: interaction.type,
                        floor_key: state.floorKey,
                        points,
                        color: interaction.color,
                    });
                });
            } else {
                renderAnnotations();
            }
        }
    }

    function onMapDoubleClick(event) {
        const note = event.target.closest('.mdt-map-note');
        if (!note) return;
        event.preventDefault();
        editNote(note.dataset.annotationId);
    }

    function zoomBy(delta) {
        const nextZoom = clamp(state.zoom + delta, 0.55, 2.8);
        if (nextZoom === state.zoom) return;
        state.zoom = nextZoom;
        renderScaledMapLayers();
        renderViewTransform();
    }

    function resetView() {
        const zoomChanged = state.zoom !== 1;
        state.zoom = 1;
        state.panX = 0;
        state.panY = 0;
        if (zoomChanged) renderScaledMapLayers();
        renderViewTransform();
    }

    function clearAnnotations() {
        const count = state.route.annotations.filter((annotation) => annotation.floor_key === state.floorKey).length;
        if (!count) return;
        if (!window.confirm(`确认清空当前楼层的 ${count} 个标注？`)) return;
        mutateRoute((route) => {
            route.annotations = route.annotations.filter((annotation) => annotation.floor_key !== state.floorKey);
        });
    }

    function openModal({title, subtitle = '', content = '', actions = [], view = ''}) {
        els.modalTitle.textContent = title;
        els.modalSubtitle.textContent = subtitle;
        els.modalContent.innerHTML = content;
        els.modalActions.innerHTML = actions.map((action, index) => `
            <button type="button" data-modal-action="${index}" class="${action.primary ? 'is-primary' : ''}">
                ${escapeHtml(action.label)}
            </button>
        `).join('');
        state.modalAction = actions;
        state.modalView = view;
        els.modal.classList.toggle('is-route-library', view === 'route-library');
        els.modal.hidden = false;
        window.setTimeout(() => $('input, textarea, button', els.modalContent)?.focus(), 0);
    }

    function closeModal() {
        els.modal.hidden = true;
        state.modalAction = null;
        state.modalView = '';
        els.modal.classList.remove('is-route-library');
    }

    function renameCurrentRoute() {
        openModal({
            title: '重命名路线',
            subtitle: state.dungeon?.display_name || '',
            content: `<label>路线名称<input id="modal-route-name" maxlength="160" value="${escapeHtml(state.route.name)}"></label>`,
            actions: [
                {label: '取消', handler: closeModal},
                {
                    label: '保存名称',
                    primary: true,
                    handler: () => {
                        const name = $('#modal-route-name')?.value.trim();
                        if (!name) return toast('路线名称不能为空。', true);
                        mutateRoute((route) => { route.name = name.slice(0, 160); });
                        closeModal();
                    },
                },
            ],
        });
    }

    function newRoute() {
        persistRoute();
        state.route = defaultRoute(state.dungeon.key);
        state.route.current_pull_id = state.route.pulls[0].id;
        state.history = [];
        state.future = [];
        persistRoute();
        renderAll();
        toast('已新建空白路线。');
    }

    async function deleteRoute() {
        const old = state.route;
        if (!window.confirm(`确认删除本地路线“${old.name}”？服务器路线不会同时删除。`)) return;
        removeStoredRoute(old.local_id);
        const fallback = !loadStoredRoutes().length
            ? preferredDefaultRoute(state.dungeon.key)
            : null;
        if (fallback) {
            await applyDefaultRoute(fallback, {automatic: true});
            toast('本地路线已删除，已重新创建默认路线副本。');
            return;
        }
        state.route = defaultRoute(state.dungeon.key);
        state.route.current_pull_id = state.route.pulls[0].id;
        persistRoute();
        renderAll();
        toast('本地路线已删除。');
    }

    async function copyText(value, fallbackSelector) {
        try {
            await navigator.clipboard.writeText(value);
        } catch (_error) {
            const field = fallbackSelector ? $(fallbackSelector) : null;
            if (field) {
                field.select();
                document.execCommand('copy');
                return;
            }
            const temporaryField = document.createElement('textarea');
            temporaryField.value = value;
            temporaryField.style.position = 'fixed';
            temporaryField.style.opacity = '0';
            document.body.appendChild(temporaryField);
            temporaryField.select();
            document.execCommand('copy');
            temporaryField.remove();
        }
    }

    async function encodeCurrentRoute() {
        const data = await fetchJson('/portal/api/mythic-planner/share-code/', {
            method: 'POST',
            body: JSON.stringify({action: 'encode', route_data: routePayload()}),
        });
        return data.share_code;
    }

    async function showExportModal(mode = 'export', {shortUrl = '', shortError = '', code = ''} = {}) {
        const shareCode = code || await encodeCurrentRoute();
        const sharing = mode === 'share';
        openModal({
            title: sharing ? '分享路线' : '导出路线',
            subtitle: sharing ? 'MDT 路线字符串与站内短链接' : 'MythicDungeonTools 6.2.10 路线字符串',
            content: `
                <label>路线分享字符串
                    <textarea id="route-code" readonly>${escapeHtml(shareCode)}</textarea>
                </label>
                <p>使用暴雪 CBOR、Deflate 和 Base64 编码，可直接导入 MythicDungeonTools 6.2.10。</p>
                ${shortUrl ? `
                    <label>站内短链接
                        <input id="public-route-link" readonly value="${escapeHtml(shortUrl)}">
                    </label>
                    <p>打开链接后会导入当前浏览器成为可编辑副本；后续修改不会影响原分享快照。</p>
                ` : ''}
                ${shortError ? `<p class="mdt-share-error">短链接生成失败：${escapeHtml(shortError)}。字符串仍可正常分享。</p>` : ''}
            `,
            actions: [
                {label: '关闭', handler: closeModal},
                {
                    label: '复制字符串',
                    primary: !shortUrl,
                    handler: async () => {
                        await copyText(shareCode, '#route-code');
                        toast('路线字符串已复制。');
                    },
                },
                ...(shortUrl ? [{
                    label: '复制短链接',
                    primary: true,
                    handler: async () => {
                        await copyText(shortUrl, '#public-route-link');
                        toast('路线短链接已复制。');
                    },
                }] : []),
            ],
        });
    }

    function showImportModal() {
        openModal({
            title: '导入路线',
            subtitle: '粘贴 !~MDT2~ 开头的 MythicDungeonTools 字符串',
            content: `<label>路线分享字符串<textarea id="route-import-code" placeholder="!~MDT2~…"></textarea></label>`,
            actions: [
                {label: '取消', handler: closeModal},
                {
                    label: '校验并导入',
                    primary: true,
                    handler: async () => {
                        const code = $('#route-import-code')?.value.trim();
                        if (!code) return toast('请先粘贴路线分享字符串。', true);
                        try {
                            const data = await fetchJson('/portal/api/mythic-planner/share-code/', {
                                method: 'POST',
                                body: JSON.stringify({action: 'decode', share_code: code}),
                            });
                            await applyImportedPayload(data.route_data);
                            closeModal();
                            toast('路线导入成功。');
                        } catch (error) {
                            toast(error.message || '路线导入失败。', true);
                        }
                    },
                },
            ],
        });
    }

    async function applyImportedPayload(payload, routeMeta = {}) {
        if (!payload || Number(payload.version || 1) !== 1 || !payload.dungeon_key) {
            throw new Error('路线数据版本或地下城标识不正确。');
        }
        if (state.dungeon?.key !== payload.dungeon_key) {
            const exists = state.catalog?.dungeons?.some((dungeon) => dungeon.key === payload.dungeon_key);
            if (!exists) throw new Error('当前数据版本没有该路线所属地下城。');
            await loadDungeon(payload.dungeon_key, {
                restore: false,
                persist: false,
            });
        }
        const route = defaultRoute(payload.dungeon_key);
        route.local_id = routeMeta.local_id || randomId();
        route.source_share_key = (
            routeMeta.source_share_key
            || (
                routeMeta.local_id === state.route?.local_id
                    ? state.route?.source_share_key
                    : ''
            )
            || ''
        );
        route.source_default_route_id = routeMeta.source_default_route_id || '';
        route.source_default_route_revision = routeMeta.source_default_route_revision || '';
        route.name = payload.name || routeMeta.name || '导入路线';
        route.dungeon_level = Number(payload.dungeon_level || 10);
        route.pulls = Array.isArray(payload.pulls) ? payload.pulls : [];
        route.annotations = Array.isArray(payload.annotations) ? payload.annotations : [];
        route.current_pull_id = route.pulls[0]?.id || '';
        route.floor_key = payload.current_floor_key || state.dungeon.floors[0]?.key || '';
        state.route = normalizeRoute(route);
        state.floorKey = state.dungeon.floors.some((floor) => floor.key === route.floor_key)
            ? route.floor_key
            : state.dungeon.floors[0]?.key || '';
        state.history = [];
        state.future = [];
        persistRoute();
        renderAll();
    }

    async function applyDefaultRoute(defaultRouteData, {automatic = false} = {}) {
        const payload = clone(defaultRouteData?.route_data || {});
        payload.version = Number(payload.version || 1);
        payload.dungeon_key = defaultRouteData.dungeon_key;
        payload.name = defaultRouteData.name || payload.name || '默认路线副本';
        payload.dungeon_level = Number(
            defaultRouteData.dungeon_level || payload.dungeon_level || 10,
        );
        await applyImportedPayload(payload, {
            name: defaultRouteData.name,
            source_default_route_id: defaultRouteData.id,
            source_default_route_revision: defaultRouteData.revision,
        });
        setStatus(
            automatic
                ? `没有本地路线，已自动创建“${defaultRouteData.name}”的本地副本。`
                : `已从默认路线“${defaultRouteData.name}”创建本地副本。`,
        );
    }

    function routeLibraryStats(routeData) {
        const pulls = Array.isArray(routeData?.pulls) ? routeData.pulls : [];
        const selected = pulls.reduce(
            (sum, pull) => sum + (Array.isArray(pull?.spawn_uids) ? pull.spawn_uids.length : 0),
            0,
        );
        return `${pulls.length} 波 · ${selected} 个怪物`;
    }

    function routeLibraryTime(value) {
        if (!value) return '尚未记录更新时间';
        const dateOnly = String(value).match(/^(\d{4})-(\d{2})-(\d{2})$/);
        if (dateOnly) return `${dateOnly[1]}/${dateOnly[2]}/${dateOnly[3]}`;
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return '更新时间未知';
        return date.toLocaleDateString('zh-CN');
    }

    function routeLibraryCode(route) {
        return String(route?.route_code || '');
    }

    function routeLibraryNotePreview(route) {
        const note = String(route?.description || '暂无备注');
        return note.length > 16 ? `${note.slice(0, 16)}…` : note;
    }

    function routeLibraryDungeon() {
        return state.catalog?.dungeons?.find(
            (dungeon) => dungeon.key === state.routeLibraryDungeonKey,
        ) || null;
    }

    function routeLibraryFilteredRows(rows) {
        return state.routeLibraryDungeonKey
            ? rows.filter((route) => route.dungeon_key === state.routeLibraryDungeonKey)
            : rows;
    }

    function routeLibraryFilter() {
        const dungeons = Array.isArray(state.catalog?.dungeons) ? state.catalog.dungeons : [];
        return `
            <div class="mdt-library-filter">
                <div>
                    <strong>路线地图</strong>
                    <span>默认显示当前选中的地图</span>
                </div>
                <label for="route-library-dungeon-filter">
                    <span>显示地图</span>
                    <select id="route-library-dungeon-filter">
                        <option value="">全部地图</option>
                        ${dungeons.map((dungeon) => `
                            <option
                                value="${escapeHtml(dungeon.key)}"
                                ${dungeon.key === state.routeLibraryDungeonKey ? 'selected' : ''}
                            >${escapeHtml(dungeon.display_name)}</option>
                        `).join('')}
                    </select>
                </label>
            </div>
        `;
    }

    function localRouteRows() {
        const rows = routeLibraryFilteredRows(loadStoredRoutes());
        if (!rows.length) {
            const dungeon = routeLibraryDungeon();
            return `<div class="mdt-library-empty">${dungeon ? `${escapeHtml(dungeon.display_name)}还没有本地路线。` : '还没有保存到当前浏览器的路线。'}</div>`;
        }
        return `<div class="mdt-route-library">${rows.map((route) => {
            const dungeon = state.catalog?.dungeons?.find((item) => item.key === route.dungeon_key);
            return `
                <article class="mdt-library-row">
                    <div class="mdt-library-copy">
                        <div class="mdt-library-title">
                            <strong>${escapeHtml(route.name || '未命名路线')}</strong>
                            <span class="mdt-library-badge">本地路线</span>
                            ${route.source_default_route_id ? '<span class="mdt-library-badge is-default">默认路线副本</span>' : ''}
                        </div>
                        <span>${escapeHtml(dungeon?.display_name || route.dungeon_key)} · ${routeLibraryStats(route)}</span>
                        <span>更新于 ${routeLibraryTime(route.updated_at)}</span>
                    </div>
                    <div class="mdt-library-actions">
                        <button type="button" data-load-route="${escapeHtml(route.local_id)}">载入</button>
                    </div>
                </article>
            `;
        }).join('')}</div>`;
    }

    function defaultRouteRows() {
        const rows = routeLibraryFilteredRows(catalogDefaultRoutes());
        if (!rows.length) {
            const dungeon = routeLibraryDungeon();
            return `<div class="mdt-library-empty">${dungeon ? `${escapeHtml(dungeon.display_name)}还没有发布推荐路线。` : '当前数据版本还没有发布推荐路线。'}</div>`;
        }
        return `<div class="mdt-route-library">${rows.map((route) => {
            const routeCode = routeLibraryCode(route);
            const isInvalid = route.is_valid === false;
            const invalidReason = String(
                route.invalid_reason || '路线与当前 MDT 数据版本不兼容，请等待管理员更新。',
            );
            return `
            <article class="mdt-library-row mdt-library-row-default ${isInvalid ? 'is-invalid' : ''}">
                <div class="mdt-library-copy mdt-library-compact-name">
                    <div class="mdt-library-title">
                        <strong>${escapeHtml(route.name || '未命名默认路线')}</strong>
                        ${isInvalid
                            ? `<span class="mdt-library-badge is-invalid" title="${escapeHtml(invalidReason)}">已失效</span>`
                            : `
                                <span class="mdt-library-badge is-default">推荐路线</span>
                                ${route.is_featured ? '<span class="mdt-library-badge is-featured">首选</span>' : ''}
                                <span class="mdt-library-enabled ${route.is_active ? 'is-active' : ''}" title="是否启用：${route.is_active ? '启用' : '停用'}">${route.is_active ? '启用' : '停用'}</span>
                            `}
                    </div>
                    <span class="mdt-library-context ${isInvalid ? 'mdt-library-invalid-reason' : ''}" ${isInvalid ? `title="${escapeHtml(invalidReason)}"` : ''}>${isInvalid ? `失效原因：${escapeHtml(invalidReason)}` : `${escapeHtml(route.dungeon_name || route.dungeon_key)} · ${routeLibraryStats(route.route_data)}`}</span>
                </div>
                <div class="mdt-library-compact-value mdt-library-applicable-level"><span>适用层数</span><strong>${escapeHtml(route.applicable_level || `${Number(route.dungeon_level || 0)} 层`)}</strong></div>
                <div class="mdt-library-compact-value mdt-library-compact-time"><span>更新时间</span><time>${escapeHtml(routeLibraryTime(route.updated_at))}</time></div>
                <button
                    type="button"
                    class="mdt-library-note-trigger"
                    title="${escapeHtml(route.description || '暂无备注')}"
                    aria-label="备注：${escapeHtml(route.description || '暂无备注')}"
                ><span>备注</span><strong>${escapeHtml(routeLibraryNotePreview(route))}</strong></button>
                <div class="mdt-library-actions">
                    ${isInvalid
                        ? `<button type="button" disabled title="${escapeHtml(invalidReason)}">路线已失效</button>`
                        : `
                            <button type="button" data-copy-default-route="${Number(route.id)}" ${routeCode ? '' : 'disabled'}>复制字符串</button>
                            <button type="button" class="is-primary" data-load-default-route="${Number(route.id)}">创建本地副本</button>
                        `}
                </div>
            </article>
        `;
        }).join('')}</div>`;
    }

    function renderRouteLibraryModal() {
        if (state.modalView !== 'route-library') return;
        const selectedDungeon = routeLibraryDungeon();
        els.modalSubtitle.textContent = selectedDungeon
            ? `正在显示：${selectedDungeon.display_name}`
            : '推荐路线与当前浏览器路线';
        els.modalContent.innerHTML = `
            ${routeLibraryFilter()}
            <section class="mdt-library-section">
                <header>
                    <div>
                        <strong>推荐路线</strong>
                        <span>由管理员发布；载入后独立保存，修改不会影响推荐数据</span>
                    </div>
                </header>
                ${defaultRouteRows()}
            </section>
            <section class="mdt-library-section">
                <header>
                    <div>
                        <strong>当前浏览器</strong>
                        <span>自动保存，最多保留 100 条</span>
                    </div>
                </header>
                ${localRouteRows()}
            </section>
        `;
    }

    function showRouteLibrary() {
        state.routeLibraryDungeonKey = state.dungeon?.key || '';
        openModal({
            title: '路线库',
            subtitle: '推荐路线与当前浏览器路线',
            content: '',
            actions: [{label: '关闭', handler: closeModal}],
            view: 'route-library',
        });
        renderRouteLibraryModal();
    }

    function showHelp() {
        openModal({
            title: '操作说明',
            subtitle: '交互逻辑参考游戏内大秘境路线规划工具',
            content: `
                <div class="mdt-help">
                    <div><strong>选择怪物：</strong>左键怪物会按编队加入当前拉怪组；按住 <kbd>Ctrl</kbd> 左键只操作单个刷新点；右键打开怪物详情。</div>
                    <div><strong>拉怪组：</strong>右侧点击某一波设为当前组，可新增、改名、删除和调整顺序。</div>
                    <div><strong>地图操作：</strong>鼠标滚轮缩放；选择手掌工具拖动画布；框选工具可一次加入多个怪物。</div>
                    <div><strong>路线标注：</strong>支持自由笔、直线、箭头、文字与擦除；文字在选择工具下可直接拖动，双击即可二次编辑；标注按楼层保存。</div>
                    <div><strong>分享：</strong>可复制 MDT 6.2.10 使用的 <kbd>!~MDT2~</kbd> 暴雪编码路线字符串，也可生成无需登录的站内短链接；两者都包含拉怪组和地图标注。</div>
                    <div><strong>实时协作：</strong>开启后，同一浏览器的多个标签页会通过 BroadcastChannel 同步当前路线。</div>
                    <div><strong>快捷键：</strong><kbd>V</kbd> 选择、<kbd>H</kbd> 拖动、<kbd>B</kbd> 框选、<kbd>P</kbd> 画笔、<kbd>L</kbd> 直线、<kbd>A</kbd> 箭头、<kbd>N</kbd> 文字、<kbd>E</kbd> 擦除、<kbd>Ctrl+Z</kbd> 撤销。</div>
                    <div><strong>数据来源：</strong>副本地图、怪物、刷新点、编队、进度、技能 ID、特性和 POI 直接转换自 <a href="https://github.com/Nnoggie/MythicDungeonTools/tree/6.2.10" target="_blank" rel="noopener noreferrer">MythicDungeonTools 6.2.10</a>，按 GPLv2 保留来源。</div>
                    <div><strong>技能资料：</strong>技能名称、基础说明和图标来自固定客户端 build 的 <a href="https://wago.tools/" target="_blank" rel="noopener noreferrer">Wago DB2</a> 快照；完整中文数值说明由 <a href="https://www.wowhead.com/cn" target="_blank" rel="noopener noreferrer">Wowhead</a> 已渲染 Tooltip 补全，并记录来源 build。</div>
                </div>
            `,
            actions: [{label: '知道了', primary: true, handler: closeModal}],
        });
    }

    async function shareRoute() {
        const button = $('#share-route');
        const originalLabel = button.textContent;
        button.disabled = true;
        button.textContent = '生成中…';
        try {
            const data = await fetchJson('/portal/api/mythic-planner/share-links/', {
                method: 'POST',
                body: JSON.stringify({route_data: routePayload()}),
            });
            const shortUrl = `${location.origin}${data.short_path}`;
            await showExportModal('share', {shortUrl, code: data.share_code});
        } catch (error) {
            try {
                await showExportModal('share', {
                    shortError: error.message || '服务器暂时不可用',
                });
            } catch (encodeError) {
                toast(encodeError.message || '路线字符串生成失败。', true);
            }
        } finally {
            button.disabled = false;
            button.textContent = originalLabel;
        }
    }

    function setupBroadcast() {
        if (!('BroadcastChannel' in window)) {
            els.liveSync.disabled = true;
            els.liveSync.title = '当前浏览器不支持 BroadcastChannel。';
            return;
        }
        state.broadcast = new BroadcastChannel('lmonitor-mythic-planner-v1');
        state.broadcast.addEventListener('message', (event) => {
            const message = event.data || {};
            if (!state.live || message.origin === state.origin || message.type !== 'route') return;
            if (message.payload?.dungeon_key !== state.dungeon?.key) return;
            state.isApplyingRemote = true;
            applyImportedPayload(message.payload, {local_id: state.route.local_id})
                .then(() => {
                    setStatus('已接收同浏览器协作标签页的路线更新。');
                })
                .catch((error) => toast(error.message, true))
                .finally(() => { state.isApplyingRemote = false; });
        });
    }

    function toggleLive() {
        if (!state.broadcast) return;
        state.live = !state.live;
        els.liveSync.setAttribute('aria-pressed', state.live ? 'true' : 'false');
        toast(state.live ? '实时协作已开启；打开另一个同页面标签即可同步。' : '实时协作已关闭。');
        if (state.live) broadcastRoute();
    }

    function broadcastRoute() {
        if (!state.live || !state.broadcast || state.isApplyingRemote || !state.route) return;
        state.broadcast.postMessage({type: 'route', origin: state.origin, payload: routePayload()});
    }

    async function maybeLoadSharedRoute() {
        const shareRequest = sharedRouteRequest();
        if (!shareRequest) return;
        const {
            shareToken,
            legacyShareId,
            sourceKey,
        } = shareRequest;
        try {
            const storedRoute = loadStoredRoutes().find(
                (route) => route.source_share_key === sourceKey,
            );
            const storedDungeonExists = storedRoute && state.catalog?.dungeons?.some(
                (dungeon) => dungeon.key === storedRoute.dungeon_key,
            );
            if (storedDungeonExists) {
                await loadDungeon(storedRoute.dungeon_key, {route: storedRoute});
                replaceSharedRouteUrl(storedRoute.dungeon_key);
                toast(`已打开当前浏览器中的路线“${storedRoute.name}”。`);
                return;
            }
            const endpoint = shareToken
                ? `/portal/api/mythic-planner/share-links/${encodeURIComponent(shareToken)}/`
                : `/portal/api/mythic-planner/shared/${encodeURIComponent(legacyShareId)}/`;
            const data = await fetchJson(endpoint);
            await applyImportedPayload(data.route_data, {
                name: data.name,
                source_share_key: sourceKey,
            });
            replaceSharedRouteUrl(data.route_data.dungeon_key);
            toast(`已将分享路线“${data.name}”导入当前浏览器，可继续编辑。`);
        } catch (error) {
            toast(error.message, true);
        }
    }

    function onToolbarCommand(command) {
        if (command === 'undo') undo();
        else if (command === 'redo') redo();
        else if (command === 'zoom-in') zoomBy(0.15);
        else if (command === 'zoom-out') zoomBy(-0.15);
        else if (command === 'zoom-reset') resetView();
        else if (command === 'clear-annotations') clearAnnotations();
    }

    function bindElements() {
        Object.assign(els, {
            app: $('#planner-app'),
            datasetLabel: $('#dataset-label'),
            seasonSelect: $('#season-select'),
            dungeonSelect: $('#dungeon-select'),
            floorTabs: $('#floor-tabs'),
            routeNameButton: $('#route-name-button'),
            toolbar: $('.mdt-toolbar'),
            annotationColor: $('#annotation-color'),
            zoomOutput: $('#zoom-output'),
            mapViewport: $('#map-viewport'),
            mapContent: $('#map-content'),
            mapEmpty: $('#map-empty'),
            patrolLayer: $('#patrol-layer'),
            pullAreaLayer: $('#pull-area-layer'),
            poiLayer: $('#poi-layer'),
            spawnLayer: $('#spawn-layer'),
            annotationLayer: $('#annotation-layer'),
            annotationShapes: $('#annotation-shapes'),
            annotationNotes: $('#annotation-notes'),
            selectionBox: $('#selection-box'),
            mapDungeonName: $('#map-dungeon-name'),
            mapFloorName: $('#map-floor-name'),
            mapCursorInfo: $('#map-cursor-info'),
            level: $('#dungeon-level'),
            levelOutput: $('#dungeon-level-output'),
            progressBar: $('#forces-progress-bar'),
            progressText: $('#forces-progress-text'),
            selectedEnemyCount: $('#selected-enemy-count'),
            routeWarning: $('#route-warning'),
            pullList: $('#pull-list'),
            enemyDetailModal: $('#enemy-detail-modal'),
            enemyDetailTitle: $('#enemy-detail-title'),
            enemyDetail: $('#enemy-detail'),
            liveSync: $('#live-sync'),
            modal: $('#planner-modal'),
            modalTitle: $('#modal-title'),
            modalSubtitle: $('#modal-subtitle'),
            modalContent: $('#modal-content'),
            modalActions: $('#modal-actions'),
            toast: $('#planner-toast'),
            statusMessage: $('#status-message'),
        });
    }

    function bindEvents() {
        els.seasonSelect.addEventListener('change', async () => {
            persistRoute();
            state.selectionGroupKey = els.seasonSelect.value;
            const firstDungeon = renderCatalogSelectors();
            if (firstDungeon) await loadDungeon(firstDungeon.key, {restore: true});
        });
        els.dungeonSelect.addEventListener('change', async () => {
            persistRoute();
            await loadDungeon(els.dungeonSelect.value, {restore: true});
        });
        els.floorTabs.addEventListener('click', (event) => {
            const button = event.target.closest('[data-floor-key]');
            if (!button) return;
            state.floorKey = button.dataset.floorKey;
            state.route.floor_key = state.floorKey;
            closeEnemyDetail();
            persistRoute();
            renderAll();
        });
        els.toolbar.addEventListener('click', (event) => {
            const tool = event.target.closest('[data-tool]')?.dataset.tool;
            const command = event.target.closest('[data-command]')?.dataset.command;
            if (tool) setTool(tool);
            if (command) onToolbarCommand(command);
        });
        els.spawnLayer.addEventListener('click', (event) => {
            const button = event.target.closest('[data-spawn-uid]');
            if (!button || state.tool !== 'select') return;
            toggleSpawn(button.dataset.spawnUid, {individual: event.ctrlKey || event.metaKey});
        });
        els.spawnLayer.addEventListener('contextmenu', (event) => {
            const button = event.target.closest('[data-spawn-uid]');
            if (!button) return;
            event.preventDefault();
            event.stopPropagation();
            openEnemyDetail(button.dataset.spawnUid);
        });
        els.mapViewport.addEventListener('pointerdown', onMapPointerDown);
        els.mapViewport.addEventListener('pointermove', onMapPointerMove);
        els.mapViewport.addEventListener('pointerup', onMapPointerUp);
        els.mapViewport.addEventListener('pointercancel', onMapPointerUp);
        els.mapViewport.addEventListener('dblclick', onMapDoubleClick);
        els.mapViewport.addEventListener('wheel', (event) => {
            event.preventDefault();
            zoomBy(event.deltaY < 0 ? 0.1 : -0.1);
        }, {passive: false});
        els.pullList.addEventListener('click', (event) => {
            if (state.suppressPullClick) {
                state.suppressPullClick = false;
                event.preventDefault();
                return;
            }
            const article = event.target.closest('[data-pull-id]');
            if (!article) return;
            const action = event.target.closest('[data-pull-action]')?.dataset.pullAction;
            if (action) {
                event.stopPropagation();
                handlePullAction(article.dataset.pullId, action);
                return;
            }
            selectPull(article.dataset.pullId);
        });
        els.pullList.addEventListener('keydown', (event) => {
            if (!['Enter', ' '].includes(event.key) || event.target.closest('button')) return;
            const article = event.target.closest('[data-pull-id]');
            if (!article) return;
            event.preventDefault();
            selectPull(article.dataset.pullId);
        });
        els.pullList.addEventListener('pointerdown', onPullPointerDown);
        els.pullList.addEventListener('pointermove', onPullPointerMove);
        els.pullList.addEventListener('pointerup', (event) => onPullPointerEnd(event));
        els.pullList.addEventListener('pointercancel', (event) => onPullPointerEnd(event, true));
        $('#add-pull').addEventListener('click', addPull);
        els.level.addEventListener('input', () => {
            state.route.dungeon_level = Number(els.level.value);
            els.levelOutput.textContent = els.level.value;
            renderPulls();
            if (!els.enemyDetailModal.hidden) renderEnemyDetail();
        });
        els.level.addEventListener('change', () => {
            persistRoute();
            broadcastRoute();
        });
        $('#new-route').addEventListener('click', newRoute);
        $('#rename-route').addEventListener('click', renameCurrentRoute);
        els.routeNameButton.addEventListener('click', renameCurrentRoute);
        $('#delete-route').addEventListener('click', deleteRoute);
        $('#export-route').addEventListener('click', async () => {
            try {
                await showExportModal('export');
            } catch (error) {
                toast(error.message || '路线字符串生成失败。', true);
            }
        });
        $('#share-route').addEventListener('click', shareRoute);
        $('#import-route').addEventListener('click', showImportModal);
        $('#open-route-library').addEventListener('click', showRouteLibrary);
        $('#open-help').addEventListener('click', showHelp);
        els.liveSync.addEventListener('click', toggleLive);
        els.enemyDetailModal.addEventListener('click', (event) => {
            if (event.target.closest('[data-close-enemy-detail]')) closeEnemyDetail();
        });
        els.modal.addEventListener('click', async (event) => {
            if (event.target.closest('[data-close-modal]')) {
                closeModal();
                return;
            }
            const button = event.target.closest('[data-modal-action]');
            if (button && state.modalAction) {
                const action = state.modalAction[Number(button.dataset.modalAction)];
                if (action?.handler) await action.handler();
            }
            const loadButton = event.target.closest('[data-load-route]');
            if (loadButton) {
                const route = loadStoredRoutes().find((row) => row.local_id === loadButton.dataset.loadRoute);
                if (route) {
                    await loadDungeon(route.dungeon_key, {route});
                    closeModal();
                    toast(`已载入路线“${route.name}”。`);
                }
                return;
            }
            const defaultButton = event.target.closest('[data-load-default-route]');
            if (defaultButton) {
                const route = catalogDefaultRoutes().find(
                    (row) => Number(row.id) === Number(defaultButton.dataset.loadDefaultRoute),
                );
                if (route?.is_valid === false) {
                    toast(`推荐路线“${route.name}”已失效：${route.invalid_reason || '请等待管理员更新。'}`, true);
                    return;
                }
                if (route) {
                    await applyDefaultRoute(route);
                    closeModal();
                    toast(`已创建默认路线“${route.name}”的本地副本。`);
                }
                return;
            }
            const copyDefaultButton = event.target.closest('[data-copy-default-route]');
            if (copyDefaultButton) {
                const route = catalogDefaultRoutes().find(
                    (row) => Number(row.id) === Number(copyDefaultButton.dataset.copyDefaultRoute),
                );
                if (route?.is_valid === false) {
                    toast(`推荐路线“${route.name}”已失效：${route.invalid_reason || '请等待管理员更新。'}`, true);
                    return;
                }
                const routeCode = routeLibraryCode(route);
                if (routeCode) {
                    await copyText(routeCode);
                    toast(`推荐路线“${route.name}”的字符串已复制。`);
                }
            }
        });
        els.modal.addEventListener('change', (event) => {
            const dungeonFilter = event.target.closest('#route-library-dungeon-filter');
            if (!dungeonFilter) return;
            state.routeLibraryDungeonKey = dungeonFilter.value;
            renderRouteLibraryModal();
        });
        window.addEventListener('keydown', (event) => {
            if (event.key === 'Escape' && !els.enemyDetailModal.hidden) {
                event.preventDefault();
                closeEnemyDetail();
                return;
            }
            if (!els.modal.hidden || /INPUT|TEXTAREA|SELECT/.test(event.target.tagName)) return;
            if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') {
                event.preventDefault();
                if (event.shiftKey) redo();
                else undo();
                return;
            }
            if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'y') {
                event.preventDefault();
                redo();
                return;
            }
            const shortcuts = {v: 'select', h: 'pan', b: 'box', p: 'pencil', l: 'line', a: 'arrow', n: 'note', e: 'erase'};
            const tool = shortcuts[event.key.toLowerCase()];
            if (tool) setTool(tool);
            if (event.key === 'Escape') {
                state.interaction = null;
                els.selectionBox.hidden = true;
                renderAnnotations();
            }
        });
        window.addEventListener('resize', renderViewTransform);
        window.addEventListener('beforeunload', persistRoute);
    }

    function init() {
        bindElements();
        bindEvents();
        setupBroadcast();
        renderToolState();
        loadCatalog();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init, {once: true});
    } else {
        init();
    }
})();
