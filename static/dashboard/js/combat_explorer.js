(() => {
    const formatAmount = new Intl.NumberFormat('zh-CN');
    function formatTime(seconds) {
        const time = Math.abs(seconds);
        return (seconds < 0 ? '−' : '') + Math.floor(time / 60) + ':' + String(time % 60).padStart(2, '0');
    }
    function showBar(event) {
        const bar = event.target.closest('.plot [data-time]');
        if (!bar) return;
        const reading = bar.closest('.timeline').querySelector('.bar-reading');
        reading.textContent = formatTime(Number(bar.dataset.time)) + '　·　' + formatAmount.format(Number(bar.dataset.value));
        reading.hidden = false;
    }
    function leaveBar(event) {
        const plot = event.target.closest('.plot');
        if (plot && !plot.contains(event.relatedTarget)) plot.closest('.timeline').querySelector('.bar-reading').hidden = true;
    }
    document.addEventListener('pointerover', showBar);
    document.addEventListener('focusin', showBar);
    document.addEventListener('pointerout', leaveBar);
    document.addEventListener('focusout', leaveBar);
    function updateTimeAxis() {
        const axis = document.querySelector('.axis[data-first]');
        if (!axis) return;
        const start = Number(axis.dataset.first);
        const end = Number(axis.dataset.last);
        const labels = Array.from({length: 5}, (_, i) => {
            const seconds = Math.round(start + (end - start) * i / 4);
            const label = document.createElement('span');
            label.textContent = formatTime(seconds);
            label.title = seconds + ' 秒';
            return label;
        });
        axis.replaceChildren(...labels);
    }
    function updatePicker(form) {
        if (!form) return;
        const rows = Array.from(form.querySelectorAll('[data-choice]'));
        const count = rows.filter(row => row.querySelector('input').checked).length;
        const visible = rows.filter(row => !row.hidden).length;
        form.querySelector('[data-no-fights]').hidden = visible > 0;
        form.querySelector('[data-fight-count]').textContent = (count ? '已选 ' + count + ' 场' : '当前范围：整份日志') + ' · 匹配 ' + visible + ' 场';
        form.querySelector('[data-check=visible]').disabled = visible === 0;
        form.querySelector('[data-check=clear]').disabled = count === 0;
        form.querySelector('[data-first]').disabled = count === 0;
        form.querySelector('[data-combine]').textContent = count ? '合并分析所选场次' : '分析整份日志';
    }
    function enhance() {
        updateTimeAxis();
        const metricToggle = document.querySelector('.metric-toggle');
        metricToggle.hidden = false;
        metricToggle.onclick = () => {
            metricToggle.setAttribute('aria-expanded', String(metricToggle.getAttribute('aria-expanded') !== 'true'));
        };
        const wave = document.getElementById('analysis-wave');
        if (wave) {
            wave.form.querySelector('button').hidden = true;
            wave.onchange = () => wave.form.requestSubmit();
        }
        const form = document.querySelector('.fight-picker');
        if (form) {
            form.querySelector('.fight-tools').hidden = false;
            form.querySelector('[data-first]').hidden = false;
            form.querySelector('[data-cancel]').hidden = false;
            updatePicker(form);
        }
    }
    enhance();
    document.addEventListener('input', event => {
        const form = event.target.closest('.fight-picker');
        if (!form) return;
        if (event.target.type === 'search') {
            const term = event.target.value.trim().toLowerCase();
            for (const row of form.querySelectorAll('[data-choice]')) row.hidden = !row.querySelector('label').textContent.toLowerCase().includes(term);
        }
        updatePicker(form);
    });
    document.addEventListener('click', event => {
        const clear = event.target.closest('[data-clear-search]');
        if (clear) {
            const search = clear.form.querySelector('input[type=search]');
            search.value = '';
            search.dispatchEvent(new Event('input', {bubbles:true}));
            search.focus();
            return;
        }
        const button = event.target.closest('button[data-check]');
        if (!button) return;
        const form = button.form;
        for (const row of form.querySelectorAll('[data-choice]')) {
            if (button.dataset.check === 'clear') row.querySelector('input').checked = false;
            else if (!row.hidden) row.querySelector('input').checked = true;
        }
        updatePicker(form);
    });
    function dismissControls(target, restoreFocus) {
        if (document.querySelector('dialog[open]')) return;
        for (const popup of document.querySelectorAll('.controls > details[open]')) {
            if (target && popup.contains(target)) continue;
            const form = popup.querySelector('.fight-picker');
            if (form) {
                form.reset();
                form.querySelectorAll('[data-choice]').forEach(row => row.hidden = false);
                updatePicker(form);
            }
            popup.open = false;
            if (restoreFocus) popup.querySelector('summary').focus();
        }
    }
    document.addEventListener('click', event => {
        if (event.target.closest('[data-cancel]')) dismissControls(null, true);
    });
    document.addEventListener('pointerdown', event => dismissControls(event.target, false));
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape') dismissControls(null, true);
    });
    function rememberView() {
        const key = document.querySelector('#explorer')?.dataset.resumeKey;
        if (!key || !location.search) return;
        const query = new URLSearchParams(location.search);
        for (const field of ['log', 'sample', 'duel']) query.delete(field);
        try { if (query.size) sessionStorage.setItem(key, '?' + query); } catch { /* 存储不可用时仍可直接分享网址。 */ }
    }
    rememberView();
    let pending, pendingUrl;
    const notice = document.querySelector('.query-notice');
    function notify(message) {
        notice.querySelector('span').textContent = message;
        notice.hidden = false;
    }
    notice.querySelector('button').onclick = () => {notice.hidden = true;};
    async function navigate(url, push, trigger) {
        const target = new URL(url, location.href).href;
        if (pendingUrl === target && !pending.signal.aborted) return;
        pending?.abort();
        if (push && target === location.href) return;
        const request = new AbortController();
        pending = request;
        pendingUrl = target;
        const previous = document.querySelector('#explorer');
        previous.setAttribute('aria-busy', 'true');
        const endLoading = window.combatPending?.start('正在按当前场次和筛选条件读取战斗数据…');
        notice.hidden = true;
        const stripOffset = previous.querySelector('.attempt-strip')?.scrollLeft;
        const inResults = Boolean(trigger?.closest('.results, .inspect-options'));
        try {
            const response = await fetch(url, {signal: request.signal});
            if (push && !response.ok && !response.redirected) {
                let reason = '无法更新视图，请重试。';
                if (response.headers.get('Content-Type')?.includes('application/json')) reason = (await response.json()).message || reason;
                if (!request.signal.aborted) notify(reason);
                return;
            }
            const html = await response.text();
            const doc = window.publishedImageFragment(html);
            const next = doc.querySelector('#explorer');
            if (request.signal.aborted) return;
            if (!response.ok || !next || response.redirected) {
                location.assign(url);
                return;
            }
            const navigation = doc.getElementById('combat-navigation');
            if (navigation) document.getElementById('combat-navigation').replaceWith(navigation);
            previous.replaceWith(next);
            enhance();
            const strip = next.querySelector('.attempt-strip');
            if (strip && stripOffset) strip.scrollLeft = stripOffset;
            if (push && trigger) {
                const applied = trigger.dataset.applied;
                const wavePicker = trigger.classList.contains('wave-picker');
                let focus;
                if (trigger.closest('.metric-links')) {
                    const toggle = next.querySelector('.metric-toggle');
                    focus = toggle.offsetWidth ? toggle : next.querySelector('.metric-links [aria-current]');
                } else {
                    focus = applied ? next.querySelector('[data-dimension="' + CSS.escape(applied) + '"]') : next.querySelector(wavePicker ? '#analysis-wave' : inResults ? '.results h2' : '.canvas h1');
                }
                if (focus) {
                    if (focus.matches('h1,h2')) focus.tabIndex = -1;
                    focus.focus({preventScroll:true});
                    if (inResults && focus.getBoundingClientRect().top < 0) focus.scrollIntoView({block:'start'});
                }
            }
            document.title = doc.title;
            if (push) history.pushState(null, '', url);
            rememberView();
        } catch (error) {
            if (error.name !== 'AbortError') {
                if (push) notify('连接失败。当前筛选和结果已保留，可以再次操作。');
                else location.assign(url);
            }
        } finally {
            endLoading?.();
            if (pending === request) {
                pending = pendingUrl = undefined;
                previous.removeAttribute('aria-busy');
                if (previous.isConnected && trigger?.matches('.wave-picker')) trigger.reset();
            }
        }
    }
    document.addEventListener('click', event => {
        const link = event.target.closest('a');
        if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        if (link.closest('.plot') && skipPlotClick) {
            event.preventDefault();
            return;
        }
        if (link.closest('.analysis-scope') || link.origin !== location.origin || link.pathname !== location.pathname) return;
        event.preventDefault();
        navigate(link.href, true, link);
    });
    document.addEventListener('submit', event => {
        const form = event.target;
        if (form.method.toLowerCase() !== 'get') return;
        event.preventDefault();
        const url = new URL(location.href);
        const query = new URLSearchParams(new FormData(form));
        if (event.submitter?.hasAttribute('data-first') && query.has('part')) query.set('fight',query.get('part'));
        const start = form.querySelector('input[type=number][name=start]');
        const end = form.querySelector('input[type=number][name=end]');
        if (start && end && !query.get('anchor') && end.value && Number(end.value) <= Number(start.value)) {
            end.setCustomValidity('结束秒数须大于起始秒数。');
            end.reportValidity();
            end.setCustomValidity('');
            return;
        }
        url.search = query.toString();
        navigate(url, true, form);
    });
    window.addEventListener('popstate', () => navigate(location.href, false));

    let drag, skipPlotClick = false;
    function bucketAt(plot, x) {
        const box = plot.getBoundingClientRect();
        const index = Math.floor((x - box.left) / box.width * plot.children.length);
        return Math.max(0, Math.min(plot.children.length - 1, index));
    }
    document.addEventListener('pointerdown', event => {
        const bar = event.target.closest('.plot a');
        if (!bar || event.button !== 0) return;
        const plot = bar.parentElement;
        drag = {plot, first: bucketAt(plot, event.clientX), pointer: event.pointerId};
    });
    document.addEventListener('pointermove', event => {
        if (!drag || drag.pointer !== event.pointerId) return;
        const {plot, first} = drag;
        const last = bucketAt(plot, event.clientX);
        Array.from(plot.children).forEach((bar, i) => bar.classList.toggle('mark', i >= Math.min(first,last) && i <= Math.max(first,last)));
    });
    function clearDrag() {
        if (drag) drag.plot.querySelectorAll('.mark').forEach(bar => bar.classList.remove('mark'));
        drag = null;
    }
    document.addEventListener('pointercancel', clearDrag);
    window.addEventListener('blur', clearDrag);
    document.addEventListener('dragstart', event => {
        if (event.target.closest('.plot')) event.preventDefault();
    });
    document.addEventListener('pointerup', event => {
        if (!drag || drag.pointer !== event.pointerId) return;
        const {plot, first} = drag;
        const last = bucketAt(plot, event.clientX);
        clearDrag();
        if (last === first) return;
        skipPlotClick = true;
        setTimeout(() => { skipPlotClick = false; }, 0);
        const low = new URL(plot.children[Math.min(first,last)].href);
        const high = new URL(plot.children[Math.max(first,last)].href);
        low.searchParams.set('end',high.searchParams.get('end'));
        navigate(low, true);
    });
})();
