(() => {
    'use strict';
    const sections = [...document.querySelectorAll('[data-reference-panel]')];
    const sectionTabs = [...document.querySelectorAll('[data-reference-tab]')];
    let activeSection = sections[0];
    const openSection = (hash) => {
        const id = hash.replace(/^#/, '');
        const target = sections.find(section => section.id === id) ||
            (id.startsWith('apl-list-') ? document.getElementById('reference-apl') : null);
        if (!target) return false;
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
            openSection(tab.hash);
            history.pushState(null, '', tab.hash);
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
    window.addEventListener('hashchange', restoreSection);
    const header = document.querySelector('.portal-header');
    if (header && window.ResizeObserver) {
        new ResizeObserver(() => {
            document.querySelector('.simc-reference').style.setProperty('--reference-header-height', `${header.getBoundingClientRect().height}px`);
        }).observe(header);
    }
    const navigationStatus = document.getElementById('reference-navigation-status');
    const workbench = document.querySelector('.reference-workbench');
    let navigationTimer;
    const resetNavigationStatus = () => {
        clearTimeout(navigationTimer);
        if (navigationStatus) navigationStatus.hidden = true;
        workbench?.removeAttribute('aria-busy');
    };
    window.addEventListener('pageshow', event => {
        if (event.persisted) resetNavigationStatus();
    });
    window.addEventListener('pagehide', () => clearTimeout(navigationTimer));
    const showNavigationStatus = form => {
        if (!navigationStatus) return;
        clearTimeout(navigationTimer);
        const selected = form.querySelector('select')?.selectedOptions[0]?.textContent.trim();
        navigationStatus.querySelector('[data-reference-loading-label]').textContent = selected
            ? `正在加载：${selected}` : '正在应用英雄天赋筛选…';
        const detail = navigationStatus.querySelector('[data-reference-loading-detail]');
        detail.textContent = '正在读取所选配置，请稍候。';
        const retry = navigationStatus.querySelector('[data-reference-loading-retry]');
        const destination = new URL(form.action, location.href);
        destination.search = new URLSearchParams(new FormData(form)).toString();
        retry.href = destination.href;
        retry.hidden = true;
        navigationStatus.hidden = false;
        workbench?.setAttribute('aria-busy', 'true');
        // Leave successful form controls enabled: native GET serialization
        // happens after submit listeners and must retain every selected value.
        navigationTimer = setTimeout(() => {
            detail.textContent = '加载时间较长，可继续等待或重新加载。';
            retry.hidden = false;
        }, 10000);
    };
    document.querySelectorAll('.simc-reference form').forEach(form => {
        form.addEventListener('change', event => {
            if (event.target.matches('select, input[type="checkbox"]')) form.requestSubmit();
        });
        form.addEventListener('submit', () => {
            let view = form.querySelector('input[name="view"]');
            if (!view) {
                view = document.createElement('input');
                view.type = 'hidden';
                view.name = 'view';
                form.append(view);
            }
            view.value = activeSection.id;
            form.action = `#${activeSection.id}`;
            showNavigationStatus(form);
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
    narrowScenes.addEventListener('change', updateSceneOrientation);
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
    window.addEventListener('hashchange', () => openList(window.location.hash));
    if (!openList(window.location.hash)) {
        const initial = panels.find(panel => panel.hasAttribute('data-apl-default')) || panels[0];
        if (initial) openList(`#${initial.id}`);
    }
})();
