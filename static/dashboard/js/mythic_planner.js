(() => {
    'use strict';

    const $ = (selector, root = document) => root.querySelector(selector);
    const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
    const escapeHtml = (value) => String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');

    const RESOURCE_CONFIG = {
        versions: {
            title: '数据版本',
            singular: '数据版本',
            description: '管理可切换的数据包版本，任意时刻只有一个版本生效。',
            columns: [
                ['key', '版本 key'],
                ['label', '版本名称'],
                ['game_version', '游戏版本'],
                ['season', '赛季'],
                ['source_name', '数据来源'],
                ['is_active', '状态'],
            ],
            fields: [
                {key: 'key', label: '版本 key', required: true, help: '稳定标识，建议使用小写字母、数字和连字符。'},
                {key: 'label', label: '版本名称', required: true},
                {key: 'game_version', label: '游戏版本'},
                {key: 'season', label: '赛季'},
                {key: 'schema_version', label: '数据结构版本', type: 'number', default: 1, min: 1},
                {key: 'source_name', label: '数据来源'},
                {key: 'source_reference', label: '来源引用'},
                {key: 'is_active', label: '设为当前生效版本', type: 'checkbox'},
                {key: 'notes', label: '版本备注', type: 'textarea', wide: true},
                {key: 'metadata', label: '扩展元数据（JSON）', type: 'json', wide: true, default: {}},
            ],
        },
        configs: {
            title: '运行配置',
            singular: '运行配置',
            description: '配置默认地下城、层数范围、按编队选择、实时协作和公开分享能力。',
            columns: [
                ['key', '配置 key'],
                ['default_dungeon_key', '默认地下城'],
                ['default_dungeon_level', '默认层数'],
                ['level_range', '层数范围'],
                ['live_sync_enabled', '实时协作'],
                ['allow_public_route_share', '公开分享'],
            ],
            fields: [
                {key: 'key', label: '配置 key', required: true, default: 'default'},
                {key: 'default_dungeon_key', label: '默认地下城', type: 'dungeon-key'},
                {key: 'default_dungeon_level', label: '默认层数', type: 'number', default: 10, min: 2},
                {key: 'min_dungeon_level', label: '最小层数', type: 'number', default: 2, min: 2},
                {key: 'max_dungeon_level', label: '最大层数', type: 'number', default: 35, min: 2},
                {key: 'group_selection_default', label: '默认按怪物编队整组选中', type: 'checkbox', default: true},
                {key: 'live_sync_enabled', label: '允许同浏览器实时协作', type: 'checkbox', default: true},
                {key: 'allow_public_route_share', label: '允许账号路线公开分享', type: 'checkbox', default: true},
                {key: 'settings', label: '其他设置（JSON）', type: 'json', wide: true, default: {}},
            ],
        },
        default_routes: {
            title: '推荐路线',
            singular: '推荐路线',
            description: '维护向所有玩家发布的推荐路线；玩家载入后会创建本地副本，后续修改不会回写这里。',
            columns: [
                ['name', '路线名'],
                ['applicable_level', '适用层数'],
                ['display_updated_on', '更新时间'],
                ['description', '备注'],
                ['is_active', '是否启用'],
                ['route_code', '字符串'],
            ],
            fields: [
                {key: 'name', label: '路线名', required: true},
                {
                    key: 'applicable_level',
                    label: '适用层数',
                    type: 'select',
                    required: true,
                    options: [
                        ['顶层', '顶层'],
                        ['中高层', '中高层'],
                        ['割草', '割草'],
                        ['集合石平推', '集合石平推'],
                    ],
                },
                {key: 'display_updated_on', label: '更新时间', type: 'date', default: 'today', required: true},
                {key: 'is_active', label: '是否启用', type: 'checkbox', default: true},
                {key: 'description', label: '备注', type: 'textarea', wide: true},
                {key: 'route_code', label: '字符串', type: 'textarea', wide: true, required: true, help: '粘贴规划器导出的 !~MDT2~ 路线字符串；系统会自动识别所属地下城并校验路线内容。'},
            ],
        },
        selection_groups: {
            title: '赛季分类',
            singular: '赛季分类',
            description: '独立维护赛季或自定义地下城分类，可自由新增第三赛季并设置前台顺序。',
            columns: [
                ['key', '分类 key'],
                ['display_name', '分类名称'],
                ['version_label', '数据版本'],
                ['order', '显示顺序'],
                ['is_active', '状态'],
            ],
            fields: [
                {key: 'data_version_id', label: '数据版本', type: 'version', required: true},
                {key: 'key', label: '分类 key', required: true, help: '同一数据版本内唯一，例如 midnight-season-3。'},
                {key: 'name', label: '英文名称', required: true},
                {key: 'name_zh', label: '中文名称'},
                {key: 'order', label: '显示顺序', type: 'number', default: 0, min: 0},
                {key: 'is_active', label: '启用分类', type: 'checkbox', default: true},
                {key: 'metadata', label: '扩展元数据（JSON）', type: 'json', wide: true, default: {}},
            ],
        },
        selection_memberships: {
            title: '地下城分配',
            singular: '地下城分配',
            description: '把同一数据版本的地下城分配到赛季分类，并维护分类内显示顺序。',
            columns: [
                ['selection_group_name', '赛季分类'],
                ['dungeon_name', '地下城'],
                ['version_label', '数据版本'],
                ['order', '分类内顺序'],
                ['is_active', '状态'],
            ],
            fields: [
                {key: 'selection_group_id', label: '赛季分类', type: 'selection-group', required: true},
                {key: 'dungeon_id', label: '地下城', type: 'dungeon', required: true},
                {key: 'order', label: '分类内顺序', type: 'number', default: 0, min: 0},
                {key: 'is_active', label: '启用分配', type: 'checkbox', default: true},
                {key: 'metadata', label: '扩展元数据（JSON）', type: 'json', wide: true, default: {}},
            ],
        },
        dungeons: {
            title: '地下城',
            singular: '地下城',
            description: '维护地下城基础信息、总敌方部队进度和排序。',
            columns: [
                ['key', '地下城 key'],
                ['display_name', '名称'],
                ['version_label', '数据版本'],
                ['short_name', '简称'],
                ['total_enemy_forces', '总进度'],
                ['is_active', '状态'],
            ],
            fields: [
                {key: 'data_version_id', label: '数据版本', type: 'version', required: true},
                {key: 'key', label: '地下城 key', required: true},
                {key: 'name', label: '英文名称', required: true},
                {key: 'name_zh', label: '中文名称'},
                {key: 'short_name', label: '简称'},
                {key: 'external_index', label: '外部索引', type: 'number'},
                {key: 'map_id', label: '游戏地图 ID', type: 'number'},
                {key: 'total_enemy_forces', label: '总敌方部队进度', type: 'number', default: 0, min: 0},
                {key: 'order', label: '显示顺序', type: 'number', default: 0, min: 0},
                {key: 'is_active', label: '启用地下城', type: 'checkbox', default: true},
                {key: 'metadata', label: '扩展元数据（JSON）', type: 'json', wide: true, default: {}},
            ],
        },
        floors: {
            title: '楼层 / 地图',
            singular: '楼层',
            description: '配置地图尺寸、背景 URL、底色和楼层顺序；背景留空时使用内置风格底图。',
            columns: [
                ['key', '楼层 key'],
                ['display_name', '名称'],
                ['dungeon_name', '地下城'],
                ['floor_index', '楼层序号'],
                ['map_size', '地图尺寸'],
                ['is_active', '状态'],
            ],
            fields: [
                {key: 'dungeon_id', label: '地下城', type: 'dungeon', required: true},
                {key: 'key', label: '楼层 key', required: true},
                {key: 'floor_index', label: '楼层序号', type: 'number', default: 1, min: 1},
                {key: 'name', label: '英文名称', required: true},
                {key: 'name_zh', label: '中文名称'},
                {key: 'background_url', label: '地图背景 URL', wide: true},
                {key: 'background_color', label: '无贴图时背景色', type: 'color', default: '#66533f'},
                {key: 'map_width', label: '地图宽度', type: 'number', default: 1000, min: 100},
                {key: 'map_height', label: '地图高度', type: 'number', default: 700, min: 100},
                {key: 'order', label: '显示顺序', type: 'number', default: 0, min: 0},
                {key: 'is_active', label: '启用楼层', type: 'checkbox', default: true},
                {key: 'metadata', label: '扩展元数据（JSON）', type: 'json', wide: true, default: {}},
            ],
        },
        enemies: {
            title: '怪物原型',
            singular: '怪物',
            description: '维护怪物进度、基础生命、控制特性和地图标记外观。',
            columns: [
                ['key', '怪物 key'],
                ['display_name', '名称'],
                ['dungeon_name', '地下城'],
                ['npc_id', 'NPC ID'],
                ['enemy_forces', '进度'],
                ['is_boss', '类型'],
                ['is_active', '状态'],
            ],
            fields: [
                {key: 'dungeon_id', label: '地下城', type: 'dungeon', required: true},
                {key: 'key', label: '怪物 key', required: true},
                {key: 'npc_id', label: 'NPC ID', type: 'number'},
                {key: 'name', label: '英文名称', required: true},
                {key: 'name_zh', label: '中文名称'},
                {key: 'enemy_forces', label: '单只进度', type: 'number', default: 0, min: 0},
                {key: 'base_health', label: '基础生命值', type: 'number', default: 0, min: 0},
                {key: 'level', label: '怪物等级', type: 'number', default: 0, min: 0},
                {key: 'creature_type', label: '生物类型'},
                {key: 'icon_url', label: '图标 URL', wide: true},
                {key: 'marker_color', label: '地图标记颜色', type: 'color', default: '#94a3b8'},
                {key: 'is_boss', label: '首领怪物', type: 'checkbox'},
                {key: 'is_active', label: '启用怪物', type: 'checkbox', default: true},
                {key: 'traits', label: '控制特性（JSON）', type: 'json', wide: true, default: {taunt: true, stun: true, interrupt: true, root: true, slow: true}},
                {key: 'metadata', label: '扩展元数据（JSON）', type: 'json', wide: true, default: {}},
            ],
        },
        spells: {
            title: '技能资料库',
            singular: '技能资料',
            description: '按 MDT 数据版本维护可复用的双语技能名称、说明、来源 build 和图标。',
            columns: [
                ['spell_id', '法术 ID'],
                ['display_name', '技能名称'],
                ['version_label', '数据版本'],
                ['source_branch', '数据分支'],
                ['snapshot_build', '客户端 build'],
                ['description_source', '说明来源'],
                ['description_quality', '说明质量'],
                ['icon_file_data_id', '图标文件 ID'],
                ['is_active', '状态'],
            ],
            fields: [
                {key: 'data_version_id', label: '数据版本', type: 'version', required: true},
                {key: 'spell_id', label: '法术 ID', type: 'number', required: true, min: 1},
                {key: 'source_branch', label: '数据分支', help: '例如 wowt、wow 或 wowxptr。'},
                {key: 'source_locale', label: '主语言', default: 'zhCN'},
                {key: 'snapshot_build', label: '客户端 build'},
                {key: 'name', label: '英文名称'},
                {key: 'name_zh', label: '中文名称'},
                {key: 'description', label: '英文说明', type: 'textarea', wide: true},
                {key: 'description_zh', label: '中文说明', type: 'textarea', wide: true},
                {key: 'aura_description', label: '英文光环说明', type: 'textarea', wide: true},
                {key: 'aura_description_zh', label: '中文光环说明', type: 'textarea', wide: true},
                {key: 'icon_file_data_id', label: '图标 FileDataID', type: 'number', min: 1},
                {key: 'icon_name', label: '图标名称'},
                {key: 'icon_url', label: '图标 URL', wide: true},
                {key: 'is_active', label: '启用技能资料', type: 'checkbox', default: true},
                {key: 'metadata', label: '扩展元数据（JSON）', type: 'json', wide: true, default: {}},
            ],
        },
        abilities: {
            title: '怪物技能',
            singular: '怪物技能',
            description: '维护技能名称、说明、打断、驱散类型和危险度。',
            columns: [
                ['spell_id', '法术 ID'],
                ['display_name', '技能名称'],
                ['enemy_name', '所属怪物'],
                ['interruptible', '打断'],
                ['dispel_type', '驱散'],
                ['danger_level', '危险度'],
                ['is_active', '状态'],
            ],
            fields: [
                {key: 'enemy_id', label: '所属怪物', type: 'enemy', required: true},
                {key: 'spell_id', label: '法术 ID', type: 'number', required: true, min: 1},
                {key: 'name', label: '英文名称', required: true},
                {key: 'name_zh', label: '中文名称'},
                {key: 'description', label: '英文说明', type: 'textarea', wide: true},
                {key: 'description_zh', label: '中文说明', type: 'textarea', wide: true},
                {key: 'icon_url', label: '图标 URL', wide: true},
                {key: 'interruptible', label: '可打断', type: 'checkbox'},
                {key: 'dispel_type', label: '驱散 / 应对标签', help: '允许组合，例如：魔法、激怒。'},
                {key: 'danger_level', label: '危险度', type: 'select', default: 1, options: [[1, '1 · 一般'], [2, '2 · 重要'], [3, '3 · 致命']]},
                {key: 'order', label: '显示顺序', type: 'number', default: 0, min: 0},
                {key: 'is_active', label: '启用技能', type: 'checkbox', default: true},
                {key: 'metadata', label: '扩展元数据（JSON）', type: 'json', wide: true, default: {}},
            ],
        },
        spawns: {
            title: '怪物刷新点',
            singular: '刷新点',
            description: '维护怪物坐标、楼层、编队、缩放和巡逻路径。',
            columns: [
                ['key', '刷新点 key'],
                ['enemy_name', '怪物'],
                ['floor_name', '楼层'],
                ['position', '坐标'],
                ['position_source', '坐标来源'],
                ['group_key', '编队'],
                ['is_active', '状态'],
            ],
            fields: [
                {key: 'enemy_id', label: '怪物', type: 'enemy', required: true},
                {key: 'floor_id', label: '楼层', type: 'floor', required: true},
                {key: 'key', label: '刷新点 key', required: true},
                {key: 'x', label: '横向坐标（0–100）', type: 'number', default: 50, min: 0, max: 100, step: 0.1},
                {key: 'y', label: '纵向坐标（0–100）', type: 'number', default: 50, min: 0, max: 100, step: 0.1},
                {key: 'group_key', label: '怪物编队 key'},
                {key: 'scale', label: '图标缩放', type: 'number', default: 1, min: 0.25, max: 5, step: 0.05},
                {key: 'is_active', label: '启用刷新点', type: 'checkbox', default: true},
                {key: 'patrol', label: '巡逻路径（JSON 点数组）', type: 'json', wide: true, default: []},
                {key: 'metadata', label: '扩展元数据（JSON）', type: 'json', wide: true, default: {}},
            ],
        },
        pois: {
            title: '地图兴趣点',
            singular: '兴趣点',
            description: '维护入口、出口、传送点、首领和交互设施。',
            columns: [
                ['key', '兴趣点 key'],
                ['label', '显示名称'],
                ['floor_name', '楼层'],
                ['poi_type', '类型'],
                ['position', '坐标'],
                ['is_active', '状态'],
            ],
            fields: [
                {key: 'floor_id', label: '楼层', type: 'floor', required: true},
                {key: 'key', label: '兴趣点 key', required: true},
                {key: 'poi_type', label: '兴趣点类型', type: 'select', default: 'note', options: [['entrance', '入口'], ['exit', '出口'], ['portal', '楼层传送'], ['boss', '首领'], ['utility', '交互设施'], ['note', '说明']]},
                {key: 'x', label: '横向坐标（0–100）', type: 'number', default: 50, min: 0, max: 100, step: 0.1},
                {key: 'y', label: '纵向坐标（0–100）', type: 'number', default: 50, min: 0, max: 100, step: 0.1},
                {key: 'label', label: '显示名称'},
                {key: 'icon_url', label: '图标 URL', wide: true},
                {key: 'target_floor_key', label: '目标楼层 key'},
                {key: 'is_active', label: '启用兴趣点', type: 'checkbox', default: true},
                {key: 'metadata', label: '扩展元数据（JSON）', type: 'json', wide: true, default: {}},
            ],
        },
    };

    const CONFIG_RESOURCE_DEPENDENCIES = {
        versions: ['versions'],
        configs: ['versions', 'dungeons', 'configs'],
        default_routes: ['versions', 'dungeons', 'default_routes'],
        selection_groups: ['versions', 'selection_groups'],
        selection_memberships: [
            'versions',
            'dungeons',
            'selection_groups',
            'selection_memberships',
        ],
        dungeons: ['versions', 'dungeons'],
        floors: ['versions', 'dungeons', 'floors'],
        enemies: ['versions', 'dungeons', 'enemies'],
        spells: ['versions', 'spells'],
        abilities: ['versions', 'dungeons', 'enemies', 'spells', 'abilities'],
        spawns: ['versions', 'dungeons', 'floors', 'enemies', 'spawns'],
        pois: ['versions', 'dungeons', 'floors', 'pois'],
    };

    const requestedResource = new URLSearchParams(window.location.search).get('resource');
    const state = {
        snapshot: null,
        resource: (
            requestedResource
            && Object.prototype.hasOwnProperty.call(RESOURCE_CONFIG, requestedResource)
        ) ? requestedResource : 'versions',
        editingId: null,
        routeDetail: null,
        toastTimer: null,
        routePreflightTimer: null,
        routePreflightSequence: 0,
        loadedResources: new Set(),
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
        if (!response.ok || payload.success === false) throw new Error(payload.message || `请求失败（${response.status}）。`);
        return payload;
    }

    function snapshotResources(resource = state.resource) {
        return Array.from(new Set([
            ...(CONFIG_RESOURCE_DEPENDENCIES[resource] || [resource]),
            'counts',
        ]));
    }

    function mergeSnapshot(snapshot, resources) {
        if (!state.snapshot) state.snapshot = {};
        resources
            .filter((resource) => resource !== 'counts')
            .forEach((resource) => {
                state.snapshot[resource] = snapshot[resource] || [];
                state.loadedResources.add(resource);
            });
        state.snapshot.counts = {
            ...(state.snapshot.counts || {}),
            ...(snapshot.counts || {}),
        };
        state.loadedResources.add('counts');
    }

    async function loadSnapshot({quiet = false, resources = snapshotResources()} = {}) {
        if (!quiet) els.tableBody.innerHTML = '<tr><td class="mp-admin-loading">正在加载数据…</td></tr>';
        try {
            const payload = await request(
                `/api/mythic-planner/manage/?resources=${resources.join(',')}`,
            );
            mergeSnapshot(payload.data, resources);
            renderAll();
            if (!quiet) toast('大秘境规划器数据已刷新。');
        } catch (error) {
            els.tableBody.innerHTML = `<tr><td class="mp-admin-empty">${escapeHtml(error.message)}</td></tr>`;
            toast(error.message, true);
        }
    }

    function activeVersion() {
        return state.snapshot?.versions?.find((row) => row.is_active) || null;
    }

    function rowBy(resource, id) {
        return state.snapshot?.[resource]?.find((row) => Number(row.id) === Number(id)) || null;
    }

    function displayName(row) {
        return row?.name_zh || row?.name || row?.label || row?.key || '—';
    }

    function dungeonForRow(resource, row) {
        if (resource === 'dungeons') return row;
        if (resource === 'routes' || resource === 'default_routes') return rowBy('dungeons', row.dungeon_id);
        if (resource === 'selection_memberships') return rowBy('dungeons', row.dungeon_id);
        if (resource === 'floors' || resource === 'enemies') return rowBy('dungeons', row.dungeon_id);
        if (resource === 'abilities') return rowBy('dungeons', rowBy('enemies', row.enemy_id)?.dungeon_id);
        if (resource === 'spawns') return rowBy('dungeons', rowBy('enemies', row.enemy_id)?.dungeon_id);
        if (resource === 'pois') return rowBy('dungeons', rowBy('floors', row.floor_id)?.dungeon_id);
        return null;
    }

    function versionForRow(resource, row) {
        if (resource === 'versions') return row;
        if (resource === 'selection_groups') return rowBy('versions', row.data_version_id);
        if (resource === 'selection_memberships') return rowBy('versions', row.data_version_id);
        if (resource === 'spells') return rowBy('versions', row.data_version_id);
        if (resource === 'dungeons') return rowBy('versions', row.data_version_id);
        return rowBy('versions', dungeonForRow(resource, row)?.data_version_id);
    }

    function renderAll() {
        renderSidebar();
        renderFilters();
        renderResource();
    }

    function renderSidebar() {
        const active = activeVersion();
        els.activeVersionLabel.textContent = active?.label || '尚未初始化';
        els.activeVersionMeta.textContent = active
            ? [active.game_version, active.season].filter(Boolean).join(' · ') || `版本 key：${active.key}`
            : '运行初始化命令或导入数据包';
        Object.keys(RESOURCE_CONFIG).forEach((resource) => {
            const count = (
                state.snapshot?.counts?.[resource]
                ?? state.snapshot?.[resource]?.length
                ?? 0
            );
            const counter = $(`#count-${resource}`);
            if (counter) counter.textContent = String(count);
        });
        $$('[data-resource]', els.resourceNav).forEach((button) => {
            button.classList.toggle('is-active', button.dataset.resource === state.resource);
        });
    }

    function renderFilters() {
        const selectedVersion = els.versionFilter.value;
        const selectedDungeon = els.dungeonFilter.value;
        els.versionFilter.innerHTML = '<option value="">全部版本</option>' + (state.snapshot?.versions || [])
            .map((row) => `<option value="${row.id}">${escapeHtml(row.label)}${row.is_active ? '（生效）' : ''}</option>`)
            .join('');
        if ((state.snapshot?.versions || []).some((row) => String(row.id) === selectedVersion)) {
            els.versionFilter.value = selectedVersion;
        }
        const versionId = els.versionFilter.value;
        const dungeons = (state.snapshot?.dungeons || []).filter((row) => !versionId || String(row.data_version_id) === versionId);
        els.dungeonFilter.innerHTML = '<option value="">全部地下城</option>' + dungeons
            .map((row) => `<option value="${row.id}">${escapeHtml(displayName(row))}</option>`)
            .join('');
        if (dungeons.some((row) => String(row.id) === selectedDungeon)) {
            els.dungeonFilter.value = selectedDungeon;
        }
        const hideFilters = ['versions', 'configs'].includes(state.resource);
        els.versionFilter.closest('label').hidden = hideFilters;
        els.dungeonFilter.closest('label').hidden = hideFilters || ['spells', 'selection_groups'].includes(state.resource);
        els.search.placeholder = '按名称、key、ID 搜索';
    }

    function filteredRows() {
        let rows = [...(state.snapshot?.[state.resource] || [])];
        const versionId = Number(els.versionFilter.value || 0);
        const dungeonId = Number(els.dungeonFilter.value || 0);
        const search = els.search.value.trim().toLowerCase();
        const showInactive = els.showInactive.checked;
        if (versionId && !['versions', 'configs'].includes(state.resource)) {
            rows = rows.filter((row) => Number(versionForRow(state.resource, row)?.id) === versionId);
        }
        if (dungeonId && !['versions', 'configs', 'spells', 'selection_groups'].includes(state.resource)) {
            rows = rows.filter((row) => Number(dungeonForRow(state.resource, row)?.id) === dungeonId);
        }
        if (!showInactive) rows = rows.filter((row) => row.is_active !== false);
        if (search) {
            rows = rows.filter((row) => {
                const relatedRows = [
                    dungeonForRow(state.resource, row),
                    versionForRow(state.resource, row),
                ];
                if (state.resource === 'spawns') {
                    relatedRows.push(
                        rowBy('enemies', row.enemy_id),
                        rowBy('floors', row.floor_id),
                    );
                }
                if (state.resource === 'abilities') {
                    relatedRows.push(rowBy('enemies', row.enemy_id));
                }
                return [row, ...relatedRows]
                    .filter(Boolean)
                    .some(
                        (item) => JSON.stringify(item)
                            .toLowerCase()
                            .includes(search),
                    );
            });
        }
        return rows;
    }

    function renderResource() {
        const config = RESOURCE_CONFIG[state.resource];
        els.resourceTitle.textContent = config.title;
        els.resourceDescription.textContent = config.description;
        els.addResource.textContent = `＋ 新增${config.singular}`;
        els.addResource.hidden = Boolean(config.readOnly);
        if (state.resource === 'configs' && (state.snapshot?.configs || []).length) {
            els.addResource.textContent = '编辑默认配置';
        }
        const rows = filteredRows();
        const activeRows = (state.snapshot?.[state.resource] || []).filter((row) => row.is_active !== false).length;
        const summaryItems = [
            ['当前显示', rows.length],
            ['全部记录', state.snapshot?.[state.resource]?.length || 0],
            ['启用记录', activeRows],
        ];
        if (state.resource === 'routes') {
            const allRoutes = state.snapshot?.routes || [];
            summaryItems.push(
                ['公开路线', allRoutes.filter((row) => row.is_public && row.is_active).length],
                ['账号数量', new Set(allRoutes.filter((row) => row.owner_user_id != null).map((row) => row.owner_user_id)).size],
            );
        }
        els.summary.innerHTML = summaryItems
            .map(([label, value]) => `<span class="mp-admin-chip">${label}<strong>${value}</strong></span>`)
            .join('');
        els.tableHead.innerHTML = `<tr>${config.columns.map(([, label]) => `<th>${escapeHtml(label)}</th>`).join('')}<th>操作</th></tr>`;
        if (!rows.length) {
            els.tableBody.innerHTML = `<tr><td colspan="${config.columns.length + 1}" class="mp-admin-empty">没有符合当前筛选条件的数据。</td></tr>`;
            return;
        }
        els.tableBody.innerHTML = rows.map((row) => `
            <tr>
                ${config.columns.map(([key]) => `<td>${formatCell(key, row)}</td>`).join('')}
                <td>
                    ${state.resource === 'routes' ? renderRouteActions(row) : `
                        <div class="mp-admin-row-actions">
                            <button type="button" data-edit-id="${row.id}">编辑</button>
                            ${Object.hasOwn(row, 'is_active') && row.is_active !== false ? `<button type="button" data-archive-id="${row.id}" class="is-danger">停用</button>` : ''}
                        </div>
                    `}
                </td>
            </tr>
        `).join('');
    }

    function renderRouteActions(row) {
        return `
            <div class="mp-admin-row-actions mp-admin-route-actions">
                <button type="button" data-route-detail="${row.id}">详情</button>
                <button type="button" data-route-public="${row.id}">${row.is_public ? '设为私有' : '公开分享'}</button>
                <button type="button" data-route-active="${row.id}" class="${row.is_active ? 'is-danger' : 'is-success'}">${row.is_active ? '停用' : '恢复'}</button>
            </div>
        `;
    }

    function formatCell(key, row) {
        if (key === 'route_name') {
            return `<div class="mp-admin-name"><strong>${escapeHtml(row.name || '未命名路线')}</strong><span>#${row.id} · ${escapeHtml(String(row.share_id || '').slice(0, 8))}</span></div>`;
        }
        if (key === 'owner_name') {
            const label = row.owner_display_name || row.owner_username || `已删除账号 #${row.owner_user_id || '—'}`;
            const meta = row.owner_email || (row.owner_exists ? `用户 ID：${row.owner_user_id}` : '账号记录已不存在');
            return `<div class="mp-admin-name"><strong>${escapeHtml(label)}</strong><span>${escapeHtml(meta)}</span></div>`;
        }
        if (key === 'route_stats') return `${Number(row.pull_count || 0)} 波 · ${Number(row.spawn_count || 0)} 个怪 · ${Number(row.annotation_count || 0)} 条标注`;
        if (key === 'route_code') {
            if (state.resource === 'default_routes' && row.is_valid === false) {
                const reason = row.invalid_reason || '路线与当前 MDT 数据版本不兼容。';
                return `<span class="mp-admin-status is-invalid" title="${escapeHtml(reason)}">已失效</span>`;
            }
            const code = String(row.route_code || '');
            if (!code) return '—';
            const preview = code.length > 30 ? `${code.slice(0, 30)}…` : code;
            return `<code class="mp-admin-route-code">${escapeHtml(preview)}<span>${code.length} 字符</span></code>`;
        }
        if (key === 'description' && state.resource === 'default_routes') {
            const description = String(row.description || '');
            return description
                ? `<span class="mp-admin-route-note">${escapeHtml(description)}</span>`
                : '—';
        }
        if (key === 'is_featured') return `<span class="mp-admin-status ${row.is_featured ? 'is-public' : ''}">${row.is_featured ? '首选' : '普通'}</span>`;
        if (key === 'share_status') return `<span class="mp-admin-status ${row.is_public ? 'is-public' : ''}">${row.is_public ? '公开' : '私有'}</span>`;
        if (key === 'updated_at' || key === 'created_at') return formatDateTime(row[key]);
        if (key === 'display_name') {
            return `<div class="mp-admin-name"><strong>${escapeHtml(displayName(row))}</strong><span>${escapeHtml(row.name || '')}</span></div>`;
        }
        if (key === 'version_label') return escapeHtml(rowBy('versions', row.data_version_id)?.label || '—');
        if (key === 'selection_group_name') return escapeHtml(displayName(rowBy('selection_groups', row.selection_group_id)));
        if (key === 'dungeon_name') return escapeHtml(displayName(rowBy('dungeons', row.dungeon_id)));
        if (key === 'enemy_name') return escapeHtml(displayName(rowBy('enemies', row.enemy_id)));
        if (key === 'floor_name') return escapeHtml(displayName(rowBy('floors', row.floor_id)));
        if (key === 'map_size') return `${Number(row.map_width || 0)} × ${Number(row.map_height || 0)}`;
        if (key === 'position') return `${Number(row.x || 0).toFixed(1)}, ${Number(row.y || 0).toFixed(1)}`;
        if (key === 'position_source') {
            return row.is_position_manual
                ? '<span class="mp-admin-status is-manual">人工锁定</span>'
                : '<span class="mp-admin-status is-active">上游导入</span>';
        }
        if (key === 'level_range') return `${row.min_dungeon_level} – ${row.max_dungeon_level}`;
        if (key === 'is_active') {
            if (state.resource === 'default_routes' && row.is_valid === false) {
                const reason = row.invalid_reason || '路线与当前 MDT 数据版本不兼容。';
                return `<span class="mp-admin-status is-invalid" title="${escapeHtml(reason)}">已失效</span>`;
            }
            return `<span class="mp-admin-status ${row.is_active ? 'is-active' : ''}">${row.is_active ? '启用' : '停用'}</span>`;
        }
        if (key === 'is_boss') return row.is_boss ? '首领' : '普通怪物';
        if (key === 'interruptible') return row.interruptible ? '可打断' : '不可打断';
        if (key === 'live_sync_enabled' || key === 'allow_public_route_share') return row[key] ? '已开启' : '已关闭';
        if (key === 'danger_level') return `${'★'.repeat(Number(row.danger_level || 1))}${'☆'.repeat(3 - Number(row.danger_level || 1))}`;
        if (key === 'key') return `<code class="mp-admin-key">${escapeHtml(row.key)}</code>`;
        const value = row[key];
        if (value === null || value === undefined || value === '') return '—';
        if (typeof value === 'boolean') return value ? '是' : '否';
        return escapeHtml(value);
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

    function relationOptions(type) {
        if (type === 'version') return (state.snapshot?.versions || []).map((row) => [row.id, `${row.label}${row.is_active ? '（生效）' : ''}`]);
        if (type === 'selection-group') {
            const versionId = Number(els.versionFilter.value || 0);
            return (state.snapshot?.selection_groups || [])
                .filter((row) => row.is_active && (!versionId || row.data_version_id === versionId))
                .map((row) => [
                    row.id,
                    `${displayName(row)} / ${rowBy('versions', row.data_version_id)?.label || '未知版本'}`,
                ]);
        }
        if (type === 'dungeon') {
            const versionId = Number(els.versionFilter.value || 0);
            return (state.snapshot?.dungeons || [])
                .filter((row) => !versionId || row.data_version_id === versionId)
                .map((row) => [row.id, displayName(row)]);
        }
        if (type === 'dungeon-key') return (state.snapshot?.dungeons || []).filter((row) => row.is_active).map((row) => [row.key, displayName(row)]);
        if (type === 'floor') {
            const dungeonId = Number(els.dungeonFilter.value || 0);
            return (state.snapshot?.floors || [])
                .filter((row) => !dungeonId || row.dungeon_id === dungeonId)
                .map((row) => [row.id, `${displayName(rowBy('dungeons', row.dungeon_id))} / ${displayName(row)}`]);
        }
        if (type === 'enemy') {
            const dungeonId = Number(els.dungeonFilter.value || 0);
            return (state.snapshot?.enemies || [])
                .filter((row) => !dungeonId || row.dungeon_id === dungeonId)
                .map((row) => [row.id, `${displayName(rowBy('dungeons', row.dungeon_id))} / ${displayName(row)}`]);
        }
        return [];
    }

    function defaultFieldValue(field) {
        if (field.key === 'data_version_id' && els.versionFilter.value) return Number(els.versionFilter.value);
        if (field.key === 'selection_group_id' && els.versionFilter.value) {
            return state.snapshot?.selection_groups?.find(
                (row) => row.data_version_id === Number(els.versionFilter.value) && row.is_active,
            )?.id || '';
        }
        if (field.key === 'dungeon_id' && els.dungeonFilter.value) return Number(els.dungeonFilter.value);
        if (field.key === 'floor_id' && els.dungeonFilter.value) {
            return state.snapshot?.floors?.find((row) => row.dungeon_id === Number(els.dungeonFilter.value))?.id || '';
        }
        if (field.key === 'enemy_id' && els.dungeonFilter.value) {
            return state.snapshot?.enemies?.find((row) => row.dungeon_id === Number(els.dungeonFilter.value))?.id || '';
        }
        if (field.type === 'date' && field.default === 'today') {
            const now = new Date();
            const localDate = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
            return localDate.toISOString().slice(0, 10);
        }
        return field.default ?? '';
    }

    function renderField(field, row) {
        const value = row && Object.hasOwn(row, field.key) ? row[field.key] : defaultFieldValue(field);
        const classes = field.wide ? 'is-wide' : '';
        const required = field.required ? ' required' : '';
        const help = field.help ? `<small>${escapeHtml(field.help)}</small>` : '';
        if (field.type === 'readonly') {
            const text = value ? formatDateTime(value) : '保存后自动生成';
            return `<label class="${classes}"><span>${escapeHtml(field.label)}</span><input name="${field.key}" type="text" value="${escapeHtml(text)}" readonly aria-readonly="true"></label>`;
        }
        if (field.type === 'checkbox') {
            return `<label class="${classes} mp-admin-checkbox"><input name="${field.key}" type="checkbox" ${value ? 'checked' : ''}><span>${escapeHtml(field.label)}</span></label>`;
        }
        const relationTypes = new Set(['version', 'selection-group', 'dungeon', 'dungeon-key', 'floor', 'enemy']);
        if (relationTypes.has(field.type) || field.type === 'select') {
            const options = field.options || relationOptions(field.type);
            return `
                <label class="${classes}">
                    <span>${escapeHtml(field.label)}</span>
                    <select name="${field.key}"${required}>
                        ${field.required ? '<option value="">请选择</option>' : '<option value="">未设置</option>'}
                        ${options.map(([optionValue, label]) => `<option value="${escapeHtml(optionValue)}" ${String(optionValue) === String(value) ? 'selected' : ''}>${escapeHtml(label)}</option>`).join('')}
                    </select>
                    ${help}
                </label>
            `;
        }
        if (field.type === 'textarea' || field.type === 'json') {
            const text = field.type === 'json' ? JSON.stringify(value ?? field.default ?? {}, null, 2) : String(value || '');
            return `<label class="${classes}"><span>${escapeHtml(field.label)}</span><textarea name="${field.key}"${required}>${escapeHtml(text)}</textarea>${help}</label>`;
        }
        const type = field.type === 'color'
            ? 'color'
            : field.type === 'number'
                ? 'number'
                : field.type === 'date'
                    ? 'date'
                    : 'text';
        const attrs = [
            field.min !== undefined ? `min="${field.min}"` : '',
            field.max !== undefined ? `max="${field.max}"` : '',
            field.step !== undefined ? `step="${field.step}"` : '',
        ].filter(Boolean).join(' ');
        return `<label class="${classes}"><span>${escapeHtml(field.label)}</span><input name="${field.key}" type="${type}" value="${escapeHtml(value)}"${required} ${attrs}>${help}</label>`;
    }

    function renderDefaultRoutePreflight(status, payload = null, message = '') {
        const host = $('#default-route-preflight', els.editorFields);
        if (!host) return;
        host.className = `mp-route-preflight is-${status}`;
        if (status === 'checking') {
            host.innerHTML = '<span class="mp-route-preflight-badge">预检中</span><p>正在解析路线字符串并匹配副本…</p>';
            return;
        }
        if (status === 'valid' && payload) {
            const versionText = payload.version_changed
                ? `来源版本 ${payload.source_data_version_key} · 已兼容匹配 ${payload.data_version.key}`
                : `${payload.data_version.label || payload.data_version.key}`;
            host.innerHTML = `
                <span class="mp-route-preflight-badge">预检通过</span>
                <div>
                    <strong>${escapeHtml(payload.dungeon.display_name || payload.dungeon.name || payload.dungeon.key)}</strong>
                    <p>${escapeHtml(versionText)}</p>
                </div>
                <dl>
                    <div><dt>钥匙层数</dt><dd>+${Number(payload.dungeon_level || 0)}</dd></div>
                    <div><dt>拉怪波次</dt><dd>${Number(payload.pull_count || 0)}</dd></div>
                    <div><dt>怪物点位</dt><dd>${Number(payload.spawn_count || 0)}</dd></div>
                    <div><dt>地图标注</dt><dd>${Number(payload.annotation_count || 0)}</dd></div>
                </dl>
            `;
            return;
        }
        if (status === 'invalid') {
            host.innerHTML = `<span class="mp-route-preflight-badge">预检失败</span><p>${escapeHtml(message || '路线字符串无法使用。')}</p>`;
            return;
        }
        host.innerHTML = '<span class="mp-route-preflight-badge">等待预检</span><p>输入路线字符串后，将自动识别对应副本并校验怪物点位。</p>';
    }

    async function preflightDefaultRoute() {
        if (state.resource !== 'default_routes') return true;
        window.clearTimeout(state.routePreflightTimer);
        const routeCodeInput = els.resourceForm.elements.namedItem('route_code');
        const routeCode = String(routeCodeInput?.value || '').trim();
        const sequence = ++state.routePreflightSequence;
        if (!routeCode) {
            renderDefaultRoutePreflight('idle');
            return false;
        }
        renderDefaultRoutePreflight('checking');
        try {
            const response = await request('/api/mythic-planner/manage/', {
                method: 'POST',
                body: JSON.stringify({
                    resource: 'default_route_preflight',
                    data: {route_code: routeCode},
                }),
            });
            if (sequence !== state.routePreflightSequence) return false;
            renderDefaultRoutePreflight('valid', response.data);
            return true;
        } catch (error) {
            if (sequence !== state.routePreflightSequence) return false;
            renderDefaultRoutePreflight('invalid', null, error.message);
            return false;
        }
    }

    function scheduleDefaultRoutePreflight() {
        window.clearTimeout(state.routePreflightTimer);
        state.routePreflightTimer = window.setTimeout(
            preflightDefaultRoute,
            420,
        );
    }

    function openEditor(id = null) {
        const config = RESOURCE_CONFIG[state.resource];
        let row = id ? rowBy(state.resource, id) : null;
        if (state.resource === 'configs' && !row && state.snapshot.configs.length) row = state.snapshot.configs[0];
        state.editingId = row?.id || null;
        els.editorTitle.textContent = `${row ? '编辑' : '新增'}${config.singular}`;
        els.editorSubtitle.textContent = row ? `记录 ID：${row.id}` : '保存后立即写入数据库';
        els.editorFields.innerHTML = config.fields.map((field) => renderField(field, row)).join('');
        if (state.resource === 'default_routes') {
            const routeCodeInput = els.resourceForm.elements.namedItem('route_code');
            routeCodeInput?.closest('label')?.insertAdjacentHTML(
                'afterend',
                '<section id="default-route-preflight" class="mp-route-preflight is-idle" aria-live="polite"></section>',
            );
            routeCodeInput?.addEventListener('input', scheduleDefaultRoutePreflight);
            if (String(routeCodeInput?.value || '').trim()) {
                preflightDefaultRoute();
            } else {
                renderDefaultRoutePreflight('idle');
            }
        }
        els.editorModal.hidden = false;
    }

    function closeEditor() {
        window.clearTimeout(state.routePreflightTimer);
        state.routePreflightSequence += 1;
        els.editorModal.hidden = true;
        state.editingId = null;
    }

    async function saveEditor(event) {
        event.preventDefault();
        const config = RESOURCE_CONFIG[state.resource];
        if (state.resource === 'default_routes') {
            const preflightPassed = await preflightDefaultRoute();
            if (!preflightPassed) {
                toast('路线字符串预检未通过，请修正后再保存。', true);
                return;
            }
        }
        const data = {};
        for (const field of config.fields) {
            if (field.type === 'readonly') continue;
            const input = els.resourceForm.elements.namedItem(field.key);
            if (!input) continue;
            if (field.type === 'checkbox') data[field.key] = input.checked;
            else data[field.key] = input.value;
        }
        try {
            const url = state.editingId
                ? `/api/mythic-planner/manage/${state.editingId}/`
                : '/api/mythic-planner/manage/';
            const payload = await request(url, {
                method: state.editingId ? 'PATCH' : 'POST',
                body: JSON.stringify({
                    resource: state.resource,
                    snapshot_resources: snapshotResources(),
                    data,
                }),
            });
            mergeSnapshot(payload.snapshot, snapshotResources());
            closeEditor();
            renderAll();
            toast(`${config.singular}已保存。`);
        } catch (error) {
            toast(error.message, true);
        }
    }

    async function archiveRow(id) {
        const row = rowBy(state.resource, id);
        const config = RESOURCE_CONFIG[state.resource];
        if (!row || !window.confirm(`确认停用${config.singular}“${displayName(row)}”？该操作不会物理删除数据。`)) return;
        try {
            const payload = await request(`/api/mythic-planner/manage/${id}/`, {
                method: 'DELETE',
                body: JSON.stringify({
                    resource: state.resource,
                    snapshot_resources: snapshotResources(),
                }),
            });
            mergeSnapshot(payload.snapshot, snapshotResources());
            renderAll();
            toast(`${config.singular}已停用。`);
        } catch (error) {
            toast(error.message, true);
        }
    }

    function routePublicLink(route) {
        if (!route?.share_id) return '';
        return `${location.origin}/portal/mythic-planner/?share=${encodeURIComponent(route.share_id)}`;
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

    function renderRouteDetail(route) {
        state.routeDetail = route;
        const ownerLabel = route.owner_display_name || route.owner_username || `已删除账号 #${route.owner_user_id || '—'}`;
        const shareLink = routePublicLink(route);
        els.routeDetailTitle.textContent = route.name || '未命名路线';
        els.routeDetailSubtitle.textContent = `路线 ID：${route.id} · 修订版本 ${route.revision}`;
        els.routeDetailBody.innerHTML = `
            <section class="mp-route-detail-hero">
                <div>
                    <span class="mp-admin-status ${route.is_active ? 'is-active' : ''}">${route.is_active ? '正常' : '已停用'}</span>
                    <span class="mp-admin-status ${route.is_public ? 'is-public' : ''}">${route.is_public ? '公开分享' : '私有路线'}</span>
                </div>
                <strong>${escapeHtml(route.name || '未命名路线')}</strong>
                <p>${escapeHtml(route.dungeon_name)} · ${Number(route.dungeon_level || 0)} 层</p>
            </section>
            <dl class="mp-route-detail-grid">
                <div><dt>所属账号</dt><dd>${escapeHtml(ownerLabel)}</dd></div>
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
                <header><strong>导入分享字符串</strong><span>可用于规划器导入排查</span></header>
                <textarea readonly spellcheck="false">${escapeHtml(route.share_code || '')}</textarea>
                <button type="button" data-copy-route-code>复制分享字符串</button>
            </section>
            <details class="mp-route-detail-json">
                <summary>查看原始路线数据（JSON）</summary>
                <pre>${escapeHtml(JSON.stringify(route.route_data || {}, null, 2))}</pre>
            </details>
        `;
        els.routeDetailActions.innerHTML = `
            <button type="button" data-close-route-detail>关闭</button>
            <span class="mp-admin-spacer"></span>
            <button type="button" data-detail-route-public="${route.id}">${route.is_public ? '设为私有' : '开启公开分享'}</button>
            <button type="button" data-detail-route-active="${route.id}" class="${route.is_active ? 'is-danger' : 'is-success'}">${route.is_active ? '停用路线' : '恢复路线'}</button>
        `;
    }

    async function openRouteDetail(id) {
        state.routeDetail = null;
        els.routeDetailTitle.textContent = '路线详情';
        els.routeDetailSubtitle.textContent = `正在读取路线 ID：${id}`;
        els.routeDetailBody.innerHTML = '<div class="mp-route-detail-loading">正在加载路线详情…</div>';
        els.routeDetailActions.innerHTML = '<button type="button" data-close-route-detail>关闭</button>';
        els.routeDetailModal.hidden = false;
        try {
            const payload = await request(`/api/mythic-planner/manage/${id}/?resource=routes`);
            renderRouteDetail(payload.data);
        } catch (error) {
            els.routeDetailBody.innerHTML = `<div class="mp-route-detail-loading is-error">${escapeHtml(error.message)}</div>`;
            toast(error.message, true);
        }
    }

    function closeRouteDetail() {
        els.routeDetailModal.hidden = true;
        state.routeDetail = null;
    }

    async function updateRouteState(id, data, successMessage, refreshDetail = false) {
        try {
            const payload = await request(`/api/mythic-planner/manage/${id}/`, {
                method: 'PATCH',
                body: JSON.stringify({resource: 'routes', data}),
            });
            state.snapshot = payload.snapshot;
            renderAll();
            toast(successMessage);
            if (refreshDetail && !els.routeDetailModal.hidden) {
                await openRouteDetail(id);
            }
        } catch (error) {
            toast(error.message, true);
        }
    }

    function toggleRoutePublic(id, refreshDetail = false) {
        const route = rowBy('routes', id);
        if (!route) return;
        const nextValue = !route.is_public;
        const action = nextValue ? '公开分享' : '设为私有';
        if (!window.confirm(`确认将路线“${route.name}”${action}？`)) return;
        updateRouteState(
            id,
            {is_public: nextValue},
            nextValue ? '路线已开启公开分享。' : '路线已设为私有。',
            refreshDetail,
        );
    }

    function toggleRouteActive(id, refreshDetail = false) {
        const route = rowBy('routes', id);
        if (!route) return;
        const nextValue = !route.is_active;
        const action = nextValue ? '恢复' : '停用';
        if (!window.confirm(`确认${action}路线“${route.name}”？${nextValue ? '' : '停用后公开链接将立即失效。'}`)) return;
        updateRouteState(
            id,
            {is_active: nextValue},
            nextValue ? '路线已恢复。' : '路线已停用。',
            refreshDetail,
        );
    }

    function openImport() {
        els.importModal.hidden = false;
        window.setTimeout(() => els.importJson.focus(), 0);
    }

    function closeImport() {
        els.importModal.hidden = true;
    }

    async function submitImport(event) {
        event.preventDefault();
        const text = els.importJson.value.trim();
        if (!text) return toast('请粘贴数据包 JSON。', true);
        let parsed;
        try {
            parsed = JSON.parse(text);
        } catch (error) {
            return toast(`JSON 格式错误：${error.message}`, true);
        }
        try {
            const payload = await request('/api/mythic-planner/manage/', {
                method: 'POST',
                body: JSON.stringify({
                    resource: 'import',
                    snapshot_resources: snapshotResources(),
                    data: {
                        payload: parsed,
                        activate: els.importActivate.checked,
                        replace: els.importReplace.checked,
                    },
                }),
            });
            mergeSnapshot(payload.snapshot, snapshotResources());
            closeImport();
            renderAll();
            toast(`数据包 ${payload.data.version_key} 导入完成：新增 ${payload.data.created}，更新 ${payload.data.updated}。`);
        } catch (error) {
            toast(error.message, true);
        }
    }

    function loadJsonTemplate() {
        const template = {
            schema_version: 1,
            data_version: {
                key: 'season-version-key',
                label: '数据版本名称',
                game_version: '游戏版本',
                season: '赛季',
                source_name: '数据来源',
                source_reference: '',
                notes: '',
                metadata: {},
            },
            selection_groups: [{
                key: 'season-group-key',
                name: 'Season Group Name',
                name_zh: '第三赛季',
                order: 1,
                dungeon_keys: ['dungeon-key'],
                metadata: {},
            }],
            dungeons: [{
                key: 'dungeon-key',
                name: 'Dungeon Name',
                name_zh: '地下城名称',
                short_name: '简称',
                total_enemy_forces: 100,
                order: 1,
                metadata: {},
                floors: [{
                    key: 'floor-1',
                    floor_index: 1,
                    name: 'Floor 1',
                    name_zh: '第一层',
                    background_url: '',
                    background_color: '#66533f',
                    map_width: 1000,
                    map_height: 700,
                    order: 1,
                    metadata: {},
                    pois: [],
                }],
                enemies: [{
                    key: 'enemy-key',
                    npc_id: 100001,
                    name: 'Enemy Name',
                    name_zh: '怪物名称',
                    enemy_forces: 5,
                    base_health: 500000,
                    level: 82,
                    creature_type: '人型生物',
                    marker_color: '#94a3b8',
                    is_boss: false,
                    traits: {taunt: true, stun: true, interrupt: true, root: true, slow: true},
                    abilities: [{
                        spell_id: 1000001,
                        name: 'Ability Name',
                        name_zh: '技能名称',
                        description_zh: '技能说明',
                        interruptible: true,
                        dispel_type: '',
                        danger_level: 2,
                    }],
                    spawns: [{
                        key: 'enemy-01',
                        floor_key: 'floor-1',
                        x: 50,
                        y: 50,
                        group_key: 'group-a',
                        scale: 1,
                        patrol: [],
                    }],
                }],
            }],
        };
        els.importJson.value = JSON.stringify(template, null, 2);
        toast('已载入最小数据包模板。');
    }

    function bindElements() {
        Object.assign(els, {
            activeVersionLabel: $('#active-version-label'),
            activeVersionMeta: $('#active-version-meta'),
            resourceNav: $('#resource-nav'),
            resourceTitle: $('#resource-title'),
            resourceDescription: $('#resource-description'),
            addResource: $('#add-resource'),
            versionFilter: $('#version-filter'),
            dungeonFilter: $('#dungeon-filter'),
            search: $('#resource-search'),
            showInactive: $('#show-inactive'),
            summary: $('#resource-summary'),
            tableHead: $('#resource-table-head'),
            tableBody: $('#resource-table-body'),
            editorModal: $('#editor-modal'),
            editorTitle: $('#editor-title'),
            editorSubtitle: $('#editor-subtitle'),
            editorFields: $('#editor-fields'),
            resourceForm: $('#resource-form'),
            importModal: $('#import-modal'),
            importForm: $('#import-form'),
            importJson: $('#import-json'),
            importActivate: $('#import-activate'),
            importReplace: $('#import-replace'),
            toast: $('#mythic-planner-config-toast'),
        });
    }

    function bindEvents() {
        els.resourceNav.addEventListener('click', (event) => {
            const button = event.target.closest('[data-resource]');
            if (!button) return;
            state.resource = button.dataset.resource;
            const url = new URL(window.location.href);
            url.searchParams.set('resource', state.resource);
            window.history.replaceState({}, '', url);
            const resources = snapshotResources();
            if (resources.every((resource) => state.loadedResources.has(resource))) {
                renderAll();
            } else {
                loadSnapshot({quiet: true, resources});
            }
        });
        els.addResource.addEventListener('click', () => openEditor());
        els.versionFilter.addEventListener('change', () => { renderFilters(); renderResource(); });
        els.dungeonFilter.addEventListener('change', renderResource);
        els.search.addEventListener('input', renderResource);
        els.showInactive.addEventListener('change', renderResource);
        els.tableBody.addEventListener('click', (event) => {
            const edit = event.target.closest('[data-edit-id]');
            const archive = event.target.closest('[data-archive-id]');
            if (edit) openEditor(Number(edit.dataset.editId));
            if (archive) archiveRow(Number(archive.dataset.archiveId));
        });
        els.resourceForm.addEventListener('submit', saveEditor);
        els.editorModal.addEventListener('click', (event) => {
            if (event.target.closest('[data-close-editor]')) closeEditor();
        });
        $('#refresh-data').addEventListener('click', () => loadSnapshot());
        $('#import-package').addEventListener('click', openImport);
        els.importModal.addEventListener('click', (event) => {
            if (event.target.closest('[data-close-import]')) closeImport();
        });
        els.importForm.addEventListener('submit', submitImport);
        $('#load-demo-json').addEventListener('click', loadJsonTemplate);
        document.addEventListener('dashboard-section-changed', (event) => {
            if (event.detail?.section !== 'mythic-planner-config') return;
            const resource = String(event.detail?.mythicResource || 'versions');
            if (resource === state.resource) return;
            $(`[data-resource="${resource}"]`, els.resourceNav)?.click();
        });
        window.addEventListener('keydown', (event) => {
            if (event.key !== 'Escape') return;
            if (!els.editorModal.hidden) closeEditor();
            if (!els.importModal.hidden) closeImport();
        });
    }

    function init() {
        bindElements();
        bindEvents();
        loadSnapshot({quiet: true});
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init, {once: true});
    } else {
        init();
    }
})();
