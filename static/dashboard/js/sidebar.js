/* 后台侧栏：按工作域组织入口，共用折叠与当前页面定位。 */
(() => {
    'use strict';

    function setOpen(item, open) {
        const link = item?.querySelector(':scope > a');
        const menu = item?.querySelector(':scope > .submenu');
        if (!link || !menu) return;
        item.classList.toggle('open', open);
        link.setAttribute('aria-expanded', String(open));
        menu.hidden = !open;
        menu.style.maxHeight = open ? 'none' : '0';
        link.querySelector('.fa-chevron-down')?.classList.toggle('rotate-180', open);
    }

    function reveal(item) {
        if (!item) return;
        document.querySelectorAll('#sidebar [aria-current]').forEach(link => link.removeAttribute('aria-current'));
        document.querySelectorAll('#sidebar li > a').forEach(link => {
            if (link.parentElement === item) return;
            link.classList.remove('bg-blue-50', 'text-blue-600', 'font-medium');
            if (!link.parentElement.classList.contains('has-submenu')) link.parentElement.classList.remove('active');
        });
        item.querySelector(':scope > a')?.setAttribute('aria-current', 'page');
        let parent = item.parentElement?.closest('.has-submenu');
        while (parent) {
            setOpen(parent, true);
            parent = parent.parentElement?.closest('.has-submenu');
        }
        if (window.innerWidth >= 1024) item.scrollIntoView({block: 'nearest'});
    }

    function initGroups() {
        const root = document.getElementById('dashboard-primary-nav');
        if (!root || root.dataset.grouped === '1') return;
        const section = name => root.querySelector(`.nav-item[data-section="${name}"]`);
        const folder = (key, label, icon, entries) => {
            const members = entries.filter(Boolean);
            if (!members.length) return null;
            const item = document.createElement('li');
            item.className = 'nav-item has-submenu';
            item.dataset.sidebarFolder = key;
            const link = document.createElement('a');
            link.href = '#';
            link.setAttribute('role', 'button');
            link.setAttribute('aria-expanded', 'false');
            link.setAttribute('aria-controls', `sidebar-${key}`);
            const symbol = document.createElement('i');
            symbol.className = `fas ${icon}`;
            symbol.setAttribute('aria-hidden', 'true');
            const title = document.createElement('span');
            title.textContent = label;
            const chevron = document.createElement('i');
            chevron.className = 'fas fa-chevron-down ml-auto text-xs';
            chevron.setAttribute('aria-hidden', 'true');
            link.append(symbol, title, chevron);
            const menu = document.createElement('ul');
            menu.id = `sidebar-${key}`;
            menu.className = 'sidebar-submenu submenu';
            menu.append(...members);
            item.append(link, menu);
            return item;
        };

        const groups = [
            {key: 'content', label: '内容运营', entries: [
                section('class-guides'),
                folder('news', '资讯与报告', 'fa-newspaper', [section('news'), section('wow-daily-reports'), section('wago-hotfix-reports')]),
                folder('site', '站点编排', 'fa-compass', [section('wow-today-settings'), section('portal-navigation')]),
            ]},
            {key: 'tools', label: '游戏工具', entries: [section('simc'), section('mythic-planner'), section('gear-builder-management'), section('tools')]},
            {key: 'system', label: '系统管理', entries: [
                root.querySelector('[data-dashboard-table="MonitorTask"]'),
                folder('logs', '日志与告警', 'fa-bell', [section('error-logs'), section('log-files')]),
                folder('access', '用户与权限', 'fa-users-cog', [section('user-management'), section('user-groups')]),
                section('database-tables'),
            ]},
        ];
        groups.forEach(group => {
            const members = group.entries.filter(Boolean);
            if (!members.length) return;
            const wrapper = document.createElement('li');
            wrapper.className = 'sidebar-group';
            wrapper.dataset.sidebarGroup = group.key;
            const label = document.createElement('div');
            label.className = 'sidebar-group-label';
            label.textContent = group.label;
            label.setAttribute('role', 'heading');
            label.setAttribute('aria-level', '2');
            const menu = document.createElement('ul');
            menu.className = 'sidebar-group-menu';
            menu.setAttribute('aria-label', group.label);
            menu.append(...members);
            wrapper.append(label, menu);
            root.appendChild(wrapper);
        });
        root.dataset.grouped = '1';
        root.addEventListener('click', event => {
            const item = event.target.closest('.nav-item, .submenu-item');
            if (item && !item.classList.contains('has-submenu')) queueMicrotask(() => reveal(item));
        }, true);
    }

    function bindSubmenus() {
        document.querySelectorAll('#sidebar .has-submenu').forEach(item => {
            const link = item.querySelector(':scope > a');
            if (!link || link.dataset.submenuBound === '1') return;
            link.dataset.submenuBound = '1';
            link.setAttribute('role', 'button');
            setOpen(item, false);
            link.addEventListener('click', event => {
                event.preventDefault();
                setOpen(item, !item.classList.contains('open'));
            });
            link.addEventListener('keydown', event => {
                if (event.key === ' ') {
                    event.preventDefault();
                    link.click();
                }
            });
        });
    }

    window.DashboardSidebar = {initGroups, bindSubmenus, setOpen, reveal};
})();
