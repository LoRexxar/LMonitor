(() => {
    'use strict';
    const workspace = document.querySelector('.reference-workbench');
    if (!workspace) return;
    const statusBox = document.getElementById('reference-navigation-status');
    let requestNumber = 0, pendingRequest, debounceTimer, slowTimer, cleanup;
    let pendingDestination = null;
    let displayedQuery = location.search;
    const resetStatus = () => {
        clearTimeout(slowTimer);
        statusBox.hidden = true;
        workspace.removeAttribute('aria-busy');
    };
    const showStatus = (url, failed = false) => {
        clearTimeout(slowTimer);
        statusBox.hidden = false;
        const spinner = statusBox.querySelector('.reference-loading-spinner');
        if (spinner) spinner.hidden = failed;
        workspace.setAttribute('aria-busy', String(!failed));
        statusBox.querySelector('[data-reference-loading-label]').textContent = failed ? '资料加载失败' : '正在更新所选配置…';
        statusBox.querySelector('[data-reference-loading-detail]').textContent = failed
            ? '当前仍显示上一次成功加载的配置，请重试。' : '正在读取当前章节及模拟结果。';
        const retry = statusBox.querySelector('[data-reference-loading-retry]');
        retry.href = url;
        retry.hidden = !failed;
        if (!failed) slowTimer = setTimeout(() => {
            statusBox.querySelector('[data-reference-loading-detail]').textContent = '加载时间较长，可继续等待或重试。';
            retry.hidden = false;
        }, 10000);
    };
    const invalidate = () => {
        clearTimeout(debounceTimer);
        pendingRequest?.abort();
        return ++requestNumber;
    };
    const navigate = async (destination, {historyMode = 'push', focusId = ''} = {}) => {
        const number = invalidate();
        const url = new URL(destination, location.href);
        pendingDestination = url.href;
        const controller = pendingRequest = new AbortController();
        showStatus(url.href);
        try {
            const response = await fetch(url, {
                headers: {'X-Simc-Reference': 'workspace'}, credentials: 'same-origin', signal: controller.signal,
            });
            if (number !== requestNumber) return;
            if (response.redirected || !response.ok || !response.headers.get('Content-Type')?.includes('application/json')) {
                throw new Error('资料请求未成功');
            }
            const data = await response.json();
            if (number !== requestNumber) return;
            if (typeof data.html !== 'string' || typeof data.navigation !== 'string' ||
                    !['reference-talents', 'reference-equipment', 'reference-apl'].includes(data.section)) {
                throw new Error('资料响应不完整');
            }
            const canonical = new URL(data.url, location.href);
            if (canonical.origin !== location.origin || canonical.pathname !== location.pathname) throw new Error('资料地址不匹配');
            canonical.hash = url.hash.startsWith('#apl-list-') && data.section === 'reference-apl' ? url.hash : `#${data.section}`;
            cleanup?.();
            workspace.replaceChildren(window.publishedImageFragment(data.html));
            const navigation = document.querySelector('.simc-module-navigation');
            if (navigation) navigation.replaceWith(window.publishedImageFragment(data.navigation));
            if (historyMode === 'push') history.pushState(null, '', canonical);
            else history.replaceState(null, '', canonical);
            displayedQuery = canonical.search;
            pendingDestination = null;
            resetStatus();
            cleanup = initializeWorkspace();
            document.dispatchEvent(new CustomEvent('simc:reference-updated'));
            if (focusId) document.getElementById(focusId)?.focus({preventScroll: true});
        } catch (error) {
            if (number !== requestNumber || error.name === 'AbortError') return;
            workspace.querySelectorAll('form').forEach(form => form.reset());
            pendingDestination = null;
            showStatus(url.href, true);
        }
    };
    window.addEventListener('popstate', () => {
        // 同一配置的 APL 分组历史由 hashchange 还原，无需再次请求后端。
        if (!pendingDestination && location.search === displayedQuery) return;
        navigate(location.href, {historyMode: 'replace'});
    });
    window.addEventListener('pagehide', () => { invalidate(); clearTimeout(slowTimer); });
    window.addEventListener('pageshow', event => { if (event.persisted) resetStatus(); });
    function initializeWorkspace() {
    const lifetime = new AbortController();
    let headerObserver;

    const sections = [...document.querySelectorAll('[data-reference-panel]')];
    const sectionTabs = [...document.querySelectorAll('[data-reference-tab]')];
    let activeSection = sections[0];
    const openSection = (hash) => {
        const id = hash.replace(/^#/, '');
        const target = sections.find(section => section.id === id) ||
            (id.startsWith('apl-list-') ? document.getElementById('reference-apl') : null);
        if (!target) return false;
        if (target.dataset.loaded !== 'true') {
            const destination = new URL(pendingDestination || location.href);
            destination.searchParams.set('view', target.id);
            destination.hash = hash;
            navigate(destination, {focusId: `${target.id}-tab`});
            return true;
        }
        activeSection = target;
        sections.forEach(section => { section.hidden = section !== target; });
        sectionTabs.forEach(tab => {
            const active = tab.hash === `#${target.id}`;
            tab.setAttribute('aria-selected', String(active));
            tab.tabIndex = active ? 0 : -1;
        });
        return true;
    };
    document.querySelector('[data-reference-tabs]')?.setAttribute('role', 'tablist');
    document.querySelector('.reference-workbench')?.classList.add('has-section-tabs');
    sectionTabs.forEach((tab, index) => {
        tab.setAttribute('role', 'tab');
        tab.setAttribute('aria-controls', tab.hash.slice(1));
        tab.addEventListener('click', event => {
            event.preventDefault();
            if (pendingDestination) {
                const destination = new URL(pendingDestination);
                destination.searchParams.set('view', tab.hash.slice(1));
                destination.hash = tab.hash;
                navigate(destination, {focusId: tab.id});
            } else if (openSection(tab.hash) && activeSection.id === tab.hash.slice(1)) {
                history.replaceState(null, '', tab.href);
                displayedQuery = location.search;
            }
        });
        tab.addEventListener('keydown', event => {
            let next;
            if (event.key === 'ArrowRight') next = (index + 1) % sectionTabs.length;
            else if (event.key === 'ArrowLeft') next = (index + sectionTabs.length - 1) % sectionTabs.length;
            else if (event.key === 'Home') next = 0;
            else if (event.key === 'End') next = sectionTabs.length - 1;
            else return;
            event.preventDefault();
            sectionTabs[next].focus();
            sectionTabs[next].click();
        });
    });
    sections.forEach(section => {
        section.setAttribute('role', 'tabpanel');
        section.setAttribute('aria-labelledby', `${section.id}-tab`);
        section.tabIndex = 0;
    });
    const restoreSection = () => {
        if (!openSection(location.hash) &&
            !openSection(new URLSearchParams(location.search).get('view') || 'reference-talents')) {
            openSection('reference-talents');
        }
    };
    restoreSection();
    window.addEventListener('hashchange', () => {
        // 浏览器前后退会同时触发 popstate 和 hashchange，共用同一次加载。
        if (pendingDestination !== location.href) restoreSection();
    }, {signal: lifetime.signal});
    const header = document.querySelector('.portal-header');
    if (header && window.ResizeObserver) {
        headerObserver = new ResizeObserver(() => {
            document.querySelector('.simc-reference').style.setProperty('--reference-header-height', `${header.getBoundingClientRect().height}px`);
        });
        headerObserver.observe(header);
    }
    document.querySelectorAll('.simc-reference form').forEach(form => {
        const destination = () => {
            const url = new URL(location.pathname, location.href);
            url.search = new URLSearchParams(new FormData(form)).toString();
            url.searchParams.set('view', activeSection.id);
            url.hash = activeSection.id;
            return url;
        };
        form.addEventListener('change', event => {
            if (!event.target.matches('select, input[type="checkbox"]')) return;
            const url = destination();
            invalidate();
            pendingDestination = url.href;
            showStatus(url.href);
            const focusId = event.target.id;
            // 合并连续勾选；旧响应即使无法取消，也不能覆盖新选择。
            debounceTimer = setTimeout(() => navigate(url, {focusId}), event.target.type === 'checkbox' ? 180 : 0);
        });
        form.addEventListener('submit', event => {
            event.preventDefault();
            navigate(destination());
        });
        const submit = form.querySelector('button[type="submit"]');
        if (submit) submit.hidden = true;
    });
    document.querySelectorAll('.simc-reference img').forEach((img) => {
        const fallback = () => {
            const placeholder = document.createElement('span');
            placeholder.className = 'reference-image-fallback';
            placeholder.textContent = '?';
            placeholder.setAttribute('aria-hidden', 'true');
            placeholder.style.width = `${img.width || 24}px`;
            placeholder.style.height = `${img.height || 24}px`;
            img.replaceWith(placeholder);
        };
        img.addEventListener('error', fallback, {once: true});
        if (img.complete && !img.naturalWidth) fallback();
    });
    const status = document.getElementById('reference-copy-status');
    let statusTimer;
    document.querySelectorAll('[data-copy]').forEach((button) => {
        button.hidden = false;
        button.addEventListener('click', async () => {
            const source = document.getElementById(button.dataset.copy);
            const text = source.value ?? source.textContent;
            try {
                await navigator.clipboard.writeText(text);
                status.textContent = `${button.textContent.replace('复制', '')}已复制。`;
                clearTimeout(statusTimer);
                statusTimer = setTimeout(() => { status.textContent = ''; }, 4000);
            } catch (_) {
                for (let parent = source.parentElement; parent; parent = parent.parentElement) {
                    if (parent.tagName === 'DETAILS') parent.open = true;
                }
                if (source.select) source.select();
                else {
                    const range = document.createRange();
                    range.selectNodeContents(source);
                    const selection = window.getSelection();
                    selection.removeAllRanges();
                    selection.addRange(range);
                }
                source.focus();
                status.textContent = '自动复制不可用，已选中文本，请按 Ctrl+C 或使用系统复制。';
            }
        });
    });
    const panels = [...document.querySelectorAll('[data-apl-panel]')];
    const tabs = [...document.querySelectorAll('[data-apl-tab]')];
    const tablist = document.querySelector('[data-apl-tabs]');
    const narrowScenes = window.matchMedia('(max-width: 640px)');
    const updateSceneOrientation = () => tablist?.setAttribute('aria-orientation', narrowScenes.matches ? 'horizontal' : 'vertical');
    if (tablist) tablist.setAttribute('role', 'tablist');
    updateSceneOrientation();
    narrowScenes.addEventListener('change', updateSceneOrientation, {signal: lifetime.signal});
    tabs.forEach(tab => {
        tab.setAttribute('role', 'tab');
        tab.setAttribute('aria-controls', tab.hash.slice(1));
    });
    panels.forEach(panel => {
        panel.setAttribute('role', 'tabpanel');
        panel.setAttribute('aria-labelledby', `${panel.id}-tab`);
        panel.tabIndex = 0;
    });
    const openList = (hash) => {
        const target = document.getElementById(hash.replace(/^#/, ''));
        if (!panels.includes(target)) return false;
        panels.forEach(panel => { panel.hidden = panel !== target; });
        tabs.forEach(tab => {
            const active = tab.hash === `#${target.id}`;
            tab.setAttribute('aria-selected', String(active));
            tab.tabIndex = active ? 0 : -1;
        });
        return true;
    };
    tabs.forEach((tab, index) => tab.addEventListener('keydown', event => {
        if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
        const offsets = {ArrowRight: 1, ArrowLeft: -1, ArrowDown: 1, ArrowUp: -1};
        let next;
        if (event.key in offsets) next = (index + offsets[event.key] + tabs.length) % tabs.length;
        else if (event.key === 'Home') next = 0;
        else if (event.key === 'End') next = tabs.length - 1;
        else return;
        event.preventDefault();
        tabs[next].focus();
        tabs[next].click();
    }));
    document.querySelectorAll('[data-apl-jump]').forEach(link => {
        link.addEventListener('click', event => {
            if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
            if (!openList(link.hash)) return;
            event.preventDefault();
            if (window.location.hash !== link.hash) history.pushState(null, '', link.hash);
            if (!link.hasAttribute('data-apl-tab')) {
                const rule = document.getElementById(link.dataset.aplRule);
                if (rule?.classList.contains('reference-rule')) rule.open = true;
                const destination = rule || panels.find(panel => panel.id === link.hash.slice(1));
                if (destination) {
                    if (!destination.hasAttribute('tabindex')) destination.tabIndex = -1;
                    destination.focus({preventScroll: true});
                    destination.scrollIntoView({block: 'start'});
                }
            }
        });
    });
    const restoreList = () => {
        if (!openList(window.location.hash)) {
            const initial = panels.find(panel => panel.hasAttribute('data-apl-default')) || panels[0];
            if (initial) openList(`#${initial.id}`);
        }
    };
    window.addEventListener('hashchange', restoreList, {signal: lifetime.signal});
    restoreList();
    return () => { lifetime.abort(); headerObserver?.disconnect(); clearTimeout(statusTimer); };
    }
    cleanup = initializeWorkspace();
})();
