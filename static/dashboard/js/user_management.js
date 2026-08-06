(() => {
    'use strict';

    const state = { page: 1, pageSize: 25, search: '', users: new Map(), loaded: false, requestId: 0 };
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
            if (!user.is_staff && !user.is_superuser) permissions.appendChild(badge('普通用户', 'bg-gray-100 text-gray-600'));
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
            actions.appendChild(edit);
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
        byId('user-management-is-active').checked = editing ? user.is_active : true;
        byId('user-management-is-staff').checked = editing ? user.is_staff : false;
        byId('user-management-is-superuser').checked = editing ? user.is_superuser : false;
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
        setFormError('');
    }

    async function submitUser(event) {
        event.preventDefault();
        const id = byId('user-management-user-id').value;
        const password = byId('user-management-password').value;
        const payload = {
            username: byId('user-management-username').value,
            email: byId('user-management-email').value,
            first_name: byId('user-management-first-name').value,
            last_name: byId('user-management-last-name').value,
            is_active: byId('user-management-is-active').checked,
            is_staff: byId('user-management-is-staff').checked,
            is_superuser: byId('user-management-is-superuser').checked,
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
        byId('user-management-modal-close').addEventListener('click', closeModal);
        byId('user-management-cancel').addEventListener('click', closeModal);
        byId('user-management-form').addEventListener('submit', submitUser);
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
    });

    window.loadDashboardUsers = loadDashboardUsers;
})();
