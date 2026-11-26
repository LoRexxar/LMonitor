(() => {
    const setIcon = window.setPublishedWowImage || ((img, src) => { img.src = src; });
    const filter = document.getElementById('filter-inspect');
    const evidence = document.getElementById('event-inspect');
    if (!filter || typeof filter.showModal !== 'function') return;

    const search = filter.querySelector('input');
    const requests = new Map();
    const openers = new Map();
    let filterLink, filterURL, filterAxis, filterPages = {}, debounce;
    let events = [], index = 0;

    async function read(dialog, url, view, render) {
        requests.get(dialog)?.abort();
        const request = new AbortController();
        requests.set(dialog, request);
        const status = dialog.querySelector('[role=status]');
        const retry = dialog.querySelector('[data-retry]');
        retry.hidden = true;
        status.textContent = '正在读取…';
        try {
            const response = await fetch(url, {signal: request.signal, headers: {'X-Combat-View': view}});
            if (!response.ok || response.redirected) throw Error();
            const data = await response.json();
            if (dialog.open && !request.signal.aborted) render(data, status);
        } catch (error) {
            if (request.signal.aborted) return;
            status.textContent = '读取失败，请检查网络或日志访问权限。';
            retry.hidden = false;
        }
    }
    for (const dialog of [filter, evidence]) {
        dialog.querySelector('[data-close]').onclick = () => dialog.close();
        dialog.addEventListener('keydown', event => {
            if (event.key !== 'Escape' || event.isComposing) return;
            event.preventDefault();
            event.stopPropagation();
            dialog.close();
        });
        dialog.addEventListener('close', () => {
            requests.get(dialog)?.abort();
            const opener = openers.get(dialog);
            if (opener?.isConnected && !document.querySelector('dialog[open]')) opener.focus({preventScroll:true});
            if (dialog === filter) clearTimeout(debounce);
        });
        dialog.addEventListener('click', event => {
            if (event.target !== dialog) return;
            const rect = dialog.getBoundingClientRect();
            if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
        });
    }
    function closeAll() {
        if (filter.open) filter.close();
        if (evidence.open) evidence.close();
    }
    window.addEventListener('popstate', closeAll);
    function loadOptions(url) {
        filterURL = url;
        const list = filter.querySelector('.inspect-options');
        list.replaceChildren();
        filterPages = {};
        filter.querySelectorAll('[data-page]').forEach(button => button.disabled = true);
        read(filter, url, 'choices', (data, status) => {
            filterPages = data;
            status.textContent = data.count ? '匹配 ' + data.count + ' 项' : '没有匹配项，可修改名称或编号。';
            for (const row of data.items) {
                const target = new URL(location.href);
                for (const key of ['page','colpage','eventpage','after']) target.searchParams.delete(key);
                target.searchParams.set(filterAxis, row.id);
                target.hash = '';
                const link = document.createElement('a');
                link.href = target;
                link.dataset.applied = filterAxis;
                link.textContent = row.name;
                if (row.icon) {
                    const image = document.createElement('img');
                    image.className = 'combat-entity-icon';
                    setIcon(image, row.icon);
                    image.alt = row.icon_label || '';
                    image.title = image.alt;
                    image.loading = 'lazy';
                    image.width = image.height = 22;
                    link.prepend(image);
                } else if (row.icon_id) {
                    const icon = document.createElement('combat-spell-icon');
                    icon.dataset.spell = row.icon_id;
                    icon.setAttribute('aria-hidden', 'true');
                    link.prepend(icon);
                }
                const hint = document.createElement('small');
                hint.textContent = row.id + ' · ' + row.hits + ' 条事件';
                link.append(hint);
                if (new URL(location.href).searchParams.get(filterAxis) === String(row.id)) link.setAttribute('aria-current','true');
                list.append(link);
            }
            filter.querySelectorAll('[data-page]').forEach(button => button.disabled = !data[button.dataset.page]);
        });
    }
    filter.querySelector('.inspect-options').addEventListener('click', event => {
        if (event.target.closest('a') && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) filter.close();
    });
    search.addEventListener('input', () => {
        clearTimeout(debounce);
        requests.get(filter)?.abort();
        filter.querySelector('.inspect-options').replaceChildren();
        filter.querySelectorAll('[data-page]').forEach(button => button.disabled = true);
        filter.querySelector('[role=status]').textContent = '正在查找…';
        debounce = setTimeout(() => {
            const url = new URL(filterLink.href);
            url.searchParams.set('term',search.value.trim());
            loadOptions(url);
        },180);
    });
    filter.querySelectorAll('[data-page]').forEach(button => button.onclick = () => {
        if (filterPages[button.dataset.page]) loadOptions(new URL(filterPages[button.dataset.page], location.href));
    });
    filter.querySelector('[data-retry]').onclick = () => loadOptions(filterURL);

    function showEvent() {
        const link = events[index];
        const payload = evidence.querySelector('.event-payload');
        payload.hidden = true;
        evidence.querySelector('[data-permalink]').href = link.href;
        evidence.querySelector('[data-event-step="-1"]').disabled = index === 0;
        evidence.querySelector('[data-event-step="1"]').disabled = index === events.length - 1;
        read(evidence, link.href, 'evidence', (data, status) => {
            status.textContent = data.filename + ' · 第 ' + data.line + ' 行 · 本页 ' + (index+1) + '/' + events.length;
            payload.querySelector('h3').textContent = data.ability + ' · ' + (data.kind_label || data.kind);
            payload.querySelector('p').textContent = data.source + ' → ' + data.target + (data.amount == null ? '' : ' · 有效量 ' + data.amount.toLocaleString('zh-CN'));
            payload.querySelector('pre').textContent = data.raw + (data.clipped ? '\n（已截取前 64 KiB）' : '');
            payload.hidden = false;
        });
    }
    evidence.querySelectorAll('[data-event-step]').forEach(button => button.onclick = () => {
        index += Number(button.dataset.eventStep);
        showEvent();
    });
    evidence.querySelector('[data-retry]').onclick = showEvent;
    document.addEventListener('click', event => {
        if (event.button || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
        const link = event.target.closest('a[data-dimension], a[data-evidence]');
        if (!link) return;
        event.preventDefault();
        if (link.hasAttribute('data-evidence')) {
            events = Array.from(document.querySelectorAll('#explorer a[data-evidence]'));
            index = events.indexOf(link);
            openers.set(evidence,link);
            evidence.showModal();
            showEvent();
        } else {
            filterLink = link;
            const url = new URL(link.href);
            filterAxis = url.searchParams.get('group');
            filter.querySelector('h2').textContent = '筛选' + link.dataset.label;
            search.value = '';
            openers.set(filter,link);
            filter.showModal();
            search.focus();
            loadOptions(url);
        }
    }, true);
})();
