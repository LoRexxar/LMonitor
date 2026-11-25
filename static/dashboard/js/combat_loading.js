(() => {
    const waiting = document.querySelector('[data-wait-page]');
    if (waiting) {
        const stage = waiting.querySelector('[data-stage]');
        const elapsed = waiting.querySelector('[data-elapsed]');
        const slow = waiting.querySelector('[data-slow]');
        const stop = waiting.querySelector('[data-stop]');
        const retry = waiting.querySelector('[data-retry]');
        const start = performance.now();
        let poll, tick, navigate, stopped = false;
        const controller = new AbortController();
        function stopTimers() {
            stopped = true;
            controller.abort();
            clearTimeout(navigate);
            clearTimeout(poll);
            clearInterval(tick);
        }
        function interrupted() {
            stopTimers();
            stage.textContent = '已停止等待。';
            stop.hidden = true;
            retry.hidden = false;
            slow.hidden = true;
        }
        async function readStage() {
            try {
                const response = await fetch(waiting.dataset.progress, {signal:controller.signal, cache:'no-store'});
                if (response.ok) {
                    const result = await response.json();
                    if (!stopped && result.message && stage.textContent !== result.message) stage.textContent = result.message;
                }
            } catch { /* 查询进度失败时，保留最后收到的状态。 */ }
            if (!stopped) poll = setTimeout(readStage, 1800);
        }
        stop.hidden = false;
        stop.onclick = () => { window.stop(); interrupted(); retry.focus(); };
        window.navigation?.addEventListener('navigateerror', () => { if (!stopped) interrupted(); });
        tick = setInterval(() => {
            const seconds = Math.floor((performance.now() - start) / 1000);
            elapsed.textContent = '已等待 ' + seconds + ' 秒';
            slow.hidden = seconds < 12;
        }, 1000);
        stage.textContent = '正在等待分析服务响应。';
        poll = setTimeout(readStage, 900);
        navigate = setTimeout(() => location.replace(waiting.dataset.continue + location.hash), 60);
        addEventListener('pagehide', stopTimers);
        addEventListener('pageshow', event => { if (event.persisted) interrupted(); });
        return;
    }

    const indicator = document.getElementById('combat-pending');
    if (!indicator) return;
    let current;
    // 同一页面切换多个筛选时，较早完成的请求不能清除新的提示。
    function start(message) {
        current?.();
        const began = performance.now();
        const title = indicator.querySelector('[role=status]');
        const time = indicator.querySelector('[data-elapsed]');
        let tick;
        const delay = setTimeout(() => {
            title.textContent = message;
            indicator.hidden = false;
            function update() {
                const seconds = Math.floor((performance.now() - began) / 1000);
                time.textContent = seconds ? '已等待 ' + seconds + ' 秒' : '';
            }
            update();
            tick = setInterval(update, 1000);
        }, 250);
        function end() {
            clearTimeout(delay);
            clearInterval(tick);
            if (current === end) {
                indicator.hidden = true;
                current = null;
            }
        }
        current = end;
        return end;
    }
    window.combatPending = {start};
    addEventListener('pageshow', () => current?.());
    addEventListener('pagehide', () => current?.());
    addEventListener('keydown', event => { if (event.key === 'Escape') current?.(); });
    window.navigation?.addEventListener('navigateerror', () => current?.());
    addEventListener('click', event => {
        const link = event.target.closest?.('a[href]');
        if (!link || event.defaultPrevented || event.button || event.ctrlKey || event.metaKey || event.shiftKey ||
            event.altKey || link.target && link.target !== '_self' || link.hasAttribute('download')) return;
        const dest = new URL(link.href);
        if (dest.origin !== location.origin || !dest.pathname.startsWith(indicator.dataset.root) ||
            dest.pathname.endsWith('/original/') || dest.hash && dest.pathname === location.pathname && dest.search === location.search) return;
        const label = link.getAttribute('aria-label') || link.textContent.trim();
        start(label ? '正在打开' + label.slice(0,48) + '…' : '正在打开战斗数据…');
    });
    addEventListener('submit', event => {
        if (event.defaultPrevented || event.target.target && event.target.target !== '_self') return;
        if (!event.target.closest('.combat-shell')) return;
        start('正在提交请求，请稍候…');
    });
})();
