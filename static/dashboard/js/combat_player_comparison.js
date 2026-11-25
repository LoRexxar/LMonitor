(() => {
    const form = document.getElementById('duel-form');
    if (!form) return;
    const pending = new Map();
    const failures = new Set();
    const submit = form.querySelector('button[type=submit]');
    const status = form.querySelector('.duel-status');
    const discard = form.querySelector('[data-discard]');

    function enableSearch(container) {
        for (const label of container.querySelectorAll('.duel-search')) label.hidden = false;
    }
    function syncStatus() {
        const missing = ['left','right'].filter(key => !form.querySelector('input[name=' + key + ']:checked'));
        submit.disabled = Boolean(missing.length || pending.size || failures.size);
        if (failures.size) status.textContent = '角色列表未能更新，请重试。';
        else if (pending.size) status.textContent = '正在读取所选日志的角色…';
        else if (missing.length) status.textContent = '请选择角色 ' + missing.map(key => key === 'left' ? 'A' : 'B').join(' 和 ') + '。';
        else status.textContent = discard && !discard.hidden ? '下方保留上次结果，提交后更新。' : '';
    }
    enableSearch(form);
    if (discard) {
        const pair = form.querySelector('.duel-pair');
        const original = pair.cloneNode(true);
        discard.onclick = event => {
            event.preventDefault();
            for (const controller of pending.values()) controller.abort();
            pending.clear();
            failures.clear();
            form.reset();
            pair.replaceChildren(...original.cloneNode(true).children);
            discard.hidden = true;
            syncStatus();
            const setup = form.closest('details');
            setup.open = false;
            setup.querySelector('summary').focus();
        };
    }
    syncStatus();
    form.addEventListener('input', event => {
        if (!event.target.matches('input[type=search]')) return;
        const needle = event.target.value.trim().toLocaleLowerCase();
        const area = event.target.closest('.roster-choice');
        const rows = Array.from(area.querySelectorAll('.duel-actors label'));
        for (const row of rows) row.hidden = !row.textContent.toLocaleLowerCase().includes(needle);
        area.querySelector('.no-actors').hidden = !rows.length || rows.some(row => !row.hidden);
    });
    async function loadRoster(source) {
        const key = source.dataset.source;
        pending.get(key)?.abort();
        const controller = new AbortController();
        pending.set(key, controller);
        failures.delete(key);
        const area = source.closest('fieldset').querySelector('.roster-choice');
        const url = new URL(location.href);
        url.hash = '';
        url.search = new URLSearchParams({
            options: key, [key + '_file']: source.value,
            [key]: area.querySelector('input:checked')?.value || ''
        });
        for (const input of form.querySelectorAll('input[name=log], input[name=mode], input[name=target_group], input[name=pairing], input[name=run]')) {
            url.searchParams.append(input.name, input.value);
        }
        area.inert = true;
        area.setAttribute('aria-busy','true');
        syncStatus();
        try {
            const response = await fetch(url, {signal:controller.signal});
            if (!response.ok || response.redirected) throw Error();
            const template = document.createElement('template');
            template.innerHTML = await response.text();
            if (!template.content.querySelector('.duel-actors')) throw Error();
            if (controller.signal.aborted) return;
            area.replaceChildren(template.content);
            enableSearch(area);
        } catch (error) {
            if (controller.signal.aborted) return;
            failures.add(key);
            const retry = document.createElement('button');
            retry.type = 'button';
            retry.textContent = '重新读取角色 ' + (key === 'left' ? 'A' : 'B');
            retry.onclick = () => loadRoster(source);
            area.replaceChildren(retry);
        } finally {
            if (pending.get(key) === controller) {
                pending.delete(key);
                area.inert = false;
                area.removeAttribute('aria-busy');
                syncStatus();
            }
        }
    }
    form.addEventListener('change', event => {
        if (event.target.type === 'search') return;
        if (discard) discard.hidden = false;
        if (event.target.matches('[data-source]')) loadRoster(event.target);
        else syncStatus();
    });

    const tools = document.querySelector('.skill-tools');
    if (tools) {
        const section = tools.closest('section');
        const body = section.querySelector('tbody');
        const rows = Array.from(body.querySelectorAll('[data-skill]'));
        const search = tools.querySelector('input');
        const presence = tools.querySelector('[data-presence]');
        const order = tools.querySelector('[data-order]');
        const empty = section.querySelector('[data-no-skills]');
        function score(row) {
            if (!row.dataset.diff) return -Infinity;
            const diff = Number(row.dataset.diff);
            return order.value === 'gap' ? Math.abs(diff) : order.value === 'a' ? -diff : diff;
        }
        function updateRows() {
            const needle = search.value.trim().toLocaleLowerCase();
            const sorted = rows.slice();
            if (order.value) sorted.sort((a,b) => score(b) - score(a));
            let count = 0;
            for (const row of sorted) {
                row.hidden = (presence.value && presence.value !== row.dataset.presence) || !row.cells[0].textContent.toLocaleLowerCase().includes(needle);
                if (!row.hidden) count++;
            }
            body.replaceChildren(...sorted);
            tools.querySelector('output').textContent = count + ' / ' + rows.length + ' 项';
            empty.hidden = count !== 0;
        }
        tools.hidden = false;
        tools.addEventListener('input',updateRows);
        empty.querySelector('button').onclick = () => {
            search.value = presence.value = '';
            updateRows();
            search.focus();
        };
        updateRows();
    }

    const timeline = document.querySelector('.duel-time');
    if (!timeline) return;
    const rows = Array.from(timeline.querySelectorAll('tr[data-second]'));
    if (!rows.length) return;
    const slider = timeline.querySelector('input[type=range]');
    slider.max = rows.length - 1;
    const chart = timeline.querySelector('.time-bars');
    for (const row of rows) {
        const group = document.createElement('div');
        for (const cell of row.querySelectorAll('td[data-state]')) {
            const bar = document.createElement('i');
            bar.dataset.state = cell.dataset.state;
            bar.style.height = Math.max(2, Number(cell.dataset.height)) + '%';
            group.append(bar);
        }
        chart.append(group);
    }
    function readTime() {
        const i = Number(slider.value);
        const row = rows[i];
        const start = Number(row.dataset.second);
        const end = start + Number(timeline.dataset.step);
        const windowLabel = start + '–' + end + ' 秒';
        slider.setAttribute('aria-valuetext',windowLabel);
        timeline.querySelector('[data-window]').textContent = windowLabel;
        timeline.querySelector('[data-time-gap]').replaceChildren(row.querySelector('[data-gap] .comparison-gap').cloneNode(true));
        Array.from(chart.children).forEach((bar,j) => bar.classList.toggle('current',j === i));
        timeline.querySelectorAll('.time-reading a').forEach((link,side) => {
            const cell = row.cells[side+1];
            link.previousElementSibling.textContent = cell.textContent.trim();
            link.hidden = cell.dataset.state === 'ended';
            const url = new URL(link.dataset.base,location.href);
            url.searchParams.set('start',start);
            url.searchParams.set('end',end);
            link.href = url;
        });
    }
    chart.onclick = event => {
        const group = event.target.closest('.time-bars > div');
        if (!group) return;
        slider.value = Array.from(chart.children).indexOf(group);
        readTime();
        slider.focus({preventScroll:true});
    };
    timeline.querySelector('.time-inspector').hidden = false;
    slider.addEventListener('input',readTime);
    readTime();
})();
