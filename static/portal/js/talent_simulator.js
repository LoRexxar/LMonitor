(function () {
    const page = document.querySelector('.talent-sim-page');
    if (!page) return;

    const specsPayload = JSON.parse(document.getElementById('talent-specs-data')?.textContent || '[]');
    const versionsPayload = JSON.parse(document.getElementById('talent-versions-data')?.textContent || '[]');
    const els = {
        versionSelect: document.getElementById('talent-version-select'),
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
        apexPoints: document.getElementById('talent-apex-points'),
        codeOutput: document.getElementById('talent-build-code-output'),
        inspectorEmpty: document.getElementById('talent-inspector-empty'),
        inspectorContent: document.getElementById('talent-inspector-content'),
        inspectorIcon: document.getElementById('talent-inspector-icon'),
        inspectorName: document.getElementById('talent-inspector-name'),
        inspectorMeta: document.getElementById('talent-inspector-meta'),
        inspectorDesc: document.getElementById('talent-inspector-desc'),
        inspectorOptions: document.getElementById('talent-inspector-options'),
        tooltipRoot: document.getElementById('talent-tooltip-root'),
        toastRoot: document.getElementById('talent-toast-root'),
    };

    const state = {
        versionKey: new URLSearchParams(location.search).get('version') || page.dataset.defaultVersion || versionsPayload[0]?.key || '',
        className: page.dataset.initialClass || 'DeathKnight',
        specName: page.dataset.initialSpec || 'Blood',
        buildCode: new URLSearchParams(location.search).get('code') || '',
        heroSubtree: new URLSearchParams(location.search).get('hero') || '',
        profileId: new URLSearchParams(location.search).get('profile_id') || new URLSearchParams(location.search).get('profile') || '',
        payload: null,
        nodes: new Map(),
        parentKeysByChild: new Map(),
        selectedKey: '',
        encodeTimer: null,
        encodeRequestSeq: 0,
        hoverKey: '',
        tooltipNodeKey: '',
        tooltipHideTimer: null,
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
        if (els.versionSelect) {
            els.versionSelect.innerHTML = versionsPayload.map(item => `<option value="${escapeHtml(item.key)}">${escapeHtml(item.label || item.key)}${item.branch === 'ptr' ? '（测试）' : ''}</option>`).join('');
            els.versionSelect.value = state.versionKey;
            els.versionSelect.addEventListener('change', () => {
                state.versionKey = els.versionSelect.value;
                state.buildCode = '';
                state.heroSubtree = '';
                state.profileId = '';
                loadTree();
            });
        }
        els.classSelect.innerHTML = specsPayload.map(item => `<option value="${escapeHtml(item.class_name)}">${escapeHtml(item.class_cn)}</option>`).join('');
        els.classSelect.value = state.className;
        renderSpecSelect();
        els.classSelect.addEventListener('change', () => {
            state.className = els.classSelect.value;
            const cls = currentClassPayload();
            state.specName = (cls?.specs || [])[0]?.spec_name || '';
            state.buildCode = '';
            state.heroSubtree = '';
            state.profileId = '';
            renderSpecSelect();
            loadTree();
        });
        els.specSelect.addEventListener('change', () => {
            state.specName = els.specSelect.value;
            state.buildCode = '';
            state.heroSubtree = '';
            state.profileId = '';
            loadTree();
        });
    }

    function renderSpecSelect() {
        const cls = currentClassPayload();
        els.specSelect.innerHTML = (cls?.specs || []).map(spec => `<option value="${escapeHtml(spec.spec_name)}">${escapeHtml(spec.spec_cn)}</option>`).join('');
        els.specSelect.value = state.specName;
    }

    async function loadTree() {
        hideTooltip();
        els.stageContainer.innerHTML = '<div class="talent-loading">正在加载天赋树...</div>';
        const params = new URLSearchParams({class: state.className, spec: state.specName});
        if (state.versionKey) params.set('version', state.versionKey);
        if (state.buildCode) params.set('code', state.buildCode);
        if (state.heroSubtree) params.set('hero', state.heroSubtree);
        if (state.profileId) params.set('profile_id', state.profileId);
        const res = await fetch(`/portal/api/talents/simulator/?${params.toString()}`);
        const data = await res.json();
        if (!data.success) {
            els.stageContainer.innerHTML = `<div class="talent-loading">${escapeHtml(data.error || '加载失败')}</div>`;
            return;
        }
        state.payload = data;
        if (data.build_code) state.buildCode = data.build_code;
        state.heroSubtree = String(data.active_hero_subtree || state.heroSubtree || '');
        indexNodes();
        renderHeader();
        renderStage();
        activateGrantedTalents(); // 自动点亮赠送天赋
        updateInspector();
        updateUrl(false);
        if (!state.buildCode) {
            scheduleEncode();
        } else {
            els.codeOutput.textContent = state.buildCode;
        }
    }

    function indexNodes() {
        state.nodes.clear();
        state.parentKeysByChild.clear();
        
        // 步骤1: 先识别赠送天赋（只在职业树中，flags=8 且 parents=[]）
        const grantedNodeKeys = new Set();
        
        for (const tree of state.payload.render_model?.trees || []) {
            for (const node of tree.nodes || []) {
                const treeType = node.tree_type || 'spec';
                const parents = node.parents || [];
                const flags = Number(node.flags || 0);
                
                // 赠送天赋的特征：职业树 且 flags=8 且无前置
                if (treeType === 'class' && flags === 8 && parents.length === 0) {
                    const key = node.node_key || nodeKey(node);
                    if (key) grantedNodeKeys.add(key);
                }
            }
        }
        
        // 步骤2: 索引所有节点，并自动标记赠送天赋
        for (const tree of state.payload.render_model?.trees || []) {
            for (const node of tree.nodes || []) {
                const key = node.node_key || nodeKey(node);
                if (!key) continue;
                node.node_key = key;
                node.points = Number(node.points || 0);
                
                // 自动识别赠送天赋
                if (grantedNodeKeys.has(key)) {
                    node.purchased = false;
                    node.selected = true;
                    // 赠送天赋自动设置为 1 点（如果当前为 0）
                    if (node.points === 0) {
                        node.points = 1;
                    }
                } else {
                    // 保留后端传来的 purchased 字段（默认 true 表示普通天赋）
                    if (node.purchased === undefined) node.purchased = true;
                    // 赠送天赋（purchased=false）即使 points=0 也要保持 selected=true
                    if (node.purchased === false) {
                        node.selected = true;
                    } else {
                        node.selected = !!node.selected || node.points > 0;
                    }
                }
                
                if (node.choice_selection == null) node.choice_selection = 0;
                node.is_apex_talent = !!node.is_apex_talent;
                node.point_pool = node.point_pool || (node.is_apex_talent ? 'apex' : (node.tree_type || 'spec'));
                state.nodes.set(key, node);
            }
            for (const path of tree.paths || []) {
                if (!path.parent_key || !path.child_key) continue;
                if (!state.parentKeysByChild.has(path.child_key)) state.parentKeysByChild.set(path.child_key, []);
                state.parentKeysByChild.get(path.child_key).push(path.parent_key);
            }
        }
    }

    function activateGrantedTalents() {
        // 自动为所有赠送天赋调用 selectNode() 点亮它们
        for (const [key, node] of state.nodes.entries()) {
            if (node.purchased === false && node.selected === true && node.points === 0) {
                // 这是赠送天赋，自动点击它
                selectNode(node);
            }
        }
    }

    function renderHeader() {
        const spec = currentSpecPayload();
        els.specIcon.src = spec?.icon || '';
        const versionLabel = state.payload.talent_version?.label || state.versionKey || '';
        els.specTitle.textContent = `${state.payload.class_cn} · ${state.payload.spec_cn}${versionLabel ? ' · ' + versionLabel : ''}`;
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
        const activeLabel = state.heroSubtree ? '已选择英雄天赋' : '先选择英雄天赋';
        wrapper.innerHTML = `<span class="talent-hero-switcher-label">${escapeHtml(activeLabel)}</span>` + subtrees.map(item => `
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
            chooseHeroSubtree(btn.dataset.heroSubtree);
        });
    }

    function chooseHeroSubtree(subtreeId) {
        if (!subtreeId || String(state.heroSubtree) === String(subtreeId)) return;
        state.heroSubtree = String(subtreeId);
        state.buildCode = '';
        loadTree();
    }

    function renderStage() {
        const model = state.payload.render_model || {};
        const trees = model.trees || [];
        const nodeCount = trees.reduce((total, tree) => total + (tree.nodes || []).length, 0);
        if (!nodeCount) {
            els.stageContainer.innerHTML = '<div class="talent-loading">当前版本暂无该专精天赋元数据，请先导入/回填对应版本的 DB2 数据。</div>';
            return;
        }
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
                pathEl.dataset.parentKey = path.parent_key || '';
                pathEl.dataset.childKey = path.child_key || '';
                pathEl.setAttribute('class', `talent-path ${pathStateClass(path)}`);
                svg.appendChild(pathEl);
            }
            for (const node of tree.nodes || []) {
                stage.appendChild(renderNode(node));
            }
        }
        renderHeroChoicePanel(stage, width, height);
        const wrapper = document.createElement('div');
        wrapper.className = 'talent-stage-scale-wrapper';
        const availableWidth = Math.max(320, els.stageContainer.clientWidth - 36);
        const panelRightEdges = trees
            .filter(tree => tree.tree_type !== 'build_code' && tree.panel)
            .map(tree => Number(tree.panel.x || 0) + Number(tree.panel.width || 0));
        const panelRightEdge = panelRightEdges.length ? Math.max(...panelRightEdges) : width;
        const scaleBaseWidth = Math.min(width, panelRightEdge);
        const scale = Math.min(1.35, Math.max(0.72, availableWidth / scaleBaseWidth));
        wrapper.style.width = `${Math.ceil(scaleBaseWidth * scale)}px`;
        wrapper.style.height = `${Math.ceil(height * scale)}px`;
        stage.style.transform = `scale(${scale})`;
        wrapper.appendChild(stage);
        els.stageContainer.innerHTML = '';
        els.stageContainer.appendChild(wrapper);
    }

    function renderHeroChoicePanel(stage, width, height) {
        const subtrees = state.payload.hero_subtrees || [];
        if (subtrees.length <= 1 || state.heroSubtree) return;
        const panel = document.createElement('div');
        panel.className = 'talent-hero-choice-panel';
        panel.style.left = `${Math.round(width / 2 - 180)}px`;
        panel.style.top = `${Math.round(Math.max(96, height * 0.16))}px`;
        panel.innerHTML = `
            <div class="talent-hero-choice-kicker">英雄天赋</div>
            <div class="talent-hero-choice-title">选择一棵英雄天赋树</div>
            <div class="talent-hero-choice-desc">英雄天赋需要先在两棵大树中选择其一，然后再点亮该树内节点。</div>
            <div class="talent-hero-choice-options">
                ${subtrees.map(item => `
                    <button type="button" class="talent-hero-choice-card" data-hero-subtree="${escapeHtml(item.id)}">
                        <span>${escapeHtml(item.title)}</span>
                        <small>${Number(item.node_count || 0)} 个节点</small>
                    </button>
                `).join('')}
            </div>
        `;
        panel.addEventListener('click', event => {
            const btn = event.target.closest('[data-hero-subtree]');
            if (!btn) return;
            chooseHeroSubtree(btn.dataset.heroSubtree);
        });
        stage.appendChild(panel);
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
        const hover = state.hoverKey && (state.hoverKey === path.parent_key || state.hoverKey === path.child_key);
        const classes = [];
        if (parent?.points > 0 && child?.points > 0) classes.push('is-active');
        else if (parent?.points > 0) classes.push('is-unlocked');
        else classes.push('is-locked');
        if (hover) classes.push('is-related');
        return classes.join(' ');
    }

    function parentKeysFor(node) {
        const fromPaths = state.parentKeysByChild.get(node.node_key) || [];
        if (fromPaths.length) return fromPaths;
        return (node.parents || []).map(parentId => `${node.tree_type || 'spec'}:${parentId}`);
    }

    function unlockedParentKeys(node) {
        return parentKeysFor(node).filter(parentKey => (state.nodes.get(parentKey)?.points || 0) > 0);
    }

    const TALENT_TREE_RULES = {
        gatedTrees: new Set(['class', 'spec', 'apex']),
        treePointCaps: {class: 34, spec: 30},
        totalPointCap: 68,
        earlyGateRows: 4,
        earlyGatePoints: 8,
        deepGateRows: 7,
        classDeepGatePoints: 23,
        specDeepGatePoints: 20,
    };

    function ruleTreeType(node) {
        if (!node) return '';
        const pool = node.point_pool || (node.is_apex_talent ? 'apex' : (node.tree_type || 'spec'));
        return TALENT_TREE_RULES.gatedTrees.has(pool) ? pool : '';
    }

    function gateTreeType(node) {
        const treeType = ruleTreeType(node);
        return treeType === 'apex' ? 'spec' : treeType;
    }

    function treeRows(treeType) {
        const rows = new Set();
        for (const candidate of state.nodes.values()) {
            if (ruleTreeType(candidate) !== treeType) continue;
            const row = Number(candidate.row || 0);
            if (row) rows.add(row);
        }
        const sorted = Array.from(rows).sort((a, b) => a - b);
        
        // 合并相邻的 row 值（差值≤10）为同一层级
        // 例如：4500 和 4501 应该算作同一层
        const merged = [];
        for (const row of sorted) {
            if (merged.length === 0 || row - merged[merged.length - 1] > 10) {
                merged.push(row);
            }
        }
        return merged;
    }

    function nodeLayer(node) {
        const treeType = gateTreeType(node);
        if (!treeType) return 0;
        if (ruleTreeType(node) === 'apex') return TALENT_TREE_RULES.deepGateRows + 1;
        const row = Number(node.row || 0);
        if (!row) return 0;
        const rows = treeRows(treeType);
        
        // 查找最接近的层级代表值
        let layer = 0;
        for (let i = 0; i < rows.length; i++) {
            if (Math.abs(row - rows[i]) <= 10) {
                layer = i + 1;
                break;
            }
        }
        return layer;
    }

    function pointsInTree(treeType, maxLayer) {
        let total = 0;
        for (const candidate of state.nodes.values()) {
            if (ruleTreeType(candidate) !== treeType) continue;
            if (maxLayer && nodeLayer(candidate) > maxLayer) continue;
            // 赠送天赋（selected=true, purchased=false, points=0）不计入门槛统计
            if (candidate.purchased === false) continue;
            total += Number(candidate.points || 0);
        }
        return total;
    }

    function treeGateBlocker(node) {
        const treeType = gateTreeType(node);
        if (!treeType) return '';
        const layer = nodeLayer(node);
        const treeName = treeType === 'class' ? '职业树' : '专精树';
        const deepGatePoints = treeType === 'class' ? TALENT_TREE_RULES.classDeepGatePoints : TALENT_TREE_RULES.specDeepGatePoints;
        if (layer > TALENT_TREE_RULES.deepGateRows && pointsInTree(treeType, TALENT_TREE_RULES.deepGateRows) < deepGatePoints) {
            return `${treeName}前${TALENT_TREE_RULES.deepGateRows}层需要至少 ${deepGatePoints} 点，才能继续点第${TALENT_TREE_RULES.deepGateRows + 1}层及以下天赋`;
        }
        if (layer > TALENT_TREE_RULES.earlyGateRows && pointsInTree(treeType, TALENT_TREE_RULES.earlyGateRows) < TALENT_TREE_RULES.earlyGatePoints) {
            return `${treeName}前${TALENT_TREE_RULES.earlyGateRows}层需要至少 ${TALENT_TREE_RULES.earlyGatePoints} 点，才能继续点第${TALENT_TREE_RULES.earlyGateRows + 1}层及以下天赋`;
        }
        return '';
    }

    function treeRuleBlocker(node, delta = 1) {
        if (delta <= 0) return '';
        const gateBlocker = treeGateBlocker(node);
        if (gateBlocker && Number(node?.points || 0) <= 0) return gateBlocker;
        const treeType = ruleTreeType(node);
        if (!treeType) return '';
        const treeName = treeType === 'class' ? '职业树' : (treeType === 'apex' ? '顶峰天赋' : '专精树');
        const pointCap = TALENT_TREE_RULES.treePointCaps[treeType];
        if (pointCap && pointsInTree(treeType) >= pointCap) {
            return `${treeName}最多只能投入 ${pointCap} 点`;
        }
        return '';
    }

    function selectionIsValid(node) {
        const parents = parentKeysFor(node);
        if (parents.length && !unlockedParentKeys(node).length) return false;
        return !treeGateBlocker(node);
    }

    function canSelect(node) {
        const parents = parentKeysFor(node);
        if (parents.length && !unlockedParentKeys(node).length) return false;
        return !treeRuleBlocker(node, 1);
    }

    function parentNamesFor(node) {
        return parentKeysFor(node)
            .map(parentKey => state.nodes.get(parentKey))
            .filter(Boolean)
            .map(parent => parent.display_name || parent.name || parent.node_key);
    }

    function nodeMaxPoints(node) {
        const baseMax = Math.max(1, Number(node?.max_points || 1));
        const options = node?.choice_options || [];
        if (!options.length || !node?.is_apex_talent || node?.is_choice_node) return baseMax;
        let total = 0;
        const seen = new Set();
        for (const option of options) {
            const identity = option?.node_id || option?.spell_id || option?.option_key || '';
            if (identity && seen.has(identity)) continue;
            if (identity) seen.add(identity);
            total += Number(option?.max_points || 1);
        }
        return Math.max(baseMax, total || baseMax);
    }

    function childKeysFor(node) {
        const children = [];
        for (const [childKey, parentKeys] of state.parentKeysByChild.entries()) {
            if (parentKeys.includes(node.node_key)) children.push(childKey);
        }
        return children;
    }

    function hasSelectedChild(node) {
        for (const [childKey, parentKeys] of state.parentKeysByChild.entries()) {
            if (!parentKeys.includes(node.node_key)) continue;
            if ((state.nodes.get(childKey)?.points || 0) > 0) return true;
        }
        return false;
    }

    function setHoverKey(key) {
        if (state.hoverKey === key) return;
        const previousKey = state.hoverKey;
        state.hoverKey = key || '';
        const relatedKeys = new Set();
        if (state.hoverKey) {
            relatedKeys.add(state.hoverKey);
            for (const parentKey of parentKeysFor(state.nodes.get(state.hoverKey) || {})) relatedKeys.add(parentKey);
            for (const childKey of childKeysFor(state.nodes.get(state.hoverKey) || {})) relatedKeys.add(childKey);
        }
        if (previousKey) {
            relatedKeys.add(previousKey);
            for (const parentKey of parentKeysFor(state.nodes.get(previousKey) || {})) relatedKeys.add(parentKey);
            for (const childKey of childKeysFor(state.nodes.get(previousKey) || {})) relatedKeys.add(childKey);
        }
        document.querySelectorAll('.talent-node-card--tree.is-related').forEach(el => el.classList.remove('is-related'));
        for (const nodeKey of relatedKeys) {
            if (!state.hoverKey) continue;
            document.querySelector(`[data-node-key="${cssEscape(nodeKey)}"]`)?.classList.add('is-related');
        }
        document.querySelectorAll('.talent-path').forEach(el => {
            const parentKey = el.dataset.parentKey || '';
            const childKey = el.dataset.childKey || '';
            el.classList.toggle('is-related', Boolean(state.hoverKey && (state.hoverKey === parentKey || state.hoverKey === childKey)));
        });
    }

    function cssEscape(value) {
        if (window.CSS && typeof window.CSS.escape === 'function') return window.CSS.escape(value);
        return String(value).replace(/(["\\])/g, '\\$1');
    }

    function renderNode(node) {
        const key = node.node_key;
        const btn = document.createElement('button');
        btn.type = 'button';
        const selectable = canSelect(node);
        const related = state.hoverKey && (state.hoverKey === key || parentKeysFor(node).includes(state.hoverKey) || childKeysFor(node).includes(state.hoverKey));
        const stateClass = node.points > 0 ? 'is-selected' : (selectable ? 'is-available' : 'is-locked');
        btn.className = `talent-node-card--tree ${stateClass} ${related ? 'is-related' : ''} ${node.is_choice_node ? 'is-choice-node' : ''} ${node.is_apex_talent ? 'is-apex-talent' : ''}`;
        btn.setAttribute('aria-pressed', node.points > 0 ? 'true' : 'false');
        btn.setAttribute('aria-disabled', selectable ? 'false' : 'true');
        btn.setAttribute('aria-label', nodeAccessibilityLabel(node, selectable));
        btn.dataset.nodeKey = key;
        btn.style.left = `${node.x}px`;
        btn.style.top = `${node.y}px`;
        btn.style.width = `${Math.max(30, Number(node.width || 36))}px`;
        btn.style.height = `${Math.max(30, Number(node.height || 36))}px`;
        btn.innerHTML = iconMarkup(node);
        btn.addEventListener('mouseenter', event => {
            setHoverKey(key);
            showTooltip(node, selectable, event.currentTarget);
        });
        btn.addEventListener('mousemove', event => positionTooltipNear(event.currentTarget));
        btn.addEventListener('mouseleave', () => {
            if (state.hoverKey === key) setHoverKey('');
            hideTooltip(key);
        });
        btn.addEventListener('focus', event => showTooltip(node, selectable, event.currentTarget));
        btn.addEventListener('blur', () => hideTooltip(key));
        btn.addEventListener('click', () => selectNode(node, 1));
        btn.addEventListener('contextmenu', event => {
            event.preventDefault();
            selectNode(node, -1);
        });
        return btn;
    }

    function nodeStateLabel(node, selectable) {
        if (Number(node.points || 0) > 0) return '已选';
        if (selectable) return '可点';
        return '锁定';
    }

    function nodeAccessibilityLabel(node, selectable) {
        const parents = parentNamesFor(node);
        const prefix = `${node.display_name || '未命名天赋'}，${nodeStateLabel(node, selectable)}，${node.points || 0}/${nodeMaxPoints(node)}点`;
        const blocker = treeRuleBlocker(node, 1);
        if (blocker && Number(node.points || 0) <= 0) return `${prefix}，${blocker}`;
        return parents.length ? `${prefix}，前置：${parents.join('或')}` : prefix;
    }

    function tooltipHtml(node, selectable) {
        const parents = parentNamesFor(node);
        const blocker = treeRuleBlocker(node, 1);
        const unlockText = blocker && Number(node.points || 0) <= 0
            ? blocker
            : parents.length
            ? (selectable ? `已满足前置：${parents.filter(name => name).slice(0, 3).join(' / ')}` : `需要前置：${parents.slice(0, 3).join(' / ')}`)
            : '无前置要求';
        const options = choiceOptionsTooltipHtml(node);
        return `<div class="talent-floating-tooltip-name">${escapeHtml(node.display_name)}</div><div class="talent-floating-tooltip-state">${escapeHtml(nodeStateLabel(node, selectable))} · ${escapeHtml(unlockText)}</div><div class="talent-floating-tooltip-desc">${escapeHtml(node.display_desc || '暂无描述')}</div>${options}`;
    }

    function choiceOptionsTooltipHtml(node) {
        const options = node.choice_options || [];
        if (!options.length) return '';
        if (node.is_apex_talent && !node.is_choice_node) {
            return `<div class="talent-floating-tooltip-options talent-floating-tooltip-options--apex" role="list" aria-label="顶峰天赋阶段">
                ${options.map(option => `
                    <div class="talent-floating-tooltip-option is-apex-entry" role="listitem">
                        <img src="${escapeHtml(option.icon_url || node.icon_url)}" alt="">
                        <span><strong>${escapeHtml(option.display_name || '未命名阶段')} · ${Number(option.max_points || 1)}点</strong><small>${escapeHtml(option.display_desc || '暂无描述')}</small></span>
                    </div>
                `).join('')}
            </div>`;
        }
        return `<div class="talent-floating-tooltip-options" role="group" aria-label="二选一天赋选项">
            ${options.map((option, index) => `
                <button type="button" class="talent-floating-tooltip-option ${Number(node.choice_selection || 0) === index ? 'is-active' : ''}" data-option-index="${index}">
                    <img src="${escapeHtml(option.icon_url || node.icon_url)}" alt="">
                    <span><strong>${escapeHtml(option.display_name || '未命名选项')}</strong><small>${escapeHtml(option.display_desc || '暂无描述')}</small></span>
                </button>
            `).join('')}
        </div>`;
    }

    function bindTooltipActions(node, anchorEl) {
        if (!els.tooltipRoot) return;
        els.tooltipRoot.querySelectorAll('[data-option-index]').forEach(btn => {
            btn.addEventListener('click', event => {
                event.preventDefault();
                event.stopPropagation();
                chooseOption(node, Number(btn.dataset.optionIndex || 0));
                const latestNode = state.nodes.get(node.node_key) || node;
                const latestAnchor = els.stageContainer.querySelector(`[data-node-key="${CSS.escape(node.node_key)}"]`) || anchorEl;
                showTooltip(latestNode, canSelect(latestNode), latestAnchor);
            });
        });
    }

    function showTooltip(node, selectable, anchorEl) {
        if (!els.tooltipRoot || !anchorEl) return;
        clearTimeout(state.tooltipHideTimer);
        state.tooltipNodeKey = node.node_key || '';
        els.tooltipRoot.innerHTML = tooltipHtml(node, selectable);
        bindTooltipActions(node, anchorEl);
        els.tooltipRoot.hidden = false;
        positionTooltipNear(anchorEl);
    }

    function hideTooltip(nodeKey, immediate = false) {
        if (!els.tooltipRoot) return;
        if (nodeKey && state.tooltipNodeKey && state.tooltipNodeKey !== nodeKey) return;
        clearTimeout(state.tooltipHideTimer);
        const doHide = () => {
            state.tooltipNodeKey = '';
            els.tooltipRoot.hidden = true;
        };
        if (immediate) {
            doHide();
        } else {
            state.tooltipHideTimer = setTimeout(doHide, 140);
        }
    }

    function positionTooltipNear(anchorEl) {
        if (!els.tooltipRoot || els.tooltipRoot.hidden || !anchorEl) return;
        const rect = anchorEl.getBoundingClientRect();
        const tooltipRect = els.tooltipRoot.getBoundingClientRect();
        const gap = 12;
        const viewportPadding = 10;
        let left = rect.left + rect.width / 2 - tooltipRect.width / 2;
        left = Math.max(viewportPadding, Math.min(left, window.innerWidth - tooltipRect.width - viewportPadding));
        let top = rect.top - tooltipRect.height - gap;
        if (top < viewportPadding) top = rect.bottom + gap;
        els.tooltipRoot.style.left = `${Math.round(left)}px`;
        els.tooltipRoot.style.top = `${Math.round(top)}px`;
    }

    function iconMarkup(node) {
        const max = nodeMaxPoints(node);
        const points = Number(node.points || 0);
        const selectable = canSelect(node);
        const pointsMarkup = `<span class="talent-node-points">${points}/${max}</span><span class="talent-node-state-badge">${escapeHtml(nodeStateLabel(node, selectable))}</span>`;
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
                if (Number(node.points || 0) > 0 && !selectionIsValid(node)) {
                    resetNodeSelection(node);
                    changed = true;
                    didPrune = true;
                }
            }
        } while (changed);
        return didPrune;
    }

    function selectNode(node, delta) {
        hideTooltip(node.node_key);
        state.selectedKey = node.node_key;
        const max = nodeMaxPoints(node);
        // 赠送天赋不能被取消
        if (delta < 0 && node.purchased === false) {
            toast('赠送天赋无法取消');
            updateInspector();
            return;
        }
        const ruleBlocker = treeRuleBlocker(node, delta);
        if (ruleBlocker) {
            toast(ruleBlocker);
            updateInspector();
            return;
        }
        if (delta > 0 && !canSelect(node) && node.points <= 0) {
            const names = parentNamesFor(node);
            toast(names.length ? `需要先点亮前置：${names.slice(0, 2).join(' / ')}` : '需要先点亮前置天赋');
            updateInspector();
            return;
        }
        if (delta < 0 && hasSelectedChild(node)) {
            toast('取消前置天赋会同步清除后续天赋');
        }
        node.points = Math.max(0, Math.min(max, Number(node.points || 0) + delta));
        node.selected = node.points > 0;
        state.profileId = '';
        if (pruneInvalidSelections()) toast('已清除失去前置条件的后续天赋');
        updateInspector();
        renderStage();
        updateCounters();
        scheduleEncode();
    }

    function updateCounters() {
        const totals = {class: 0, hero: 0, spec: 0, apex: 0};
        const granted = {class: 0, hero: 0, spec: 0}; // 赠送天赋点数（单独统计）
        let apexMax = 0;
        for (const node of state.nodes.values()) {
            const pool = node.point_pool || (node.is_apex_talent ? 'apex' : (node.tree_type || 'spec'));
            if (totals[pool] == null) totals[pool] = 0;
            
            const points = Number(node.points || 0);
            if (points > 0) {
                // 赠送天赋单独统计，不计入消耗的点数
                if (node.purchased === false) {
                    if (granted[pool] != null) granted[pool] += points;
                } else {
                    totals[pool] += points;
                }
            }
            if (pool === 'apex') apexMax += nodeMaxPoints(node);
        }
        // 显示时包含赠送天赋的点数
        els.classPoints.textContent = (totals.class || 0) + (granted.class || 0);
        els.heroPoints.textContent = (totals.hero || 0) + (granted.hero || 0);
        els.specPoints.textContent = (totals.spec || 0) + (granted.spec || 0);
        if (els.apexPoints) els.apexPoints.textContent = apexMax ? `${totals.apex || 0}/${apexMax}` : '0/4';
    }

    function updateInspector() {
        if (!els.inspectorContent || !els.inspectorEmpty) return;
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
        const selectable = canSelect(node);
        const parents = parentNamesFor(node);
        const parentText = parents.length ? `<span class="talent-inspector-meta-text">前置：${escapeHtml(parents.slice(0, 2).join(' / '))}</span>` : '';
        const blocker = treeRuleBlocker(node, 1);
        const blockerText = blocker && Number(node.points || 0) <= 0 ? `<span class="talent-inspector-meta-text">${escapeHtml(blocker)}</span>` : '';
        els.inspectorMeta.innerHTML = `<span class="talent-inspector-status talent-inspector-status--${nodeStatusKey(node, selectable)}">${escapeHtml(nodeStateLabel(node, selectable))}</span><span class="talent-inspector-meta-text">${escapeHtml(treeLabel(node.tree_type, node.point_pool))} · ${node.points || 0}/${nodeMaxPoints(node)} 点</span>${blockerText || parentText}`;
        els.inspectorDesc.textContent = '技能说明请悬停天赋图标查看。';
        renderOptions(node);
    }

    function nodeStatusKey(node, selectable) {
        if (Number(node.points || 0) > 0) return 'selected';
        if (selectable) return 'available';
        return 'locked';
    }

    function renderOptions(node) {
        if (!els.inspectorOptions) return;
        const options = node.choice_options || [];
        if (!options.length) {
            els.inspectorOptions.innerHTML = '';
            return;
        }
        if (node.is_apex_talent && !node.is_choice_node) {
            els.inspectorOptions.innerHTML = `<div class="talent-inspector-apex-entries">
                ${options.map(option => `
                    <div class="talent-inspector-option is-apex-entry">
                        <img src="${escapeHtml(option.icon_url || node.icon_url)}" alt="">
                        <span><strong>${escapeHtml(option.display_name || '未命名阶段')} · ${Number(option.max_points || 1)}点</strong><span>${escapeHtml(option.display_desc || '').slice(0, 90)}</span></span>
                    </div>
                `).join('')}
            </div>`;
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
        const wasSelected = Number(node.points || 0) > 0;
        node.choice_selection = index;
        state.profileId = '';
        node.display_spell_id = option.display_spell_id || option.spell_id || node.display_spell_id;
        node.spell_id = option.spell_id || node.spell_id;
        node.icon_url = option.icon_url || node.icon_url;
        node.display_name = option.display_name || node.display_name;
        node.display_desc = option.display_desc || node.display_desc;
        if (wasSelected) {
            node.selected = true;
        } else {
            node.selected = false;
            toast('已切换二选一选项；左键点亮后才计入 build');
        }
        updateInspector();
        renderStage();
        updateCounters();
        scheduleEncode();
    }

    function treeLabel(type, pool) {
        if (pool === 'apex') return '顶峰天赋';
        return {class: '职业树', hero: '英雄树', spec: '专精树'}[type || 'spec'] || type;
    }

    function selectedNodesPayload() {
        const nodes = [];
        for (const node of state.nodes.values()) {
            if (Number(node.points || 0) <= 0) continue;
            const payload = {
                tree_type: node.tree_type || 'spec',
                node_id: node.node_id || null,
                talent_id: node.talent_id || null,
                spell_id: node.spell_id || null,
                display_spell_id: node.display_spell_id || null,
                points: Number(node.points || 0),
            };
            if (node.is_choice_node) payload.choice_selection = Number(node.choice_selection || 0);
            nodes.push(payload);
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
                version: state.versionKey,
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
        if (state.versionKey) params.set('version', state.versionKey);
        if (state.heroSubtree) params.set('hero', state.heroSubtree);
        if (state.profileId) params.set('profile_id', state.profileId);
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
        state.profileId = '';
        loadTree();
    });
    els.importInput.addEventListener('keydown', event => {
        if (event.key === 'Enter') {
            state.buildCode = extractCode(els.importInput.value);
            state.profileId = '';
            loadTree();
        }
    });
    els.resetBtn.addEventListener('click', () => {
        state.buildCode = '';
        state.profileId = '';
        for (const node of state.nodes.values()) {
            // 跳过赠送天赋（purchased=false, selected=true, points=0）
            if (node.purchased === false && node.selected === true && node.points === 0) {
                continue;
            }
            resetNodeSelection(node);
        }
        renderStage();
        updateCounters();
        updateInspector();
        scheduleEncode();
    });
    els.copyUrlBtn.addEventListener('click', () => copyText(location.href));
    els.copyCodeBtn.addEventListener('click', () => copyText(state.buildCode));
    if (els.tooltipRoot) {
        els.tooltipRoot.addEventListener('mouseenter', () => clearTimeout(state.tooltipHideTimer));
        els.tooltipRoot.addEventListener('mouseleave', () => hideTooltip(state.tooltipNodeKey, true));
    }
    window.addEventListener('popstate', () => {
        const params = new URLSearchParams(location.search);
        state.className = params.get('class') || state.className;
        state.specName = params.get('spec') || state.specName;
        state.versionKey = params.get('version') || page.dataset.defaultVersion || state.versionKey;
        state.buildCode = params.get('code') || '';
        state.heroSubtree = params.get('hero') || '';
        initSelects();
        loadTree();
    });

    initSelects();
    loadTree();
})();
