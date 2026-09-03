(() => {
    const storageKey = 'lmonitor-portal-theme';
    const root = document.documentElement;
    const iconSprite = '/static/portal/icons/icons.svg';

    const applyTheme = (theme) => {
        const isDark = theme === 'dark';
        root.classList.toggle('portal-theme-dark', isDark);
        root.style.colorScheme = isDark ? 'dark' : 'light';

        const toggle = document.getElementById('portal-theme-toggle');
        if (!toggle) return;
        toggle.setAttribute('aria-pressed', String(isDark));
        toggle.setAttribute('aria-label', isDark ? '切换为浅色模式' : '切换为深色模式');
        toggle.title = isDark ? '切换为浅色模式' : '切换为深色模式';
    };

    const savedTheme = window.localStorage.getItem(storageKey);
    applyTheme(savedTheme === 'dark' ? 'dark' : 'light');

    const navigationDataPromise = fetch('/portal/api/navigation/', {
        headers: { Accept: 'application/json' },
        credentials: 'same-origin',
    })
        .then((response) => response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`)))
        .then((payload) => payload?.data || null)
        .catch(() => null);

    window.getPortalNavigationData = () => navigationDataPromise;

    const toolsDataPromise = fetch('/portal/api/tools/', {
        headers: { Accept: 'application/json' },
        credentials: 'same-origin',
    })
        .then((response) => response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`)))
        .then((payload) => payload?.data || null)
        .catch(() => null);

    window.getPortalToolsData = () => toolsDataPromise;

    const safeIconKey = (value) => {
        const allowed = new Set(['calendar', 'newspaper', 'chart', 'tools', 'chat', 'video', 'refresh', 'globe', 'chevron-down']);
        return allowed.has(String(value || '')) ? String(value) : 'globe';
    };

    const safeUrl = (value) => {
        const raw = String(value || '').trim();
        if (!raw) return '';
        try {
            const parsed = new URL(raw, window.location.origin);
            if (!['http:', 'https:'].includes(parsed.protocol)) return '';
            return parsed.origin === window.location.origin
                ? `${parsed.pathname}${parsed.search}${parsed.hash}`
                : parsed.href;
        } catch (error) {
            return '';
        }
    };

    const makeSvg = (iconKey, className = '') => {
        const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        if (className) svg.setAttribute('class', className);
        svg.setAttribute('aria-hidden', 'true');
        const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
        use.setAttribute('href', `${iconSprite}#icon-${safeIconKey(iconKey)}`);
        svg.appendChild(use);
        return svg;
    };

    const renderPrimaryNavigation = (data) => {
        const nav = document.getElementById('portal-primary-nav');
        const items = Array.isArray(data?.header) ? data.header : [];
        if (!nav || !items.length) return;

        const categories = Array.isArray(data?.categories) ? data.categories : [];
        const metaByKey = new Map(categories.map((item) => [String(item.key || ''), item]));
        const grouped = new Map();
        items.forEach((item) => {
            const key = String(item.category || 'tools');
            if (!grouped.has(key)) grouped.set(key, []);
            grouped.get(key).push(item);
        });

        nav.replaceChildren();
        categories.forEach((category) => {
            const key = String(category.key || '');
            const groupItems = grouped.get(key) || [];
            if (!groupItems.length) return;

            const details = document.createElement('details');
            details.className = 'portal-nav-group';
            details.dataset.navCategory = key;
            const summary = document.createElement('summary');
            summary.className = 'portal-nav-summary';
            summary.appendChild(makeSvg(category.icon_key));
            const label = document.createElement('span');
            label.textContent = String(category.name || key);
            summary.appendChild(label);
            summary.appendChild(makeSvg('chevron-down', 'portal-nav-chevron'));
            const panel = document.createElement('div');
            panel.className = 'portal-nav-panel';

            groupItems.forEach((item) => {
                const href = safeUrl(item.url);
                if (!href) return;
                const link = document.createElement('a');
                link.className = 'portal-quick-nav-link';
                link.href = href;
                const parsed = new URL(href, window.location.origin);
                if (parsed.pathname === window.location.pathname && !parsed.hash) link.classList.add('is-active');
                if (item.open_in_new_tab && parsed.origin !== window.location.origin) {
                    link.target = '_blank';
                    link.rel = 'noreferrer';
                }
                const icon = document.createElement('span');
                icon.className = 'portal-nav-item-icon';
                icon.appendChild(makeSvg(item.icon_key || metaByKey.get(key)?.icon_key));
                const copy = document.createElement('span');
                copy.className = 'portal-nav-item-copy';
                const title = document.createElement('strong');
                title.textContent = String(item.name || '未命名入口');
                copy.appendChild(title);
                if (item.desc) {
                    const desc = document.createElement('small');
                    desc.textContent = String(item.desc);
                    copy.appendChild(desc);
                }
                link.append(icon, copy);
                if (item.badge) {
                    const badge = document.createElement('span');
                    badge.className = 'portal-nav-item-badge';
                    if (item.badge_tone === 'new') badge.classList.add('is-new');
                    badge.textContent = String(item.badge);
                    link.appendChild(badge);
                }
                panel.appendChild(link);
            });
            details.append(summary, panel);
            nav.appendChild(details);
        });

        nav.querySelectorAll('.portal-nav-group').forEach((group) => {
            group.addEventListener('toggle', () => {
                if (!group.open) return;
                document.getElementById('portal-external-links')?.removeAttribute('open');
                nav.querySelectorAll('.portal-nav-group[open]').forEach((other) => {
                    if (other !== group) other.removeAttribute('open');
                });
            });
        });
    };

    const renderExternalLinks = (data) => {
        const menu = document.getElementById('portal-external-links');
        const panel = document.getElementById('portal-external-links-panel');
        if (!menu || !panel) return;
        const items = Array.isArray(data?.items) ? data.items : [];
        const categories = Array.isArray(data?.categories) ? data.categories : [];
        if (!items.length) {
            panel.replaceChildren();
            const empty = document.createElement('p');
            empty.className = 'portal-external-menu-empty';
            empty.textContent = '尚未配置外部链接，可在后台“外部快捷链接”中添加。';
            panel.appendChild(empty);
            return;
        }

        const intro = document.createElement('div');
        intro.className = 'portal-external-menu-intro';
        const introTitle = document.createElement('strong');
        introTitle.textContent = '外部资源';
        const introCopy = document.createElement('span');
        introCopy.textContent = '以下链接将离开 WowDaily，并在新标签页中打开';
        intro.append(introTitle, introCopy);

        const groups = document.createElement('div');
        groups.className = 'portal-external-menu-groups';
        const categoryByKey = new Map(categories.map((item) => [String(item.key || ''), item]));
        const grouped = new Map();
        items.forEach((item) => {
            const key = String(item.category || 'tools');
            if (!grouped.has(key)) grouped.set(key, []);
            grouped.get(key).push(item);
        });
        const orderedKeys = categories.map((item) => String(item.key || '')).filter((key) => grouped.has(key));
        grouped.forEach((_, key) => { if (!orderedKeys.includes(key)) orderedKeys.push(key); });

        orderedKeys.forEach((key) => {
            const category = categoryByKey.get(key) || { name: key, icon_key: 'globe' };
            const section = document.createElement('section');
            section.className = 'portal-external-menu-group';
            const heading = document.createElement('div');
            heading.className = 'portal-external-menu-heading';
            heading.appendChild(makeSvg(category.icon_key));
            const headingText = document.createElement('strong');
            headingText.textContent = String(category.name || key);
            heading.appendChild(headingText);
            const count = document.createElement('span');
            count.textContent = String(grouped.get(key).length);
            heading.appendChild(count);
            const list = document.createElement('div');
            list.className = 'portal-external-menu-list';

            grouped.get(key).forEach((item) => {
                const href = safeUrl(item.url);
                if (!href) return;
                const parsed = new URL(href, window.location.origin);
                if (parsed.origin === window.location.origin) return;
                const link = document.createElement('a');
                link.className = 'portal-external-menu-link';
                link.href = parsed.href;
                link.target = '_blank';
                link.rel = 'noopener noreferrer';
                const visual = document.createElement('span');
                visual.className = 'portal-external-menu-icon';
                visual.appendChild(makeSvg(item.icon_key || category.icon_key || 'globe'));
                const copy = document.createElement('span');
                copy.className = 'portal-external-menu-copy';
                const title = document.createElement('strong');
                title.textContent = String(item.name || '未命名链接');
                copy.appendChild(title);
                if (item.desc) {
                    const desc = document.createElement('small');
                    desc.textContent = String(item.desc);
                    copy.appendChild(desc);
                }
                const arrow = document.createElement('span');
                arrow.className = 'portal-external-menu-arrow';
                arrow.textContent = '↗';
                arrow.setAttribute('aria-hidden', 'true');
                link.append(visual, copy, arrow);
                list.appendChild(link);
            });
            section.append(heading, list);
            groups.appendChild(section);
        });
        panel.replaceChildren(intro, groups);
    };

    document.addEventListener('DOMContentLoaded', () => {
        applyTheme(root.classList.contains('portal-theme-dark') ? 'dark' : 'light');
        document.getElementById('portal-theme-toggle')?.addEventListener('click', () => {
            const nextTheme = root.classList.contains('portal-theme-dark') ? 'light' : 'dark';
            window.localStorage.setItem(storageKey, nextTheme);
            applyTheme(nextTheme);
        });
        navigationDataPromise.then(renderPrimaryNavigation);
        toolsDataPromise.then(renderExternalLinks);
        document.getElementById('portal-external-links')?.addEventListener('toggle', (event) => {
            if (!event.currentTarget.open) return;
            document.querySelectorAll('#portal-primary-nav .portal-nav-group[open]').forEach((group) => group.removeAttribute('open'));
        });
        document.addEventListener('click', (event) => {
            if (!event.target.closest?.('#portal-primary-nav')) {
                document.querySelectorAll('#portal-primary-nav .portal-nav-group[open]').forEach((group) => group.removeAttribute('open'));
            }
            if (!event.target.closest?.('#portal-external-links')) {
                document.getElementById('portal-external-links')?.removeAttribute('open');
            }
        });
    });
})();
