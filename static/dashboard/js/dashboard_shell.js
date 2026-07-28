(() => {
    'use strict';

    function dashboardTarget(item) {
        const section = item.dataset.section || item.dataset.dashboardSection;
        const tool = item.dataset.tool;
        const table = item.dataset.table;
        const params = new URLSearchParams();
        if (table) params.set('table', table);
        else if (tool) params.set('tool', tool);
        else if (section) params.set('section', section);
        const query = params.toString();
        return query ? `/dashboard/?${query}` : '/dashboard/';
    }

    function bindDashboardLinks() {
        document.querySelectorAll('.nav-item:not(.has-submenu)[data-section]').forEach(item => {
            const link = item.querySelector(':scope > a');
            if (link) link.href = dashboardTarget(item);
        });
        document.querySelectorAll('.submenu-item').forEach(item => {
            const link = item.querySelector(':scope > a');
            if (link) link.href = dashboardTarget(item);
        });
    }

    function bindSubmenus() {
        document.querySelectorAll('.nav-item.has-submenu').forEach(item => {
            const link = item.querySelector(':scope > a');
            const submenu = item.querySelector(':scope > .submenu');
            const chevron = link?.querySelector('.fa-chevron-down');
            if (!link || !submenu) return;
            link.addEventListener('click', event => {
                event.preventDefault();
                const willOpen = !item.classList.contains('open');
                item.classList.toggle('open', willOpen);
                link.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
                submenu.style.maxHeight = willOpen ? `${submenu.scrollHeight}px` : '0';
                if (chevron) chevron.classList.toggle('rotate-180', willOpen);
            });
        });
    }

    function bindMobileSidebar() {
        const toggle = document.getElementById('sidebar-toggle');
        const sidebar = document.getElementById('sidebar');
        const overlay = document.getElementById('sidebar-overlay');
        if (!toggle || !sidebar || !overlay) return;

        const close = () => {
            sidebar.classList.remove('open');
            overlay.classList.remove('show');
            document.body.style.overflow = '';
        };
        toggle.addEventListener('click', () => {
            const open = !sidebar.classList.contains('open');
            sidebar.classList.toggle('open', open);
            overlay.classList.toggle('show', open);
            document.body.style.overflow = open ? 'hidden' : '';
        });
        overlay.addEventListener('click', close);
        sidebar.addEventListener('click', event => {
            if (
                window.innerWidth < 1024
                && event.target.closest('.nav-item:not(.has-submenu), .submenu-item')
            ) close();
        });
        document.addEventListener('keydown', event => {
            if (event.key === 'Escape') close();
        });
        window.addEventListener('resize', () => {
            if (window.innerWidth >= 1024) close();
        });
    }

    function csrfToken() {
        return (
            document.querySelector('meta[name="csrf-token"]')?.content
            || document.querySelector('[name=csrfmiddlewaretoken]')?.value
            || ''
        );
    }

    function bindUserMenu() {
        const button = document.getElementById('user-menu-button');
        const menu = document.getElementById('user-menu');
        const logout = document.getElementById('logout-btn');
        if (button && menu) {
            button.addEventListener('click', event => {
                event.stopPropagation();
                menu.classList.toggle('hidden');
            });
            document.addEventListener('click', event => {
                if (!button.contains(event.target) && !menu.contains(event.target)) {
                    menu.classList.add('hidden');
                }
            });
        }
        if (logout) {
            logout.addEventListener('click', async event => {
                event.preventDefault();
                try {
                    const response = await fetch('/auth/logout/', {
                        method: 'POST',
                        credentials: 'same-origin',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-CSRFToken': csrfToken(),
                        },
                    });
                    const result = await response.json();
                    if (!response.ok || result.status !== 'success') {
                        throw new Error(result.message || '登出失败');
                    }
                    window.location.href = result.redirect_url || '/auth/login/';
                } catch (error) {
                    window.alert(error.message || '登出失败，请稍后重试');
                }
            });
        }
    }

    function init() {
        bindDashboardLinks();
        bindSubmenus();
        bindMobileSidebar();
        bindUserMenu();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init, {once: true});
    } else {
        init();
    }
})();
