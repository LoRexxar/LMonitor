(function () {
    'use strict';

    const state = { records: [], loaded: false, loading: false, dirty: false };

    function root() {
        return document.getElementById('portal-navigation');
    }

    function escapeHtml(value) {
        return String(value ?? '')
            .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
            .replaceAll('"', '&quot;').replaceAll("'", '&#039;');
    }

    function csrfToken() {
        return document.querySelector('#csrf-form input[name="csrfmiddlewaretoken"]')?.value || '';
    }

    function setMessage(message, tone = 'info') {
        const node = document.getElementById('portal-navigation-message');
        if (!node) return;
        node.textContent = message || '';
        node.className = 'hidden border-b px-5 py-3 text-sm';
        if (!message) return;
        node.classList.remove('hidden');
        const classes = tone === 'error'
            ? ['border-red-100', 'bg-red-50', 'text-red-700']
            : tone === 'success'
                ? ['border-emerald-100', 'bg-emerald-50', 'text-emerald-700']
                : ['border-blue-100', 'bg-blue-50', 'text-blue-700'];
        node.classList.add(...classes);
    }

    function setBusy(value) {
        state.loading = value;
        ['portal-navigation-save', 'portal-navigation-refresh', 'portal-navigation-add-group'].forEach(id => {
            const button = document.getElementById(id);
            if (button) button.disabled = value;
        });
    }

    function renderSummary() {
        const node = document.getElementById('portal-navigation-summary');
        if (!node) return;
        const items = state.records.flatMap(group => group.items || []);
        const visible = items.filter(item => item.is_active).length;
        node.textContent = `${state.records.length} 个分组 · ${items.length} 个站内入口 · ${visible} 个已启用${state.dirty ? ' · 有未保存更改' : ''}`;
    }

    function moveButtons(kind, index, length) {
        return `<div class="inline-flex shrink-0 overflow-hidden rounded-lg border border-gray-300 bg-white">
            <button type="button" data-move="up" data-kind="${kind}" class="h-10 w-10 text-gray-600 hover:bg-gray-50 disabled:text-gray-300" aria-label="上移" ${index === 0 ? 'disabled' : ''}><i class="fas fa-arrow-up"></i></button>
            <button type="button" data-move="down" data-kind="${kind}" class="h-10 w-10 border-l border-gray-300 text-gray-600 hover:bg-gray-50 disabled:text-gray-300" aria-label="下移" ${index === length - 1 ? 'disabled' : ''}><i class="fas fa-arrow-down"></i></button>
        </div>`;
    }

    function itemTemplate(item, groupIndex, itemIndex, itemCount) {
        return `<article class="rounded-lg border border-gray-200 bg-white p-4" data-group-index="${groupIndex}" data-item-index="${itemIndex}">
            <div class="flex flex-col gap-3 xl:flex-row xl:items-start">
                <div class="grid min-w-0 flex-1 gap-3 md:grid-cols-2 xl:grid-cols-12">
                    <label class="block xl:col-span-3"><span class="mb-1 block text-xs font-medium text-gray-600">入口名称</span><input data-item-field="name" maxlength="200" value="${escapeHtml(item.name)}" class="min-h-10 w-full rounded-lg border border-gray-300 px-3 py-2 text-sm" placeholder="例如：大秘境分数线"></label>
                    <label class="block xl:col-span-4"><span class="mb-1 block text-xs font-medium text-gray-600">站内地址</span><input data-item-field="url" maxlength="1000" value="${escapeHtml(item.url)}" class="min-h-10 w-full rounded-lg border border-gray-300 px-3 py-2 font-mono text-sm" placeholder="/#section-mplus-cutoffs"></label>
                    <label class="block xl:col-span-3"><span class="mb-1 block text-xs font-medium text-gray-600">说明</span><input data-item-field="desc" maxlength="500" value="${escapeHtml(item.desc)}" class="min-h-10 w-full rounded-lg border border-gray-300 px-3 py-2 text-sm" placeholder="用于卡片辅助说明"></label>
                    <label class="block xl:col-span-2"><span class="mb-1 block text-xs font-medium text-gray-600">图标键</span><input data-item-field="icon_key" maxlength="48" value="${escapeHtml(item.icon_key)}" class="min-h-10 w-full rounded-lg border border-gray-300 px-3 py-2 text-sm" placeholder="继承分组"></label>
                    <label class="block xl:col-span-3"><span class="mb-1 block text-xs font-medium text-gray-600">徽标文字</span><input data-item-field="badge" maxlength="32" value="${escapeHtml(item.badge)}" class="min-h-10 w-full rounded-lg border border-gray-300 px-3 py-2 text-sm" placeholder="常用 / 新"></label>
                    <label class="block xl:col-span-3"><span class="mb-1 block text-xs font-medium text-gray-600">徽标样式</span><select data-item-field="badge_tone" class="min-h-10 w-full rounded-lg border border-gray-300 px-3 py-2 text-sm"><option value="default" ${item.badge_tone !== 'new' ? 'selected' : ''}>常规</option><option value="new" ${item.badge_tone === 'new' ? 'selected' : ''}>新内容（红色）</option></select></label>
                    <div class="flex flex-wrap items-end gap-x-5 gap-y-2 xl:col-span-6">
                        <label class="inline-flex min-h-10 items-center gap-2 text-sm text-gray-700"><input type="checkbox" data-item-field="show_in_header" class="h-5 w-5 rounded border-gray-300 text-blue-600" ${item.show_in_header ? 'checked' : ''}>顶部导航</label>
                        <label class="inline-flex min-h-10 items-center gap-2 text-sm text-gray-700"><input type="checkbox" data-item-field="show_in_home_guide" class="h-5 w-5 rounded border-gray-300 text-blue-600" ${item.show_in_home_guide ? 'checked' : ''}>首页入口卡片</label>
                        <label class="inline-flex min-h-10 items-center gap-2 text-sm text-gray-700"><input type="checkbox" data-item-field="is_active" class="h-5 w-5 rounded border-gray-300 text-blue-600" ${item.is_active ? 'checked' : ''}>启用</label>
                    </div>
                </div>
                <div class="flex items-center gap-2 xl:pt-5">
                    ${moveButtons('item', itemIndex, itemCount)}
                    <button type="button" data-remove-item class="h-10 w-10 rounded-lg border border-red-200 text-red-600 hover:bg-red-50" aria-label="删除入口"><i class="fas fa-trash"></i></button>
                </div>
            </div>
        </article>`;
    }

    function groupTemplate(group, groupIndex) {
        const items = Array.isArray(group.items) ? group.items : [];
        return `<section class="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm" data-group-index="${groupIndex}">
            <div class="border-b border-gray-200 p-4">
                <div class="flex flex-col gap-3 xl:flex-row xl:items-end">
                    <div class="grid min-w-0 flex-1 gap-3 sm:grid-cols-2 xl:grid-cols-12">
                        <label class="block xl:col-span-3"><span class="mb-1 block text-xs font-medium text-gray-600">分组名称</span><input data-group-field="name" maxlength="100" value="${escapeHtml(group.name)}" class="min-h-10 w-full rounded-lg border border-gray-300 px-3 py-2 text-sm"></label>
                        <label class="block xl:col-span-2"><span class="mb-1 block text-xs font-medium text-gray-600">稳定标识</span><input data-group-field="key" maxlength="64" value="${escapeHtml(group.key)}" class="min-h-10 w-full rounded-lg border border-gray-300 px-3 py-2 font-mono text-sm"></label>
                        <label class="block xl:col-span-4"><span class="mb-1 block text-xs font-medium text-gray-600">分组说明</span><input data-group-field="description" maxlength="300" value="${escapeHtml(group.description)}" class="min-h-10 w-full rounded-lg border border-gray-300 px-3 py-2 text-sm"></label>
                        <label class="block xl:col-span-2"><span class="mb-1 block text-xs font-medium text-gray-600">图标键</span><input data-group-field="icon_key" maxlength="48" value="${escapeHtml(group.icon_key)}" class="min-h-10 w-full rounded-lg border border-gray-300 px-3 py-2 text-sm"></label>
                        <label class="inline-flex min-h-10 items-center gap-2 pb-0.5 text-sm text-gray-700 xl:col-span-1"><input type="checkbox" data-group-field="is_active" class="h-5 w-5 rounded border-gray-300 text-blue-600" ${group.is_active ? 'checked' : ''}>启用</label>
                    </div>
                    <div class="flex flex-wrap items-center gap-2">
                        <span class="mr-auto text-xs text-gray-500 xl:mr-0">${items.length} 个入口</span>
                        <button type="button" data-add-item class="min-h-10 rounded-lg border border-blue-200 px-3 py-2 text-sm font-medium text-blue-700 hover:bg-blue-50"><i class="fas fa-plus mr-1"></i>新增入口</button>
                        ${moveButtons('group', groupIndex, state.records.length)}
                        <button type="button" data-remove-group class="h-10 w-10 rounded-lg border border-red-200 text-red-600 hover:bg-red-50" aria-label="删除分组"><i class="fas fa-trash"></i></button>
                    </div>
                </div>
            </div>
            <div class="space-y-3 bg-gray-50/70 p-3 sm:p-4">
                ${items.length ? items.map((item, itemIndex) => itemTemplate(item, groupIndex, itemIndex, items.length)).join('') : '<div class="rounded-lg border border-dashed border-gray-300 bg-white px-4 py-7 text-center text-sm text-gray-500">该分组还没有入口</div>'}
            </div>
        </section>`;
    }

    function render() {
        const list = document.getElementById('portal-navigation-list');
        const empty = document.getElementById('portal-navigation-empty');
        if (!list || !empty) return;
        list.innerHTML = state.records.map(groupTemplate).join('');
        list.classList.toggle('hidden', !state.records.length);
        empty.classList.toggle('hidden', Boolean(state.records.length));
        renderSummary();
    }

    function markDirty() {
        state.dirty = true;
        renderSummary();
    }

    async function load(force = false) {
        if ((state.loaded && !force) || state.loading) return;
        setBusy(true);
        setMessage('正在读取首页导航…');
        try {
            const response = await fetch(root().dataset.apiUrl, { headers: { Accept: 'application/json' }, credentials: 'same-origin' });
            const payload = await response.json();
            if (!response.ok || !payload.success) throw new Error(payload.error || `读取失败（${response.status}）`);
            state.records = payload.records || [];
            state.loaded = true;
            state.dirty = false;
            setMessage('');
            render();
        } catch (error) {
            setMessage(error.message || '读取首页导航失败', 'error');
        } finally {
            setBusy(false);
        }
    }

    async function save() {
        if (state.loading) return;
        setBusy(true);
        setMessage('正在保存导航配置…');
        try {
            const response = await fetch(root().dataset.apiUrl, {
                method: 'PATCH', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-CSRFToken': csrfToken() },
                body: JSON.stringify({ groups: state.records }),
            });
            const payload = await response.json();
            if (!response.ok || !payload.success) throw new Error(payload.error || `保存失败（${response.status}）`);
            state.records = payload.records || [];
            state.dirty = false;
            render();
            setMessage('首页导航已保存；刷新 Portal 后即可看到新配置。', 'success');
        } catch (error) {
            setMessage(error.message || '保存首页导航失败', 'error');
        } finally {
            setBusy(false);
        }
    }

    function itemAt(target) {
        const node = target.closest('[data-item-index]');
        const groupIndex = Number(node?.dataset.groupIndex);
        const itemIndex = Number(node?.dataset.itemIndex);
        return { groupIndex, itemIndex, item: state.records[groupIndex]?.items?.[itemIndex] };
    }

    function groupAt(target) {
        const node = target.closest('[data-group-index]');
        const groupIndex = Number(node?.dataset.groupIndex);
        return { groupIndex, group: state.records[groupIndex] };
    }

    function bindEvents() {
        const section = root();
        if (!section || section.dataset.bound === 'true') return;
        section.dataset.bound = 'true';
        document.getElementById('portal-navigation-save')?.addEventListener('click', save);
        document.getElementById('portal-navigation-refresh')?.addEventListener('click', () => {
            if (state.dirty && !window.confirm('放弃尚未保存的首页导航更改？')) return;
            state.loaded = false;
            load(true);
        });
        document.getElementById('portal-navigation-add-group')?.addEventListener('click', () => {
            state.records.push({ id: null, key: `group-${Date.now().toString(36)}`, name: '新分组', description: '', icon_key: 'globe', is_active: true, items: [] });
            markDirty(); render();
        });
        section.addEventListener('input', event => {
            const field = event.target.dataset.groupField;
            if (field) {
                const { group } = groupAt(event.target);
                if (!group || event.target.type === 'checkbox') return;
                group[field] = event.target.value; markDirty(); return;
            }
            const itemField = event.target.dataset.itemField;
            if (itemField) {
                const { item } = itemAt(event.target);
                if (!item || event.target.type === 'checkbox' || event.target.tagName === 'SELECT') return;
                item[itemField] = event.target.value; markDirty();
            }
        });
        section.addEventListener('change', event => {
            const field = event.target.dataset.groupField;
            if (field) {
                const { group } = groupAt(event.target);
                if (!group) return;
                group[field] = event.target.type === 'checkbox' ? event.target.checked : event.target.value; markDirty(); return;
            }
            const itemField = event.target.dataset.itemField;
            if (itemField) {
                const { item } = itemAt(event.target);
                if (!item) return;
                item[itemField] = event.target.type === 'checkbox' ? event.target.checked : event.target.value; markDirty();
            }
        });
        section.addEventListener('click', event => {
            const add = event.target.closest('[data-add-item]');
            if (add) {
                const { group } = groupAt(add);
                group.items.push({ id: null, name: '新入口', url: '/#section-', desc: '', icon_key: '', badge: '', badge_tone: 'default', show_in_header: false, show_in_home_guide: true, is_active: true });
                markDirty(); render(); return;
            }
            const removeItem = event.target.closest('[data-remove-item]');
            if (removeItem) {
                const { groupIndex, itemIndex, item } = itemAt(removeItem);
                if (!window.confirm(`删除入口“${item.name}”？保存后生效。`)) return;
                state.records[groupIndex].items.splice(itemIndex, 1); markDirty(); render(); return;
            }
            const removeGroup = event.target.closest('[data-remove-group]');
            if (removeGroup) {
                const { groupIndex, group } = groupAt(removeGroup);
                if (!window.confirm(`删除分组“${group.name}”及其中全部入口？保存后生效。`)) return;
                state.records.splice(groupIndex, 1); markDirty(); render(); return;
            }
            const move = event.target.closest('[data-move]');
            if (!move) return;
            if (move.dataset.kind === 'group') {
                const { groupIndex } = groupAt(move);
                const next = move.dataset.move === 'up' ? groupIndex - 1 : groupIndex + 1;
                if (next < 0 || next >= state.records.length) return;
                [state.records[groupIndex], state.records[next]] = [state.records[next], state.records[groupIndex]];
            } else {
                const { groupIndex, itemIndex } = itemAt(move);
                const items = state.records[groupIndex].items;
                const next = move.dataset.move === 'up' ? itemIndex - 1 : itemIndex + 1;
                if (next < 0 || next >= items.length) return;
                [items[itemIndex], items[next]] = [items[next], items[itemIndex]];
            }
            markDirty(); render();
        });
    }

    window.loadPortalNavigationManagement = function () { bindEvents(); return load(false); };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bindEvents, { once: true });
    else bindEvents();
})();
