(function () {
    const page = document.querySelector('.talent-sim-page');
    if (!page) return;

    const specsPayload = JSON.parse(document.getElementById('talent-specs-data')?.textContent || '[]');
    const els = {
        classSelect: document.getElementById('talent-class-select'),
        specSelect: document.getElementById('talent-spec-select'),
        importInput: document.getElementById('talent-import-input'),
        importBtn: document.getElementById('talent-import-btn'),
        resetBtn: document.getElementById('talent-reset-btn'),
        copyUrlBtn: document.getElementById('talent-copy-url-btn'),
        copyCodeBtn: document.getElementById('talent-copy-code-btn'),
        stageContainer: document.getElementById('talent-stage-container'),
        specIcon: document.getElementById('talent-spec-icon'),
        specTitle: document.getElementById('talent-spec-title'),
        parseStatus: document.getElementById('talent-parse-status'),
        classPoints: document.getElementById('talent-class-points'),
        heroPoints: document.getElementById('talent-hero-points'),
        specPoints: document.getElementById('talent-spec-points'),
        totalPoints: document.getElementById('talent-total-points'),
        codeOutput: document.getElementById('talent-build-code-output'),
        inspectorEmpty: document.getElementById('talent-inspector-empty'),
        inspectorContent: document.getElementById('talent-inspector-content'),
        inspectorIcon: document.getElementById('talent-inspector-icon'),
        inspectorName: document.getElementById('talent-inspector-name'),
        inspectorMeta: document.getElementById('talent-inspector-meta'),
        inspectorDesc: document.getElementById('talent-inspector-desc'),
        inspectorOptions: document.getElementById('talent-inspector-options'),
        toastRoot: document.getElementById('talent-toast-root'),
    };

    const state = {
        className: page.dataset.initialClass || 'DeathKnight',
        specName: page.dataset.initialSpec || 'Blood',
        buildCode: new URLSearchParams(location.search).get('code') || '',
        heroSubtree: new URLSearchParams(location.search).get('hero') || '',
        payload: null,
        nodes: new Map(),
        parentKeysByChild: new Map(),
        selectedKey: '',
        encodeTimer: null,
        encodeRequestSeq: 0,
    };

    function nodeKey(node) {
        const identity = node.node_id || node.talent_id || node.spell_id || node.display_spell_id;
        return identity ? `${node.tree_type || 'spec'}:${identity}` : '';
    }

    function escapeHtml(value) {
        return String(value || '').replace(/[&<>'"]/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'}[ch]));
    }

    function toast(message) {
        const el = document.createElement('div');
        el.className = 'talent-toast';
        el.textContent = message;
        els.toastRoot.appendChild(el);
        setTimeout(() => el.remove(), 2400);
    }

    function currentClassPayload() {
        return specsPayload.find(item => item.class_name === state.className) || specsPayload[0];
    }

    function currentSpecPayload() {
        const cls = currentClassPayload();
        return (cls?.specs || []).find(item => item.spec_name === state.specName) || (cls?.specs || [])[0];
    }

    function initSelects() {
        els.classSelect.innerHTML = specsPayload.map(item => `<option value="${escapeHtml(item.class_name)}">${escapeHtml(item.class_cn)}</option>`).join('');
        els.classSelect.value = state.className;
        renderSpecSelect();
        els.classSelect.addEventListener('change', () => {
            state.className = els.classSelect.value;
            const cls = currentClassPayload();
            state.specName = (cls?.specs || [])[0]?.spec_name || '';
            state.buildCode = '';
            state.heroSubtree = '';
            renderSpecSelect();
            loadTree();
        });
        els.specSelect.addEventListener('change', () => {
            state.specName = els.specSelect.value;
            state.buildCode = '';
            state.heroSubtree = '';
            loadTree();
        });
    }

    function renderSpecSelect() {
        const cls = currentClassPayload();
        els.specSelect.innerHTML = (cls?.specs || []).map(spec => `<option value="${escapeHtml(spec.spec_name)}">${escapeHtml(spec.spec_cn)}</option>`).join('');
        els.specSelect.value = state.specName;
    }

    async function loadTree() {
        els.stageContainer.innerHTML = '<div class="talent-loading">正在加载天赋树...</div>';
        const params = new URLSearchParams({class: state.className, spec: state.specName});
        if (state.buildCode) params.set('code', state.buildCode);
        if (state.heroSubtree) params.set('hero', state.heroSubtree);
        const res = await fetch(`/portal/api/talents/simulator/?${params.toString()}`);
        const data = await res.json();
        if (!data.success) {
            els.stageContainer.innerHTML = `<div class="talent-loading">${escapeHtml(data.error || '加载失败')}</div>`;
            return;
        }
        state.payload = data;
        state.heroSubtree = String(data.active_hero_subtree || state.heroSubtree || '');
        indexNodes();
        renderHeader();
        renderStage();
        updateInspector();
        updateUrl(false);
        scheduleEncode();
    }

    function indexNodes() {
        state.nodes.clear();
        state.parentKeysByChild.clear();
        for (const tree of state.payload.render_model?.trees || []) {
            for (const node of tree.nodes || []) {
                const key = node.node_key || nodeKey(node);
                if (!key) continue;
                node.node_key = key;
                node.points = Number(node.points || 0);
                node.selected = !!node.selected || node.points > 0;
                if (node.choice_selection == null) node.choice_selection = 0;
                state.nodes.set(key, node);
            }
            for (const path of tree.paths || []) {
                if (!path.parent_key || !path.child_key) continue;
                if (!state.parentKeysByChild.has(path.child_key)) state.parentKeysByChild.set(path.child_key, []);
                state.parentKeysByChild.get(path.child_key).push(path.parent_key);
            }
        }
    }

    function renderHeader() {
        const spec = currentSpecPayload();
        els.specIcon.src = spec?.icon || '';
        els.specTitle.textContent = `${state.payload.class_cn} · ${state.payload.spec_cn}`;
        const statusMap = {success: '已导入 build code', empty: '空白模拟器', missing: '暂无天赋元数据'};
        els.parseStatus.textContent = statusMap[state.payload.parse_status] || state.payload.parse_status || '就绪';
        els.importInput.value = state.buildCode;
        updateCounters();
        renderHeroSwitcher();
    }

    function renderHeroSwitcher() {
        const subtrees = state.payload.hero_subtrees || [];
        if (subtrees.length <= 1) return;
        const wrapper = document.createElement('div');
        wrapper.className = 'talent-hero-switcher';
        wrapper.innerHTML = subtrees.map(item => `
            <button type="button" class="talent-sim-btn ${String(item.id) === String(state.heroSubtree) ? 'talent-sim-btn--primary' : 'talent-sim-btn--ghost'}" data-hero-subtree="${escapeHtml(item.id)}">
                ${escapeHtml(item.title)}
            </button>
        `).join('');
        const existing = document.querySelector('.talent-hero-switcher');
        if (existing) existing.remove();
        document.querySelector('.talent-sim-summary')?.appendChild(wrapper);
        wrapper.addEventListener('click', (event) => {
            const btn = event.target.closest('[data-hero-subtree]');
            if (!btn) return;
            state.heroSubtree = btn.dataset.heroSubtree;
            state.buildCode = '';
            loadTree();
        });
    }

    function renderStage() {
        const model = state.payload.render_model || {};
        const layout = model.layout || {};
        const width = Math.max(900, Number(layout.width || 1000));
        const height = Math.max(680, Number(layout.height || 700));
        const stage = document.createElement('div');
        stage.className = 'talent-render-stage';
        stage.style.width = `${width}px`;
        stage.style.height = `${height}px`;

        const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('class', 'talent-stage-svg');
        svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
        stage.appendChild(svg);

        for (const tree of model.trees || []) {
            if (tree.tree_type === 'build_code' || !tree.panel) continue;
            stage.appendChild(renderPanel(tree));
            for (const path of tree.paths || []) {
                const pathEl = document.createElementNS('http://www.w3.org/2000/svg', 'path');
                pathEl.setAttribute('d', path.svg_path || '');
                pathEl.setAttribute('class', `talent-path ${pathStateClass(path)}`);
                svg.appendChild(pathEl);
            }
            for (const node of tree.nodes || []) {
                stage.appendChild(renderNode(node));
            }
        }
        const wrapper = document.createElement('div');
        wrapper.className = 'talent-stage-scale-wrapper';
        const availableWidth = Math.max(320, els.stageContainer.clientWidth - 36);
        const scale = Math.min(1, Math.max(0.72, availableWidth / width));
        wrapper.style.width = `${Math.ceil(width * scale)}px`;
        wrapper.style.height = `${Math.ceil(height * scale)}px`;
        stage.style.transform = `scale(${scale})`;
        wrapper.appendChild(stage);
        els.stageContainer.innerHTML = '';
        els.stageContainer.appendChild(wrapper);
    }

    function renderPanel(tree) {
        const panel = tree.panel;
        const el = document.createElement('div');
        el.className = `talent-panel talent-panel--${tree.tree_type || 'spec'}`;
        el.style.left = `${panel.x}px`;
        el.style.top = `${panel.y}px`;
        el.style.width = `${panel.width}px`;
        el.style.height = `${panel.height}px`;
        el.innerHTML = `<div class="talent-panel-head"><div class="talent-panel-title">${escapeHtml(tree.title)}</div></div>`;
        return el;
    }

    function pathStateClass(path) {
        const parent = state.nodes.get(path.parent_key);
        const child = state.nodes.get(path.child_key);
        if (parent?.points > 0 && child?.points > 0) return 'is-active';
        if (parent?.points > 0) return 'is-unlocked';
        return 'is-locked';
    }

    function parentKeysFor(node) {
        const fromPaths = state.parentKeysByChild.get(node.node_key) || [];
        if (fromPaths.length) return fromPaths;
        return (node.parents || []).map(parentId => `${node.tree_type || 'spec'}:${parentId}`);
    }

    function canSelect(node) {
        const parents = parentKeysFor(node);
        if (!parents.length) return true;
        return parents.some(parentKey => (state.nodes.get(parentKey)?.points || 0) > 0);
    }

    function hasSelectedChild(node) {
        for (const [childKey, parentKeys] of state.parentKeysByChild.entries()) {
            if (!parentKeys.includes(node.node_key)) continue;
            if ((state.nodes.get(childKey)?.points || 0) > 0) return true;
        }
        return false;
    }

    function renderNode(node) {
        const key = node.node_key;
        const btn = document.createElement('button');
        btn.type = 'button';
        const selectable = canSelect(node);
        const stateClass = node.points > 0 ? 'is-selected' : (selectable ? 'is-available' : 'is-locked');
        btn.className = `talent-node-card--tree ${stateClass} ${node.is_choice_node ? 'is-choice-node' : ''}`;
        btn.setAttribute('aria-pressed', node.points > 0 ? 'true' : 'false');
        btn.setAttribute('aria-disabled', selectable ? 'false' : 'true');
        btn.dataset.nodeKey = key;
        btn.style.left = `${node.x}px`;
        btn.style.top = `${node.y}px`;
        btn.style.width = `${Math.max(30, Number(node.width || 36))}px`;
        btn.style.height = `${Math.max(30, Number(node.height || 36))}px`;
        btn.innerHTML = `${iconMarkup(node)}<div class="talent-node-tooltip"><div class="talent-node-tooltip-name">${escapeHtml(node.display_name)}</div><div class="talent-node-tooltip-desc">${escapeHtml(node.display_desc).slice(0, 260)}</div></div>`;
        btn.addEventListener('click', () => selectNode(node, 1));
        btn.addEventListener('contextmenu', event => {
            event.preventDefault();
            selectNode(node, -1);
        });
        return btn;
    }

    function iconMarkup(node) {
        const max = Number(node.max_points || 1);
        const points = Number(node.points || 0);
        const pointsMarkup = `<span class="talent-node-points">${points}/${max}</span>`;
        if (node.is_choice_node && points <= 0 && (node.choice_options || []).length >= 2) {
            const [left, right] = node.choice_options;
            return `<span class="talent-node-icon"><span class="talent-icon-split"><span class="talent-icon-split-half"><img src="${escapeHtml(left.icon_url || node.icon_url)}" alt=""></span><span class="talent-icon-split-half is-right"><img src="${escapeHtml(right.icon_url || node.icon_url)}" alt=""></span></span>${pointsMarkup}</span>`;
        }
        return `<span class="talent-node-icon"><img src="${escapeHtml(node.icon_url)}" alt="">${pointsMarkup}</span>`;
    }

    function resetNodeSelection(node) {
        node.points = 0;
        node.selected = false;
    }

    function pruneInvalidSelections() {
        let changed = false;
        let didPrune = false;
        do {
            changed = false;
            for (const node of state.nodes.values()) {
                if (Number(node.points || 0) > 0 && !canSelect(node)) {
                    resetNodeSelection(node);
                    changed = true;
                    didPrune = true;
                }
            }
        } while (changed);
        return didPrune;
    }

    function selectNode(node, delta) {
        state.selectedKey = node.node_key;
        const max = Number(node.max_points || 1);
        if (delta > 0 && !canSelect(node) && node.points <= 0) {
            toast('需要先点亮前置天赋');
            updateInspector();
            return;
        }
        if (delta < 0 && hasSelectedChild(node)) {
            toast('取消前置天赋会同步清除后续天赋');
        }
        node.points = Math.max(0, Math.min(max, Number(node.points || 0) + delta));
        node.selected = node.points > 0;
        if (pruneInvalidSelections()) toast('已清除失去前置条件的后续天赋');
        updateInspector();
        renderStage();
        updateCounters();
        scheduleEncode();
    }

    function updateCounters() {
        const totals = {class: 0, hero: 0, spec: 0};
        for (const node of state.nodes.values()) {
            const type = node.tree_type || 'spec';
            if (totals[type] == null) totals[type] = 0;
            totals[type] += Number(node.points || 0);
        }
        els.classPoints.textContent = totals.class || 0;
        els.heroPoints.textContent = totals.hero || 0;
        els.specPoints.textContent = totals.spec || 0;
        els.totalPoints.textContent = (totals.class || 0) + (totals.hero || 0) + (totals.spec || 0);
    }

    function updateInspector() {
        const node = state.nodes.get(state.selectedKey);
        if (!node) {
            els.inspectorEmpty.hidden = false;
            els.inspectorContent.hidden = true;
            return;
        }
        els.inspectorEmpty.hidden = true;
        els.inspectorContent.hidden = false;
        els.inspectorIcon.src = node.icon_url || '';
        els.inspectorName.textContent = node.display_name || '未命名天赋';
        els.inspectorMeta.textContent = `${treeLabel(node.tree_type)} · ${node.points || 0}/${node.max_points || 1} 点`;
        els.inspectorDesc.textContent = node.display_desc || '暂无描述';
        renderOptions(node);
    }

    function renderOptions(node) {
        const options = node.choice_options || [];
        if (!options.length) {
            els.inspectorOptions.innerHTML = '';
            return;
        }
        els.inspectorOptions.innerHTML = options.map((option, index) => `
            <button type="button" class="talent-inspector-option ${Number(node.choice_selection || 0) === index ? 'is-active' : ''}" data-option-index="${index}">
                <img src="${escapeHtml(option.icon_url)}" alt="">
                <span><strong>${escapeHtml(option.display_name)}</strong><span>${escapeHtml(option.display_desc).slice(0, 90)}</span></span>
            </button>
        `).join('');
        els.inspectorOptions.querySelectorAll('[data-option-index]').forEach(btn => {
            btn.addEventListener('click', () => chooseOption(node, Number(btn.dataset.optionIndex || 0)));
        });
    }

    function chooseOption(node, index) {
        if (!canSelect(node) && node.points <= 0) {
            toast('需要先点亮前置天赋');
            return;
        }
        const option = (node.choice_options || [])[index];
        if (!option) return;
        node.choice_selection = index;
        node.display_spell_id = option.display_spell_id || option.spell_id || node.display_spell_id;
        node.spell_id = option.spell_id || node.spell_id;
        node.icon_url = option.icon_url || node.icon_url;
        node.display_name = option.display_name || node.display_name;
        node.display_desc = option.display_desc || node.display_desc;
        if (node.points <= 0) node.points = 1;
        node.selected = true;
        updateInspector();
        renderStage();
        updateCounters();
        scheduleEncode();
    }

    function treeLabel(type) {
        return {class: '职业树', hero: '英雄树', spec: '专精树'}[type || 'spec'] || type;
    }

    function selectedNodesPayload() {
        const nodes = [];
        for (const node of state.nodes.values()) {
            if (Number(node.points || 0) <= 0) continue;
            nodes.push({
                tree_type: node.tree_type || 'spec',
                node_id: node.node_id || null,
                talent_id: node.talent_id || null,
                spell_id: node.spell_id || null,
                display_spell_id: node.display_spell_id || null,
                points: Number(node.points || 0),
                choice_selection: Number(node.choice_selection || 0),
            });
        }
        return nodes;
    }

    function scheduleEncode() {
        clearTimeout(state.encodeTimer);
        state.encodeTimer = setTimeout(encodeCurrent, 250);
    }

    async function encodeCurrent() {
        const requestSeq = ++state.encodeRequestSeq;
        const selected = selectedNodesPayload();
        if (!selected.length) {
            state.buildCode = '';
            els.codeOutput.textContent = '暂无';
            updateUrl(false);
            return;
        }
        const res = await fetch('/portal/api/talents/simulator/encode/', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                class_name: state.className,
                spec_name: state.specName,
                reference_build_code: state.buildCode,
                selected_nodes: selected,
            }),
        });
        const data = await res.json();
        if (requestSeq !== state.encodeRequestSeq) return;
        if (data.success && data.build_code) {
            state.buildCode = data.build_code;
            els.codeOutput.textContent = data.build_code;
            updateUrl(false);
        } else {
            els.codeOutput.textContent = data.error || '暂无可用参考导入头，需先导入一次该专精 build code';
        }
    }

    function updateUrl(push) {
        const params = new URLSearchParams({class: state.className, spec: state.specName});
        if (state.heroSubtree) params.set('hero', state.heroSubtree);
        if (state.buildCode) params.set('code', state.buildCode);
        const url = `/portal/talents/?${params.toString()}`;
        history[push ? 'pushState' : 'replaceState']({}, '', url);
    }

    function extractCode(raw) {
        const value = String(raw || '').trim();
        if (!value) return '';
        try {
            const url = new URL(value);
            return url.searchParams.get('code') || value;
        } catch (_) {
            return value;
        }
    }

    async function copyText(text, fallback) {
        const value = text || fallback || '';
        if (!value) {
            toast('当前没有可复制内容');
            return;
        }
        await navigator.clipboard.writeText(value);
        toast('已复制');
    }

    els.importBtn.addEventListener('click', () => {
        state.buildCode = extractCode(els.importInput.value);
        loadTree();
    });
    els.importInput.addEventListener('keydown', event => {
        if (event.key === 'Enter') {
            state.buildCode = extractCode(els.importInput.value);
            loadTree();
        }
    });
    els.resetBtn.addEventListener('click', () => {
        state.buildCode = '';
        for (const node of state.nodes.values()) {
            resetNodeSelection(node);
        }
        renderStage();
        updateCounters();
        updateInspector();
        scheduleEncode();
    });
    els.copyUrlBtn.addEventListener('click', () => copyText(location.href));
    els.copyCodeBtn.addEventListener('click', () => copyText(state.buildCode));
    window.addEventListener('popstate', () => {
        const params = new URLSearchParams(location.search);
        state.className = params.get('class') || state.className;
        state.specName = params.get('spec') || state.specName;
        state.buildCode = params.get('code') || '';
        state.heroSubtree = params.get('hero') || '';
        initSelects();
        loadTree();
    });

    initSelects();
    loadTree();
})();
