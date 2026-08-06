(() => {
    'use strict';

    const state = {
        page: 1,
        pageSize: 25,
        search: '',
        users: new Map(),
        loaded: false,
        requestId: 0,
        quickRequestId: 0,
        quickCreating: false,
        resetRequestId: 0,
        resettingPassword: false,
        resetUser: null,
        groups: [],
        editingGroupId: null,
        groupsLoaded: false,
        editingUserGroupIds: [],
    };
    const byId = id => document.getElementById(id);

    function csrfToken() {
        return document.querySelector('meta[name="csrf-token"]')?.content || '';
    }

    function setMessage(text, error = false) {
        const box = byId('user-management-message');
        if (!box) return;
        box.textContent = text || '';
        box.className = text
            ? `mt-4 rounded-lg px-4 py-3 text-sm ${error ? 'bg-red-50 text-red-700' : 'bg-green-50 text-green-700'}`
            : 'hidden mt-4 rounded-lg px-4 py-3 text-sm';
    }

    function setFormError(text) {
        const box = byId('user-management-form-error');
        if (!box) return;
        box.textContent = text || '';
        box.classList.toggle('hidden', !text);
    }

    function formatError(payload) {
        if (payload?.errors) {
            return Object.entries(payload.errors)
                .map(([field, messages]) => `${field}: ${Array.isArray(messages) ? messages.join('；') : messages}`)
                .join('；');
        }
        return payload?.message || '操作失败';
    }

    function textCell(value, className = 'px-4 py-3 text-sm text-gray-700') {
        const td = document.createElement('td');
        td.className = className;
        td.textContent = value ?? '';
        return td;
    }

    function badge(text, classes) {
        const span = document.createElement('span');
        span.className = `inline-flex mr-1 mb-1 px-2 py-1 rounded-full text-xs ${classes}`;
        span.textContent = text;
        return span;
    }

    function renderUsers(payload) {
        const body = byId('user-management-table-body');
        if (!body) return;
        body.replaceChildren();
        state.users.clear();

        payload.data.forEach(user => {
            state.users.set(String(user.id), user);
            const row = document.createElement('tr');
            row.className = 'hover:bg-gray-50';
            row.appendChild(textCell(user.id));

            const identity = document.createElement('td');
            identity.className = 'px-4 py-3 text-sm';
            const username = document.createElement('div');
            username.className = 'font-medium text-gray-900';
            username.textContent = user.username;
            const email = document.createElement('div');
            email.className = 'text-xs text-gray-500';
            email.textContent = user.email || '—';
            identity.append(username, email);
            row.appendChild(identity);

            row.appendChild(textCell(`${user.last_name || ''}${user.first_name || ''}` || '—'));
            const permissions = document.createElement('td');
            permissions.className = 'px-4 py-3 text-sm';
            if (user.is_superuser) permissions.appendChild(badge('超级管理员', 'bg-purple-100 text-purple-700'));
            if (user.is_staff) permissions.appendChild(badge('Staff', 'bg-blue-100 text-blue-700'));
            if (!user.is_staff && !user.is_superuser) permissions.appendChild(badge('普通会员', 'bg-gray-100 text-gray-600'));
            (user.user_groups || []).forEach(group => permissions.appendChild(badge(group.name, 'bg-emerald-100 text-emerald-700')));
            row.appendChild(permissions);

            const status = document.createElement('td');
            status.className = 'px-4 py-3 text-sm';
            status.appendChild(user.is_active
                ? badge('启用', 'bg-green-100 text-green-700')
                : badge('停用', 'bg-red-100 text-red-700'));
            row.appendChild(status);
            row.appendChild(textCell(user.last_login ? new Date(user.last_login).toLocaleString('zh-CN') : '从未登录'));

            const actions = document.createElement('td');
            actions.className = 'px-4 py-3 text-right';
            const edit = document.createElement('button');
            edit.type = 'button';
            edit.className = 'px-3 py-1.5 text-sm text-blue-600 border border-blue-200 rounded-lg hover:bg-blue-50';
            edit.textContent = '编辑';
            edit.addEventListener('click', () => openModal(user));
            const resetPasswordButton = document.createElement('button');
            resetPasswordButton.type = 'button';
            resetPasswordButton.className = 'ml-2 px-3 py-1.5 text-sm text-red-600 border border-red-200 rounded-lg hover:bg-red-50';
            resetPasswordButton.textContent = '重置密码';
            resetPasswordButton.addEventListener('click', () => openPasswordReset(user));
            actions.append(edit, resetPasswordButton);
            row.appendChild(actions);
            body.appendChild(row);
        });

        if (!payload.data.length) {
            const row = document.createElement('tr');
            const cell = textCell('没有匹配的用户', 'px-4 py-10 text-center text-gray-500');
            cell.colSpan = 7;
            row.appendChild(cell);
            body.appendChild(row);
        }
        byId('user-management-page-info').textContent = `第 ${payload.page} / ${payload.total_pages} 页，共 ${payload.total_count} 个用户`;
        renderPagination(payload.page, payload.total_pages);
    }

    function renderPagination(page, totalPages) {
        const box = byId('user-management-pagination');
        box.replaceChildren();
        [['上一页', page - 1, page <= 1], ['下一页', page + 1, page >= totalPages]].forEach(([label, target, disabled]) => {
            const button = document.createElement('button');
            button.type = 'button';
            button.textContent = label;
            button.disabled = disabled;
            button.className = 'px-3 py-1.5 border rounded-lg text-sm disabled:opacity-40';
            button.addEventListener('click', () => {
                state.page = target;
                loadDashboardUsers();
            });
            box.appendChild(button);
        });
    }

    async function loadDashboardUsers() {
        const body = byId('user-management-table-body');
        if (!body) return;
        const params = new URLSearchParams({ page: state.page, page_size: state.pageSize, search: state.search });
        const requestId = ++state.requestId;
        try {
            const response = await fetch('/api/dashboard/users/?' + params.toString(), { headers: { 'Accept': 'application/json' } });
            const payload = await response.json();
            if (requestId !== state.requestId) return;
            if (!response.ok) throw new Error(formatError(payload));
            renderUsers(payload);
            state.loaded = true;
        } catch (error) {
            if (requestId !== state.requestId) return;
            setMessage(error.message || '加载用户失败', true);
        }
    }

    function renderGroupOptions(selectedIds = []) {
        const select = byId('user-management-groups');
        const selected = new Set(selectedIds.map(String));
        select.replaceChildren(new Option('未分组', ''));
        state.groups.forEach(group => {
            if (!group.is_active && !selected.has(String(group.id))) return;
            const option = document.createElement('option');
            option.value = group.id;
            option.textContent = group.name;
            option.selected = selected.has(String(group.id));
            select.appendChild(option);
        });
    }

    function renderGroupList() {
        const list = byId('user-management-group-list');
        list.replaceChildren();
        state.groups.forEach(group => {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'w-full border-b px-4 py-3 text-left hover:bg-gray-50';
            button.textContent = `${group.name} · ${group.user_count} 人${group.is_active ? '' : ' · 已停用'}`;
            button.addEventListener('click', () => editGroup(group));
            list.appendChild(button);
        });
        if (!state.groups.length) list.textContent = '尚未创建用户组';
    }

    async function loadUserGroups() {
        const response = await fetch('/api/dashboard/user-groups/', { headers: { 'Accept': 'application/json' } });
        const payload = await response.json();
        if (!response.ok) throw new Error(formatError(payload));
        state.groups = payload.data;
        state.groupsLoaded = true;
        renderGroupOptions(state.editingUserGroupIds);
        renderGroupList();
    }

    function editGroup(group = null) {
        state.editingGroupId = group?.id || null;
        byId('user-management-group-name').value = group?.name || '';
        byId('user-management-group-description').value = group?.description || '';
        byId('user-management-group-active').checked = group ? group.is_active : true;
        byId('user-management-group-form-title').textContent = group ? '编辑用户组' : '新建用户组';
    }

    async function openGroupModal() {
        try {
            await loadUserGroups();
            editGroup();
            const modal = byId('user-management-group-modal');
            modal.classList.remove('hidden');
            modal.classList.add('flex');
        } catch (error) {
            setMessage(error.message || '加载用户组失败', true);
        }
    }

    function closeGroupModal() {
        const modal = byId('user-management-group-modal');
        modal.classList.add('hidden');
        modal.classList.remove('flex');
    }

    async function submitGroup(event) {
        event.preventDefault();
        const id = state.editingGroupId;
        const response = await fetch(id ? `/api/dashboard/user-groups/${id}/` : '/api/dashboard/user-groups/', {
            method: id ? 'PATCH' : 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
            body: JSON.stringify({ name: byId('user-management-group-name').value, description: byId('user-management-group-description').value, is_active: byId('user-management-group-active').checked }),
        });
        const payload = await response.json();
        if (!response.ok) {
            byId('user-management-group-error').textContent = formatError(payload);
            return;
        }
        byId('user-management-group-error').textContent = '';
        await loadUserGroups();
        editGroup(payload.data);
        await loadDashboardUsers();
    }

    function openModal(user = null) {
        const editing = Boolean(user);
        byId('user-management-modal-title').textContent = editing ? '编辑用户' : '新增用户';
        byId('user-management-user-id').value = editing ? user.id : '';
        byId('user-management-username').value = editing ? user.username : '';
        byId('user-management-email').value = editing ? user.email : '';
        byId('user-management-first-name').value = editing ? user.first_name : '';
        byId('user-management-last-name').value = editing ? user.last_name : '';
        byId('user-management-password').value = '';
        byId('user-management-password').required = !editing;
        byId('user-management-password-label').textContent = editing ? '重置密码（可选）' : '密码';
        byId('user-management-password-help').textContent = editing ? '留空表示保持原密码不变。' : '新增用户必须设置密码。';
        const role = user?.is_superuser ? 'superuser' : (user?.is_staff ? 'staff' : 'member');
        byId('user-management-is-active').checked = editing ? user.is_active : true;
        byId('user-management-role').value = editing ? role : 'member';
        const group = user?.user_groups?.[0];
        state.editingUserGroupIds = group ? [group.id] : [];
        renderGroupOptions(state.editingUserGroupIds);
        setFormError('');
        const modal = byId('user-management-modal');
        modal.classList.remove('hidden');
        modal.classList.add('flex');
    }

    function closeModal() {
        const modal = byId('user-management-modal');
        modal.classList.add('hidden');
        modal.classList.remove('flex');
        byId('user-management-form').reset();
        state.editingUserGroupIds = [];
        setFormError('');
    }

    async function quickCreateUser() {
        const username = byId('user-management-quick-username').value.trim();
        if (!username) {
            byId('user-management-quick-error').textContent = '请输入用户名';
            byId('user-management-quick-error').classList.remove('hidden');
            return;
        }
        const button = byId('user-management-quick-submit');
        const requestId = ++state.quickRequestId;
        state.quickCreating = true;
        button.disabled = true;
        byId('user-management-quick-close').disabled = true;
        byId('user-management-quick-cancel').disabled = true;
        try {
            const response = await fetch('/api/dashboard/users/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
                body: JSON.stringify({ username, quick_create: true }),
            });
            const result = await response.json();
            if (requestId !== state.quickRequestId) return;
            if (!response.ok) throw new Error(formatError(result));
            const data = result.data;
            const explanation = `账号：${data.username}\n密码：${data.generated_password}\n权限：普通会员`;
            byId('user-management-quick-result').textContent = explanation;
            byId('user-management-quick-result').classList.remove('hidden');
            try {
                await copyText(explanation);
                if (requestId !== state.quickRequestId) return;
                byId('user-management-quick-copy-status').textContent = '已自动复制到剪贴板';
            } catch (copyError) {
                if (requestId !== state.quickRequestId) return;
                byId('user-management-quick-copy-status').textContent = '自动复制失败，请点击“复制说明”';
            }
            byId('user-management-quick-copy').disabled = false;
            byId('user-management-quick-copy').onclick = () => copyText(explanation).then(
                () => { byId('user-management-quick-copy-status').textContent = '已复制到剪贴板'; },
                () => { byId('user-management-quick-copy-status').textContent = '复制失败，请手动复制'; },
            );
            setMessage('账号已创建');
            state.page = 1;
            await loadDashboardUsers();
        } catch (error) {
            if (requestId !== state.quickRequestId) return;
            byId('user-management-quick-error').textContent = error.message || '快捷创建失败';
            byId('user-management-quick-error').classList.remove('hidden');
        } finally {
            if (requestId === state.quickRequestId) {
                state.quickCreating = false;
                button.disabled = false;
                byId('user-management-quick-close').disabled = false;
                byId('user-management-quick-cancel').disabled = false;
            }
        }
    }

    function copyText(text) {
        if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text);
        const input = document.createElement('textarea');
        input.value = text;
        input.style.position = 'fixed';
        input.style.opacity = '0';
        document.body.appendChild(input);
        try {
            input.select();
            if (!document.execCommand('copy')) throw new Error('浏览器拒绝复制');
            return Promise.resolve();
        } catch (error) {
            return Promise.reject(error);
        } finally {
            input.remove();
        }
    }

    function clearQuickCreateResult() {
        const result = byId('user-management-quick-result');
        result.textContent = '';
        result.classList.add('hidden');
        const copy = byId('user-management-quick-copy');
        copy.disabled = true;
        copy.onclick = null;
        byId('user-management-quick-copy-status').textContent = '';
    }

    function openQuickCreate() {
        if (state.quickCreating) return;
        state.quickRequestId += 1;
        byId('user-management-quick-username').value = '';
        byId('user-management-quick-error').classList.add('hidden');
        clearQuickCreateResult();
        const modal = byId('user-management-quick-modal');
        modal.classList.remove('hidden');
        modal.classList.add('flex');
        byId('user-management-quick-username').focus();
    }

    function closeQuickCreate() {
        if (state.quickCreating) return;
        state.quickRequestId += 1;
        clearQuickCreateResult();
        byId('user-management-quick-username').value = '';
        const modal = byId('user-management-quick-modal');
        modal.classList.add('hidden');
        modal.classList.remove('flex');
    }

    function clearPasswordResetResult() {
        const result = byId('user-management-reset-result');
        result.textContent = '';
        result.classList.add('hidden');
        const copy = byId('user-management-reset-copy');
        copy.disabled = true;
        copy.onclick = null;
        byId('user-management-reset-copy-status').textContent = '';
    }

    function openPasswordReset(user) {
        if (state.resettingPassword) return;
        state.resetRequestId += 1;
        state.resetUser = { id: user.id, username: user.username };
        clearPasswordResetResult();
        const error = byId('user-management-reset-error');
        error.textContent = '';
        error.classList.add('hidden');
        byId('user-management-reset-confirmation').textContent = `确定重置账号“${user.username}”的密码吗？原密码将立即失效。`;
        byId('user-management-reset-submit').classList.remove('hidden');
        byId('user-management-reset-cancel').textContent = '取消';
        const modal = byId('user-management-reset-modal');
        modal.classList.remove('hidden');
        modal.classList.add('flex');
    }

    function closePasswordReset() {
        if (state.resettingPassword) return;
        state.resetRequestId += 1;
        state.resetUser = null;
        clearPasswordResetResult();
        byId('user-management-reset-error').textContent = '';
        const modal = byId('user-management-reset-modal');
        modal.classList.add('hidden');
        modal.classList.remove('flex');
    }

    async function resetUserPassword() {
        const user = state.resetUser;
        if (!user || state.resettingPassword) return;
        const requestId = ++state.resetRequestId;
        const submit = byId('user-management-reset-submit');
        state.resettingPassword = true;
        submit.disabled = true;
        byId('user-management-reset-close').disabled = true;
        byId('user-management-reset-cancel').disabled = true;
        try {
            const response = await fetch(`/api/dashboard/users/${user.id}/`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
                body: JSON.stringify({ reset_password: true }),
            });
            const result = await response.json();
            if (requestId !== state.resetRequestId) return;
            if (!response.ok) throw new Error(formatError(result));
            const data = result.data;
            const explanation = `账号：${data.username}\n密码：${data.generated_password}`;
            const resultBox = byId('user-management-reset-result');
            resultBox.textContent = explanation;
            resultBox.classList.remove('hidden');
            byId('user-management-reset-confirmation').textContent = '密码已重置。该密码仅显示一次，请立即交付给用户。';
            submit.classList.add('hidden');
            byId('user-management-reset-cancel').textContent = '关闭';
            try {
                await copyText(explanation);
                if (requestId !== state.resetRequestId) return;
                byId('user-management-reset-copy-status').textContent = '已自动复制到剪贴板';
            } catch (copyError) {
                if (requestId !== state.resetRequestId) return;
                byId('user-management-reset-copy-status').textContent = '自动复制失败，请点击“复制账号密码”';
            }
            const copy = byId('user-management-reset-copy');
            copy.disabled = false;
            copy.onclick = () => {
                if (requestId !== state.resetRequestId) return;
                copyText(explanation).then(
                    () => { if (requestId === state.resetRequestId) byId('user-management-reset-copy-status').textContent = '已复制到剪贴板'; },
                    () => { if (requestId === state.resetRequestId) byId('user-management-reset-copy-status').textContent = '复制失败，请手动复制'; },
                );
            };
            setMessage(`账号“${data.username}”的密码已重置`);
        } catch (error) {
            if (requestId !== state.resetRequestId) return;
            const errorBox = byId('user-management-reset-error');
            const unknown = error instanceof TypeError || error.name === 'AbortError';
            errorBox.textContent = unknown
                ? '请求结果未知：密码可能已经重置，请再次点击“重置密码”以获取确定的新密码。'
                : (error.message || '重置密码失败');
            errorBox.classList.remove('hidden');
        } finally {
            if (requestId === state.resetRequestId) {
                state.resettingPassword = false;
                submit.disabled = false;
                byId('user-management-reset-close').disabled = false;
                byId('user-management-reset-cancel').disabled = false;
            }
        }
    }

    async function submitUser(event) {
        event.preventDefault();
        if (!state.groupsLoaded) {
            setFormError('用户组数据尚未加载，暂不能保存用户');
            return;
        }
        const id = byId('user-management-user-id').value;
        const password = byId('user-management-password').value;
        const role = byId('user-management-role').value;
        const payload = {
            username: byId('user-management-username').value,
            email: byId('user-management-email').value,
            first_name: byId('user-management-first-name').value,
            last_name: byId('user-management-last-name').value,
            is_active: byId('user-management-is-active').checked,
            is_staff: role === 'staff' || role === 'superuser',
            is_superuser: role === 'superuser',
            user_group_ids: [...byId('user-management-groups').selectedOptions].filter(option => option.value).map(option => Number(option.value)),
        };
        if (!id || password) payload.password = password;

        const submit = byId('user-management-submit');
        submit.disabled = true;
        try {
            const response = await fetch(id ? `/api/dashboard/users/${id}/` : '/api/dashboard/users/', {
                method: id ? 'PATCH' : 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
                body: JSON.stringify(payload),
            });
            const result = await response.json();
            if (!response.ok) throw new Error(formatError(result));
            closeModal();
            setMessage(id ? '用户已更新' : '用户已创建');
            state.page = 1;
            await loadDashboardUsers();
        } catch (error) {
            setFormError(error.message || '保存失败');
        } finally {
            submit.disabled = false;
        }
    }

    document.addEventListener('DOMContentLoaded', () => {
        if (!byId('user-management')) return;
        byId('user-management-add').addEventListener('click', () => openModal());
        byId('user-management-groups-button').addEventListener('click', openGroupModal);
        byId('user-management-group-close').addEventListener('click', closeGroupModal);
        byId('user-management-group-new').addEventListener('click', () => editGroup());
        byId('user-management-group-form').addEventListener('submit', submitGroup);
        byId('user-management-quick-add').addEventListener('click', openQuickCreate);
        byId('user-management-quick-close').addEventListener('click', closeQuickCreate);
        byId('user-management-quick-cancel').addEventListener('click', closeQuickCreate);
        byId('user-management-quick-form').addEventListener('submit', event => {
            event.preventDefault();
            quickCreateUser();
        });
        byId('user-management-reset-close').addEventListener('click', closePasswordReset);
        byId('user-management-reset-cancel').addEventListener('click', closePasswordReset);
        byId('user-management-reset-submit').addEventListener('click', resetUserPassword);
        byId('user-management-modal-close').addEventListener('click', closeModal);
        byId('user-management-cancel').addEventListener('click', closeModal);
        byId('user-management-form').addEventListener('submit', submitUser);
        byId('user-management-groups').addEventListener('change', event => {
            state.editingUserGroupIds = [...event.target.selectedOptions]
                .map(option => Number(option.value));
        });
        byId('user-management-page-size').addEventListener('change', event => {
            state.pageSize = Number(event.target.value) || 25;
            state.page = 1;
            loadDashboardUsers();
        });
        let timer = null;
        byId('user-management-search').addEventListener('input', event => {
            clearTimeout(timer);
            timer = setTimeout(() => {
                state.search = event.target.value.trim();
                state.page = 1;
                loadDashboardUsers();
            }, 250);
        });
        loadUserGroups().catch(error => setMessage(error.message || '加载用户组失败', true));
    });

    window.loadDashboardUsers = loadDashboardUsers;
})();
