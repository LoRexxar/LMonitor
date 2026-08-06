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
    const clamp = (value, minimum, maximum) => (
        Math.min(maximum, Math.max(minimum, value))
    );
    const POSITION_DIRECTORY_RESOURCES = ['versions', 'dungeons'];
    const POSITION_DUNGEON_RESOURCES = ['floors', 'enemies', 'spawns'];
    const SPAWN_GROUP_COLORS = [
        '#2563eb', '#db2777', '#059669', '#d97706', '#7c3aed',
        '#0891b2', '#dc2626', '#4f46e5', '#65a30d', '#c026d3',
    ];

    const state = {
        snapshot: null,
        selectedId: null,
        draft: null,
        pointerId: null,
        mode: 'edit',
        enemyId: null,
        isCreating: false,
        isGrouping: false,
        groupSelection: new Set(),
        activeGroupKey: '',
        manualSequence: 0,
        toastTimer: null,
    };
    const els = {};

    function rowBy(resource, id) {
        return state.snapshot?.[resource]?.find(
            (row) => Number(row.id) === Number(id),
        ) || null;
    }

    function displayName(row) {
        return row?.name_zh || row?.name || row?.label || row?.key || '—';
    }

    function selectedVersionId() {
        return Number(els.version.value || 0);
    }

    function selectedDungeonId() {
        return Number(els.dungeon.value || 0);
    }

    function selectedFloorId() {
        return Number(els.floor.value || 0);
    }

    function selectedSpawn() {
        return state.selectedId ? rowBy('spawns', state.selectedId) : null;
    }

    function selectedEnemy() {
        return rowBy('enemies', state.enemyId);
    }

    function toast(message, isError = false) {
        window.clearTimeout(state.toastTimer);
        els.toast.textContent = message;
        els.toast.classList.toggle('is-error', isError);
        els.toast.hidden = false;
        state.toastTimer = window.setTimeout(() => {
            els.toast.hidden = true;
        }, 3500);
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

    function clearSelection() {
        state.selectedId = null;
        state.draft = null;
        state.pointerId = null;
        state.mode = 'edit';
        state.enemyId = null;
        state.isCreating = false;
        state.isGrouping = false;
        state.groupSelection.clear();
        state.activeGroupKey = '';
    }

    function chooseFirstOption(select) {
        if (!select.value && select.options.length > 1) {
            select.value = select.options[1].value;
        }
    }

    function renderFilters({initialize = false} = {}) {
        const previousVersion = els.version.value;
        const previousDungeon = els.dungeon.value;
        const previousFloor = els.floor.value;
        const versions = state.snapshot?.versions || [];
        els.version.innerHTML = '<option value="">请选择数据版本</option>' + versions
            .map((row) => `
                <option value="${row.id}">
                    ${escapeHtml(row.label)}${row.is_active ? '（生效）' : ''}
                </option>
            `)
            .join('');
        if (versions.some((row) => String(row.id) === previousVersion)) {
            els.version.value = previousVersion;
        } else if (initialize) {
            const active = versions.find((row) => row.is_active);
            els.version.value = active ? String(active.id) : '';
            chooseFirstOption(els.version);
        }

        const dungeons = (state.snapshot?.dungeons || []).filter(
            (row) => Number(row.data_version_id) === selectedVersionId(),
        );
        els.dungeon.innerHTML = '<option value="">请选择地下城</option>' + dungeons
            .filter((row) => row.is_active !== false)
            .map((row) => `<option value="${row.id}">${escapeHtml(displayName(row))}</option>`)
            .join('');
        if (dungeons.some((row) => String(row.id) === previousDungeon)) {
            els.dungeon.value = previousDungeon;
        } else if (initialize) {
            chooseFirstOption(els.dungeon);
        }

        const floors = (state.snapshot?.floors || []).filter(
            (row) => (
                Number(row.dungeon_id) === selectedDungeonId()
                && row.is_active !== false
            ),
        );
        els.floor.innerHTML = '<option value="">请选择楼层</option>' + floors
            .map((row) => `<option value="${row.id}">${escapeHtml(displayName(row))}</option>`)
            .join('');
        if (floors.some((row) => String(row.id) === previousFloor)) {
            els.floor.value = previousFloor;
        } else if (initialize) {
            chooseFirstOption(els.floor);
        }
    }

    function enemiesForDungeon() {
        return (state.snapshot?.enemies || []).filter(
            (row) => (
                Number(row.dungeon_id) === selectedDungeonId()
                && (row.is_active !== false || Number(row.id) === Number(state.enemyId))
            ),
        );
    }

    function renderEnemyOptions(selectedId = null) {
        const currentValue = selectedId == null ? state.enemyId : Number(selectedId);
        const enemies = enemiesForDungeon();
        els.enemy.innerHTML = '<option value="">请选择怪物</option>' + enemies
            .map((row) => `
                <option value="${row.id}">
                    ${escapeHtml(displayName(row))}${row.npc_id ? ` · NPC ${row.npc_id}` : ''}
                </option>
            `)
            .join('');
        if (enemies.some((row) => String(row.id) === String(currentValue))) {
            els.enemy.value = String(currentValue);
        }
    }

    function floorSpawns() {
        const floorId = selectedFloorId();
        return (state.snapshot?.spawns || []).filter((row) => {
            if (Number(row.floor_id) !== floorId) return false;
            if (!els.showInactive.checked && row.is_active === false) return false;
            return true;
        });
    }

    function visibleSpawns() {
        const search = els.search.value.trim().toLowerCase();
        return floorSpawns().filter((row) => {
            const enemy = rowBy('enemies', row.enemy_id);
            if (!search) return true;
            return [row, enemy].filter(Boolean).some(
                (item) => JSON.stringify(item).toLowerCase().includes(search),
            );
        });
    }

    function groupDisplayName(groupKey) {
        const key = String(groupKey || '');
        if (!key) return '未分组';
        if (key.startsWith('manual-group-')) {
            return `自定义怪群 ${key.slice('manual-group-'.length)}`;
        }
        if (key.startsWith('group-')) {
            return `怪群 ${key.slice('group-'.length)}`;
        }
        return `怪群 ${key}`;
    }

    function groupSortValue(groupKey) {
        const match = String(groupKey || '').match(/(\d+)$/);
        return match ? Number(match[1]) : Number.MAX_SAFE_INTEGER;
    }

    function spawnGroups() {
        const groups = new Map();
        for (const row of floorSpawns()) {
            if (!row.group_key) continue;
            if (!groups.has(row.group_key)) groups.set(row.group_key, []);
            groups.get(row.group_key).push(row);
        }
        return Array.from(groups, ([key, members]) => ({key, members}))
            .sort((left, right) => (
                groupSortValue(left.key) - groupSortValue(right.key)
                || left.key.localeCompare(right.key, 'zh-CN')
            ))
            .map((group, index) => ({
                ...group,
                label: groupDisplayName(group.key),
                color: SPAWN_GROUP_COLORS[index % SPAWN_GROUP_COLORS.length],
            }));
    }

    function spawnGroup(groupKey, groups = null) {
        return (groups || spawnGroups()).find(
            (group) => group.key === groupKey,
        ) || null;
    }

    function markerHtml(row, groups) {
        const enemy = rowBy('enemies', row.enemy_id);
        const name = displayName(enemy);
        const label = Array.from(String(name).trim())[0] || '?';
        const markerColor = /^#[0-9a-f]{3,8}$/i.test(enemy?.marker_color || '')
            ? enemy.marker_color
            : '#64748b';
        const isSelected = Number(row.id) === Number(state.selectedId);
        const group = spawnGroup(row.group_key, groups);
        const isGroupSelected = (
            state.mode === 'group'
            && state.groupSelection.has(Number(row.id))
        );
        const isGroupTarget = Boolean(
            state.mode === 'group'
            &&
            state.activeGroupKey
            && row.group_key === state.activeGroupKey,
        );
        const position = isSelected && state.draft
            ? state.draft
            : {x: Number(row.x), y: Number(row.y)};
        const groupDescription = group
            ? `${group.label} · ${group.members.length} 个点位`
            : '未分组';
        return `
            <button
                type="button"
                class="mp-spawn-map-marker${row.is_position_manual ? ' is-manual' : ''}${isSelected ? ' is-selected' : ''}${group ? ' is-grouped' : ''}${isGroupSelected ? ' is-group-selected' : ''}${isGroupTarget ? ' is-group-target' : ''}"
                data-spawn-marker="${row.id}"
                style="left:${clamp(position.x, 0, 100)}%;top:${clamp(position.y, 0, 100)}%;--marker-color:${markerColor};--group-color:${group?.color || '#94a3b8'}"
                title="${escapeHtml(name)} · ${escapeHtml(groupDescription)} · ${Number(position.x).toFixed(2)}, ${Number(position.y).toFixed(2)}"
                aria-label="${escapeHtml(name)}，${escapeHtml(groupDescription)}，坐标 ${Number(position.x).toFixed(2)}, ${Number(position.y).toFixed(2)}"
                ${state.mode === 'group' ? `aria-pressed="${isGroupSelected ? 'true' : 'false'}"` : ''}
            >${escapeHtml(label)}</button>
        `;
    }

    function draftMarkerHtml() {
        if (state.mode !== 'create' || !state.draft) return '';
        const enemy = selectedEnemy();
        const name = displayName(enemy);
        const label = Array.from(String(name).trim())[0] || '+';
        const markerColor = /^#[0-9a-f]{3,8}$/i.test(enemy?.marker_color || '')
            ? enemy.marker_color
            : '#2563eb';
        return `
            <button
                type="button"
                class="mp-spawn-map-marker is-selected is-draft"
                data-draft-marker
                style="left:${state.draft.x}%;top:${state.draft.y}%;--marker-color:${markerColor}"
                title="新增：${escapeHtml(name)}"
                aria-label="待新增的${escapeHtml(name)}"
            >${escapeHtml(label)}</button>
        `;
    }

    function renderMap() {
        const floor = rowBy('floors', selectedFloorId());
        els.canvas.classList.toggle(
            'is-create-mode',
            state.mode === 'create' && Boolean(state.enemyId),
        );
        els.canvas.classList.toggle('is-group-mode', state.mode === 'group');
        els.canvas.classList.toggle('is-placing', state.isCreating);
        if (!floor) {
            els.canvas.classList.remove('is-ready');
            els.canvas.style.removeProperty('aspect-ratio');
            els.canvas.style.removeProperty('background-image');
            els.layer.innerHTML = '';
            els.empty.hidden = false;
            els.empty.textContent = '请选择数据版本、地下城和楼层。';
            return;
        }
        els.canvas.classList.add('is-ready');
        els.canvas.style.aspectRatio = `${Math.max(1, Number(floor.map_width || 1))} / ${Math.max(1, Number(floor.map_height || 1))}`;
        els.canvas.style.backgroundColor = floor.background_color || '#302d2a';
        els.canvas.style.backgroundImage = floor.background_url
            ? `url(${JSON.stringify(String(floor.background_url))})`
            : 'none';
        const rows = visibleSpawns();
        const selectedStillVisible = rows.some(
            (row) => Number(row.id) === Number(state.selectedId),
        );
        if (state.mode === 'edit' && state.selectedId && !selectedStillVisible) {
            clearSelection();
        }
        els.empty.hidden = rows.length > 0 || state.mode === 'create';
        els.empty.textContent = '当前楼层没有符合筛选条件的刷新点。';
        const groups = spawnGroups();
        els.layer.innerHTML = rows.map(
            (row) => markerHtml(row, groups),
        ).join('') + draftMarkerHtml();
    }

    function selectedGroupSpawns() {
        return floorSpawns().filter(
            (row) => state.groupSelection.has(Number(row.id)),
        );
    }

    function renderGroupInspector() {
        const groups = spawnGroups();
        const selectedRows = selectedGroupSpawns();
        const selectedCount = selectedRows.length;
        const activeGroup = spawnGroup(state.activeGroupKey, groups);
        const selectedNames = selectedRows.slice(0, 3).map((row) => (
            displayName(rowBy('enemies', row.enemy_id))
        ));
        els.groupSelectionTitle.textContent = selectedCount
            ? `已选择 ${selectedCount} 个点位`
            : '尚未选择点位';
        els.groupSelectionSummary.textContent = selectedCount
            ? (
                `${selectedNames.join('、')}`
                + (selectedCount > selectedNames.length
                    ? ` 等 ${selectedCount} 个点位`
                    : '')
            )
            : '点击地图圆点，或从下方选择一个已有怪群。';
        els.groupCount.textContent = String(groups.length);
        els.groupList.innerHTML = groups.length
            ? groups.map((group) => {
                const isActive = group.key === state.activeGroupKey;
                const manualCount = group.members.filter(
                    (row) => row.is_group_manual,
                ).length;
                return `
                    <button
                        type="button"
                        class="mp-spawn-group-row${isActive ? ' is-active' : ''}"
                        data-spawn-group-key="${escapeHtml(group.key)}"
                        style="--group-color:${group.color}"
                        aria-pressed="${isActive ? 'true' : 'false'}"
                    >
                        <span class="mp-spawn-group-swatch" aria-hidden="true"></span>
                        <span>
                            <strong>${escapeHtml(group.label)}</strong>
                            <small>${manualCount ? `${manualCount} 个点位人工维护` : 'MDT 原始分组'}</small>
                        </span>
                        <b>${group.members.length}</b>
                    </button>
                `;
            }).join('')
            : '<div class="mp-spawn-group-empty">当前楼层还没有怪群。请在地图上多选点位后新建。</div>';
        els.groupClear.disabled = state.isGrouping || !selectedCount;
        els.groupCreate.disabled = state.isGrouping || !selectedCount;
        els.groupAssign.disabled = (
            state.isGrouping
            || !selectedCount
            || !activeGroup
        );
        els.groupRemove.disabled = (
            state.isGrouping
            || !selectedRows.some((row) => Boolean(row.group_key))
        );
        els.groupRestore.disabled = (
            state.isGrouping
            || !selectedRows.some((row) => row.is_group_manual)
        );
    }

    function renderInspector() {
        const row = selectedSpawn();
        const creating = state.mode === 'create';
        const grouping = state.mode === 'group';
        const enabled = creating || Boolean(row);
        const enemy = row ? rowBy('enemies', row.enemy_id) : selectedEnemy();
        const draft = state.draft || (row ? {
            x: Number(row.x),
            y: Number(row.y),
        } : null);
        const createEnemy = selectedEnemy();

        els.pointInspector.hidden = grouping;
        els.groupInspector.hidden = !grouping;
        els.groupManage.classList.toggle('is-active', grouping);
        els.groupManage.textContent = grouping ? '正在管理怪群' : '管理怪群';
        els.groupManage.disabled = !selectedFloorId() || creating;
        els.create.disabled = creating || grouping || !selectedFloorId();
        if (grouping) {
            renderGroupInspector();
            return;
        }

        renderEnemyOptions(state.enemyId ?? row?.enemy_id);
        els.enemy.disabled = !enabled || state.isCreating;
        els.key.disabled = true;
        els.keyField.hidden = creating;
        els.scale.disabled = !enabled || state.isCreating;
        els.coordinates.hidden = creating;
        els.x.disabled = creating || !enabled;
        els.y.disabled = creating || !enabled;
        els.save.hidden = creating;
        els.save.disabled = creating || !enabled || !draft || !Number(state.enemyId);
        els.reset.hidden = creating;
        els.reset.disabled = !(
            !creating
            && row?.is_position_manual
            && row?.imported_position
        );
        els.cancel.hidden = !creating;
        els.cancel.disabled = state.isCreating;

        els.enemyName.textContent = creating
            ? (
                createEnemy
                    ? `连续添加：${displayName(createEnemy)}`
                    : '选择要添加的怪物'
            )
            : (row ? displayName(enemy) : '尚未选择怪物');
        els.selectionMeta.textContent = creating
            ? (
                createEnemy
                    ? '直接点击地图，点击一次就新增一个点'
                    : '先选择怪物，然后点击地图落点'
            )
            : (
                row
                    ? `${row.key} · ${row.is_position_manual ? '人工坐标已锁定' : '当前使用上游导入坐标'}`
                    : '点击地图上的圆点开始编辑'
            );
        if (!creating) {
            els.key.value = row?.key || '';
            els.scale.value = row ? Number(row.scale || 1).toFixed(2) : '';
        }
        els.currentGroup.textContent = creating
            ? '新增后默认为未分组'
            : groupDisplayName(row?.group_key);
        els.currentGroup.classList.toggle(
            'is-manual',
            Boolean(row?.is_group_manual),
        );
        els.x.value = draft ? Number(draft.x).toFixed(2) : '';
        els.y.value = draft ? Number(draft.y).toFixed(2) : '';
        els.positionHelp.textContent = creating
            ? (
                state.isCreating
                    ? '正在添加点位，请稍候…'
                    : (
                        createEnemy
                            ? '无需填写坐标；每点击一次地图就会立即新增一个人工点位。'
                            : '选择怪物后直接点击地图，无需填写坐标。'
                    )
            )
            : (
                row?.is_position_manual
                    ? '该点位已人工锁定；可恢复到最近一次导入的上游坐标。'
                    : '修改坐标、关联怪物或怪群联动后，点击保存写入数据库。'
            );
        els.save.textContent = '保存修改';
    }

    function renderSummary() {
        const rows = visibleSpawns();
        const allFloorRows = (state.snapshot?.spawns || []).filter(
            (row) => Number(row.floor_id) === selectedFloorId(),
        );
        const items = [
            ['当前显示', rows.length],
            ['本层全部', allFloorRows.length],
            ['已有怪群', spawnGroups().length],
            ['人工点位', allFloorRows.filter((row) => row.is_position_manual).length],
            ['关联怪物', new Set(rows.map((row) => row.enemy_id)).size],
        ];
        els.summary.innerHTML = items
            .map(([label, value]) => `<span class="mp-admin-chip">${label}<strong>${value}</strong></span>`)
            .join('');
    }

    function renderTable() {
        const rows = visibleSpawns();
        const groups = spawnGroups();
        if (!rows.length) {
            els.tableBody.innerHTML = '<tr><td colspan="9" class="mp-admin-empty">当前楼层没有符合筛选条件的刷新点。</td></tr>';
            return;
        }
        els.tableBody.innerHTML = rows.map((row) => {
            const enemy = rowBy('enemies', row.enemy_id);
            const group = spawnGroup(row.group_key, groups);
            const rowClasses = [
                Number(row.id) === Number(state.selectedId)
                    ? 'is-selected'
                    : '',
                state.groupSelection.has(Number(row.id))
                    ? 'is-group-selected'
                    : '',
            ].filter(Boolean).join(' ');
            return `
                <tr class="${rowClasses}">
                    <td>
                        <div class="mp-admin-name">
                            <strong>${escapeHtml(displayName(enemy))}</strong>
                            <span>${escapeHtml(enemy?.name || '')}</span>
                        </div>
                    </td>
                    <td><span class="mp-admin-key">${escapeHtml(row.key)}</span></td>
                    <td>${enemy?.npc_id || '—'}</td>
                    <td>${Number(enemy?.enemy_forces || 0)}</td>
                    <td>${Number(row.x).toFixed(2)}, ${Number(row.y).toFixed(2)}</td>
                    <td>
                        <span class="mp-admin-status${group ? ' is-grouped' : ''}${row.is_group_manual ? ' is-manual' : ''}">
                            ${escapeHtml(group?.label || '未分组')}
                        </span>
                    </td>
                    <td>
                        <span class="mp-admin-status ${row.is_position_manual ? 'is-manual' : ''}">
                            ${row.is_position_manual ? '人工维护' : 'MDT 导入'}
                        </span>
                    </td>
                    <td>
                        <span class="mp-admin-status ${row.is_active !== false ? 'is-active' : ''}">
                            ${row.is_active !== false ? '启用' : '停用'}
                        </span>
                    </td>
                    <td>
                        <div class="mp-admin-row-actions">
                            <button type="button" data-locate-spawn="${row.id}">定位并编辑</button>
                        </div>
                    </td>
                </tr>
            `;
        }).join('');
    }

    function renderAll() {
        renderMap();
        renderInspector();
        renderSummary();
        renderTable();
    }

    function mergeDungeonSnapshot(snapshot) {
        state.snapshot = {
            ...(state.snapshot || {}),
            floors: snapshot.floors || [],
            enemies: snapshot.enemies || [],
            spawns: snapshot.spawns || [],
        };
    }

    async function loadDungeonSnapshot(dungeonId, {quiet = false} = {}) {
        clearSelection();
        mergeDungeonSnapshot({});
        renderFilters();
        renderAll();
        if (!dungeonId) return;
        if (!quiet) {
            els.tableBody.innerHTML = '<tr><td colspan="8" class="mp-admin-loading">正在加载当前地下城点位…</td></tr>';
        }
        const payload = await request(
            `/api/mythic-planner/manage/?resources=${POSITION_DUNGEON_RESOURCES.join(',')}&dungeon_id=${dungeonId}`,
        );
        mergeDungeonSnapshot(payload.data);
        renderFilters({initialize: true});
        renderAll();
    }

    async function loadSnapshot({quiet = false} = {}) {
        if (!quiet) {
            els.tableBody.innerHTML = '<tr><td colspan="8" class="mp-admin-loading">正在加载点位数据…</td></tr>';
        }
        try {
            const payload = await request(
                `/api/mythic-planner/manage/?resources=${POSITION_DIRECTORY_RESOURCES.join(',')}`,
            );
            state.snapshot = payload.data;
            renderFilters({initialize: true});
            await loadDungeonSnapshot(selectedDungeonId(), {quiet: true});
            if (!quiet) toast('地图点位数据已刷新。');
        } catch (error) {
            els.tableBody.innerHTML = `<tr><td colspan="8" class="mp-admin-empty">${escapeHtml(error.message)}</td></tr>`;
            toast(error.message, true);
        }
    }

    function selectSpawn(spawnId, {scroll = false} = {}) {
        const row = rowBy('spawns', spawnId);
        if (!row) return;
        state.mode = 'edit';
        state.groupSelection.clear();
        state.activeGroupKey = '';
        state.isGrouping = false;
        state.selectedId = row.id;
        state.draft = {x: Number(row.x), y: Number(row.y)};
        state.enemyId = row.enemy_id;
        state.isCreating = false;
        renderAll();
        if (scroll) {
            els.editor.scrollIntoView({behavior: 'smooth', block: 'start'});
        }
    }

    function beginCreate() {
        if (!selectedFloorId()) {
            toast('请先选择地下城和楼层。', true);
            return;
        }
        state.mode = 'create';
        state.selectedId = null;
        state.draft = null;
        state.enemyId = null;
        state.isCreating = false;
        state.groupSelection.clear();
        state.activeGroupKey = '';
        state.isGrouping = false;
        els.key.value = '';
        els.scale.value = '1.00';
        renderAll();
        els.enemy.focus();
    }

    function cancelCreate() {
        clearSelection();
        renderAll();
    }

    function beginGroupManage() {
        if (!selectedFloorId()) {
            toast('请先选择地下城和楼层。', true);
            return;
        }
        state.mode = 'group';
        state.selectedId = null;
        state.draft = null;
        state.pointerId = null;
        state.enemyId = null;
        state.isCreating = false;
        state.isGrouping = false;
        state.groupSelection.clear();
        state.activeGroupKey = '';
        renderAll();
    }

    function finishGroupManage() {
        clearSelection();
        renderAll();
    }

    function toggleGroupSpawn(spawnId) {
        const numericId = Number(spawnId);
        if (!rowBy('spawns', numericId)) return;
        if (state.groupSelection.has(numericId)) {
            state.groupSelection.delete(numericId);
        } else {
            state.groupSelection.add(numericId);
        }
        renderMap();
        renderGroupInspector();
        renderTable();
    }

    function selectSpawnGroup(groupKey) {
        const group = spawnGroup(groupKey);
        if (!group) return;
        state.activeGroupKey = group.key;
        state.groupSelection = new Set(
            group.members.map((row) => Number(row.id)),
        );
        renderAll();
    }

    async function updateSpawnGroups(action) {
        const spawnIds = Array.from(state.groupSelection);
        if (!spawnIds.length || state.isGrouping) return;
        if (action === 'assign' && !state.activeGroupKey) {
            toast('请先选择一个已有怪群。', true);
            return;
        }
        state.isGrouping = true;
        renderGroupInspector();
        const actionLabels = {
            create: '已创建新怪群。',
            assign: '已加入选中的怪群。',
            remove: '已将所选点位移出怪群。',
            restore: '已恢复所选点位的 MDT 分组。',
        };
        try {
            const payload = await request('/api/mythic-planner/manage/', {
                method: 'PATCH',
                body: JSON.stringify({
                    resource: 'spawn_groups',
                    snapshot_resources: POSITION_DUNGEON_RESOURCES,
                    snapshot_dungeon_id: selectedDungeonId(),
                    data: {
                        action,
                        spawn_ids: spawnIds,
                        ...(action === 'assign'
                            ? {group_key: state.activeGroupKey}
                            : {}),
                    },
                }),
            });
            mergeDungeonSnapshot(payload.snapshot);
            state.groupSelection = new Set(payload.data.spawn_ids || []);
            state.activeGroupKey = ['create', 'assign'].includes(action)
                ? payload.data.group_key
                : '';
            toast(actionLabels[action] || '怪群设置已保存。');
        } catch (error) {
            toast(error.message, true);
        } finally {
            state.isGrouping = false;
            renderFilters();
            renderAll();
        }
    }

    function positionOnMap(event) {
        const rect = els.canvas.getBoundingClientRect();
        if (!rect.width || !rect.height) return null;
        return {
            x: clamp(((event.clientX - rect.left) / rect.width) * 100, 0, 100),
            y: clamp(((event.clientY - rect.top) / rect.height) * 100, 0, 100),
        };
    }

    function updateDraft(position) {
        if (!position) return;
        state.draft = {
            x: clamp(Number(position.x), 0, 100),
            y: clamp(Number(position.y), 0, 100),
        };
        const marker = state.mode === 'create'
            ? $('[data-draft-marker]', els.layer)
            : $(`[data-spawn-marker="${state.selectedId}"]`, els.layer);
        if (marker) {
            marker.style.left = `${state.draft.x}%`;
            marker.style.top = `${state.draft.y}%`;
        } else {
            renderMap();
        }
        els.x.value = state.draft.x.toFixed(2);
        els.y.value = state.draft.y.toFixed(2);
        if (state.mode !== 'create') {
            els.save.disabled = !Number(state.enemyId);
        }
    }

    function beginDrag(event) {
        if (state.mode !== 'edit') return;
        const marker = event.target.closest('[data-spawn-marker]');
        if (!marker || event.button !== 0) return;
        event.preventDefault();
        const spawnId = Number(marker.dataset.spawnMarker);
        if (spawnId !== Number(state.selectedId)) {
            selectSpawn(spawnId);
        }
        state.pointerId = event.pointerId;
        marker.classList.add('is-dragging');
        try {
            marker.setPointerCapture?.(event.pointerId);
        } catch (_error) {
            // 某些合成鼠标事件没有可捕获的活动指针，拖动仍可继续。
        }
    }

    function moveDrag(event) {
        if (state.pointerId !== event.pointerId) return;
        event.preventDefault();
        updateDraft(positionOnMap(event));
    }

    function endDrag(event) {
        if (state.pointerId !== event.pointerId) return;
        state.pointerId = null;
        $$('[data-spawn-marker]', els.layer).forEach(
            (marker) => marker.classList.remove('is-dragging'),
        );
    }

    function updateDraftFromInputs() {
        const x = Number(els.x.value);
        const y = Number(els.y.value);
        if (!Number.isFinite(x) || !Number.isFinite(y)) return;
        updateDraft({x, y});
    }

    function validatedScale() {
        const scale = Number(els.scale.value || 1);
        if (!Number.isFinite(scale) || scale < 0.25 || scale > 5) {
            throw new Error('缩放必须在 0.25 到 5 之间。');
        }
        return scale;
    }

    function nextManualKey() {
        state.manualSequence += 1;
        return `manual-${Date.now().toString(36)}-${state.manualSequence.toString(36)}`;
    }

    function editFormData() {
        const scale = validatedScale();
        if (!Number(state.enemyId)) throw new Error('请选择要关联的怪物原型。');
        if (!state.draft) throw new Error('请在地图上选择或拖动一个点位。');
        const key = els.key.value.trim();
        if (!key) throw new Error('刷新点 key 不能为空。');
        return {
            enemy_id: Number(state.enemyId),
            floor_id: selectedFloorId(),
            key,
            x: Number(state.draft.x.toFixed(4)),
            y: Number(state.draft.y.toFixed(4)),
            scale,
            patrol: selectedSpawn()?.patrol || [],
            is_active: selectedSpawn()?.is_active !== false,
        };
    }

    async function savePosition() {
        const row = selectedSpawn();
        if (!row || state.mode === 'create') return;
        try {
            const data = editFormData();
            const payload = await request(
                `/api/mythic-planner/manage/${row.id}/`,
                {
                    method: 'PATCH',
                    body: JSON.stringify({
                        resource: 'spawns',
                        snapshot_resources: POSITION_DUNGEON_RESOURCES,
                        snapshot_dungeon_id: selectedDungeonId(),
                        data,
                    }),
                },
            );
            mergeDungeonSnapshot(payload.snapshot);
            state.mode = 'edit';
            state.selectedId = payload.data.id;
            const saved = selectedSpawn();
            state.enemyId = saved?.enemy_id || null;
            state.draft = saved
                ? {x: Number(saved.x), y: Number(saved.y)}
                : null;
            renderFilters();
            renderAll();
            toast('点位和关联怪物已保存。');
        } catch (error) {
            toast(error.message, true);
        }
    }

    async function createPositionAt(position) {
        if (state.mode !== 'create' || state.isCreating || !position) return;
        if (!Number(state.enemyId)) {
            toast('请先选择要添加的怪物。', true);
            els.enemy.focus();
            return;
        }
        const enemy = selectedEnemy();
        state.isCreating = true;
        state.draft = {
            x: clamp(Number(position.x), 0, 100),
            y: clamp(Number(position.y), 0, 100),
        };
        renderInspector();
        renderMap();
        try {
            const payload = await request('/api/mythic-planner/manage/', {
                method: 'POST',
                body: JSON.stringify({
                    resource: 'spawns',
                    snapshot_resources: POSITION_DUNGEON_RESOURCES,
                    snapshot_dungeon_id: selectedDungeonId(),
                    data: {
                        enemy_id: Number(state.enemyId),
                        floor_id: selectedFloorId(),
                        key: nextManualKey(),
                        x: Number(state.draft.x.toFixed(4)),
                        y: Number(state.draft.y.toFixed(4)),
                        scale: validatedScale(),
                        patrol: [],
                        is_active: true,
                    },
                }),
            });
            mergeDungeonSnapshot(payload.snapshot);
            renderFilters();
            toast(`已添加${displayName(enemy)}；可以继续点击地图添加。`);
        } catch (error) {
            toast(error.message, true);
        } finally {
            state.isCreating = false;
            state.draft = null;
            renderAll();
        }
    }

    async function resetPosition() {
        const row = selectedSpawn();
        if (
            !row
            || !row.is_position_manual
            || !window.confirm('确认恢复到最近一次导入的上游坐标？')
        ) return;
        try {
            const payload = await request(
                `/api/mythic-planner/manage/${row.id}/`,
                {
                    method: 'PATCH',
                    body: JSON.stringify({
                        resource: 'spawn_position_reset',
                        snapshot_resources: POSITION_DUNGEON_RESOURCES,
                        snapshot_dungeon_id: selectedDungeonId(),
                    }),
                },
            );
            mergeDungeonSnapshot(payload.snapshot);
            const restored = selectedSpawn();
            if (restored) {
                const floor = rowBy('floors', restored.floor_id);
                if (Number(floor?.dungeon_id) === selectedDungeonId()) {
                    els.floor.value = String(restored.floor_id);
                }
                state.draft = {x: Number(restored.x), y: Number(restored.y)};
            }
            renderAll();
            toast('已恢复为最近一次导入的上游坐标。');
        } catch (error) {
            toast(error.message, true);
        }
    }

    async function changeDirectory(level) {
        clearSelection();
        if (level === 'version') {
            renderFilters();
            chooseFirstOption(els.dungeon);
        }
        try {
            await loadDungeonSnapshot(selectedDungeonId());
        } catch (error) {
            toast(error.message, true);
        }
    }

    function changeFloor() {
        clearSelection();
        renderAll();
    }

    function bindElements() {
        Object.assign(els, {
            editor: $('#spawn-map-editor'),
            version: $('#position-version-filter'),
            dungeon: $('#position-dungeon-filter'),
            floor: $('#spawn-map-floor'),
            search: $('#position-search'),
            showInactive: $('#position-show-inactive'),
            summary: $('#position-summary'),
            canvas: $('#spawn-map-canvas'),
            empty: $('#spawn-map-empty'),
            layer: $('#spawn-map-layer'),
            create: $('#spawn-map-create'),
            cancel: $('#spawn-map-cancel'),
            enemyName: $('#spawn-map-enemy-name'),
            selectionMeta: $('#spawn-map-selection-meta'),
            enemy: $('#spawn-map-enemy'),
            keyField: $('#spawn-map-key-field'),
            key: $('#spawn-map-key'),
            currentGroup: $('#spawn-map-current-group'),
            scale: $('#spawn-map-scale'),
            coordinates: $('#spawn-map-coordinates'),
            x: $('#spawn-map-x'),
            y: $('#spawn-map-y'),
            positionHelp: $('#spawn-map-position-help'),
            save: $('#spawn-map-save'),
            reset: $('#spawn-map-reset'),
            pointInspector: $('#spawn-map-point-inspector'),
            groupInspector: $('#spawn-map-group-inspector'),
            groupManage: $('#spawn-map-group-manage'),
            groupDone: $('#spawn-group-done'),
            groupSelectionTitle: $('#spawn-group-selection-title'),
            groupSelectionSummary: $('#spawn-group-selection-summary'),
            groupCount: $('#spawn-group-count'),
            groupList: $('#spawn-group-list'),
            groupClear: $('#spawn-group-clear'),
            groupCreate: $('#spawn-group-create'),
            groupAssign: $('#spawn-group-assign'),
            groupRemove: $('#spawn-group-remove'),
            groupRestore: $('#spawn-group-restore'),
            tableBody: $('#position-table-body'),
            toast: $('#mythic-planner-positions-toast'),
        });
    }

    function bindEvents() {
        $('#refresh-positions').addEventListener('click', () => loadSnapshot());
        els.version.addEventListener('change', () => changeDirectory('version'));
        els.dungeon.addEventListener('change', () => changeDirectory('dungeon'));
        els.floor.addEventListener('change', changeFloor);
        els.search.addEventListener('input', renderAll);
        els.showInactive.addEventListener('change', renderAll);
        els.create.addEventListener('click', beginCreate);
        els.cancel.addEventListener('click', cancelCreate);
        els.groupManage.addEventListener('click', () => {
            if (state.mode === 'group') {
                finishGroupManage();
            } else {
                beginGroupManage();
            }
        });
        els.groupDone.addEventListener('click', finishGroupManage);
        els.groupClear.addEventListener('click', () => {
            state.groupSelection.clear();
            renderAll();
        });
        els.groupCreate.addEventListener('click', () => updateSpawnGroups('create'));
        els.groupAssign.addEventListener('click', () => updateSpawnGroups('assign'));
        els.groupRemove.addEventListener('click', () => updateSpawnGroups('remove'));
        els.groupRestore.addEventListener('click', () => updateSpawnGroups('restore'));
        els.groupList.addEventListener('click', (event) => {
            const button = event.target.closest('[data-spawn-group-key]');
            if (button) selectSpawnGroup(button.dataset.spawnGroupKey);
        });
        els.enemy.addEventListener('change', () => {
            state.enemyId = Number(els.enemy.value || 0) || null;
            state.draft = null;
            renderInspector();
            renderMap();
        });
        els.x.addEventListener('input', updateDraftFromInputs);
        els.y.addEventListener('input', updateDraftFromInputs);
        els.save.addEventListener('click', savePosition);
        els.reset.addEventListener('click', resetPosition);
        els.layer.addEventListener('pointerdown', beginDrag);
        els.layer.addEventListener('pointermove', moveDrag);
        els.layer.addEventListener('pointerup', endDrag);
        els.layer.addEventListener('pointercancel', endDrag);
        els.layer.addEventListener('click', (event) => {
            const marker = event.target.closest('[data-spawn-marker]');
            if (marker) {
                const spawnId = Number(marker.dataset.spawnMarker);
                if (state.mode === 'group') {
                    toggleGroupSpawn(spawnId);
                    return;
                }
                if (spawnId !== Number(state.selectedId)) {
                    selectSpawn(spawnId);
                }
                return;
            }
            if (state.mode === 'create') {
                createPositionAt(positionOnMap(event));
            }
        });
        els.tableBody.addEventListener('click', (event) => {
            const button = event.target.closest('[data-locate-spawn]');
            if (button) {
                selectSpawn(Number(button.dataset.locateSpawn), {scroll: true});
            }
        });
    }

    function init() {
        bindElements();
        bindEvents();
        loadSnapshot();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init, {once: true});
    } else {
        init();
    }
})();
