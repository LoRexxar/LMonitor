(() => {
    const waiting = document.querySelector('[data-wait-page]');
    if (waiting) {
        const panel = waiting.querySelector('.wait-panel');
        const stage = waiting.querySelector('[data-stage]');
        const elapsed = waiting.querySelector('[data-elapsed]');
        const slow = waiting.querySelector('[data-slow]');
        const stop = waiting.querySelector('[data-stop]');
        const retry = waiting.querySelector('[data-retry]');
        const connection = waiting.querySelector('[data-connection]');
        const start = performance.now();
        const controller = new AbortController();
        let poll, tick, failures = 0;
        function cleanup() {
            clearTimeout(poll);
            clearInterval(tick);
            controller.abort();
            window.removeEventListener('offline', offline);
        }
        function finish(message, state = 'error') {
            if (controller.signal.aborted) return;
            cleanup();
            panel.dataset.state = state;
            stage.setAttribute('role', state === 'error' ? 'alert' : 'status');
            stage.textContent = message;
            const restoreFocus = document.activeElement === stop;
            stop.hidden = slow.hidden = connection.hidden = true;
            retry.hidden = false;
            if (state === 'error') retry.textContent = '重新读取';
            if (restoreFocus) retry.focus();
        }
        function offline() { finish('网络连接已断开。恢复连接后可以重新读取。'); }
        async function readStage() {
            try {
                const response = await fetch(waiting.dataset.progress, {signal:controller.signal, cache:'no-store'});
                if (!response.ok || !response.headers.get('Content-Type')?.includes('application/json')) throw Error();
                const result = await response.json();
                if (controller.signal.aborted) return;
                failures = 0;
                connection.hidden = true;
                if (result.message) stage.textContent = result.message;
            } catch {
                if (!controller.signal.aborted && ++failures >= 2) {
                    connection.textContent = '暂时无法获取最新进度，仍在等待分析结果。';
                    connection.hidden = false;
                }
            }
            if (!controller.signal.aborted) poll = setTimeout(readStage, 1800);
        }
        async function loadDocument() {
            try {
                const response = await fetch(waiting.dataset.continue, {signal:controller.signal,
                    cache:'no-store', mode:'same-origin', headers:{Accept:'text/html','X-Combat-Navigation':'continue'}});
                const dest = new URL(response.url);
                if (!dest.pathname.startsWith(waiting.dataset.root) && response.redirected && response.ok) {
                    location.replace(dest.href);
                    return;
                }
                const html = await response.text();
                if (controller.signal.aborted) return;
                const trusted = response.headers.get('X-Combat-Document') === '1';
                if (!response.ok || !trusted) {
                    let reason;
                    if (trusted) {
                        const alert = new DOMParser().parseFromString(html, 'text/html').querySelector('[role=alert]');
                        reason = alert?.firstChild?.textContent.trim();
                    }
                    if (!reason && response.headers.get('Content-Type')?.includes('application/json')) {
                        try { reason = JSON.parse(html).message; } catch { /* 保留 HTTP 错误提示。 */ }
                    }
                    const reasons = {403:'此账号没有访问权限。',404:'所选日志或战斗已不可用。',429:'请求过于频繁，请稍后再试。',504:'服务器响应超时，可以重新读取。'};
                    finish(reason || reasons[response.status] || '分析服务暂时没有返回可用的页面，请稍后重试。');
                    return;
                }
                // 只加载本站日志视图标记的完整 HTML，保留最终地址供返回、筛选和刷新使用。
                dest.searchParams.delete('_clwait');
                dest.searchParams.delete('refresh');
                dest.hash = location.hash;
                cleanup();
                history.replaceState(history.state, '', dest);
                document.open();
                document.write(html);
                document.close();
            } catch {
                if (!controller.signal.aborted) finish('连接分析服务失败，请检查网络后重新读取。');
            }
        }
        stop.hidden = false;
        stop.onclick = () => finish('已停止等待。', 'stopped');
        window.addEventListener('offline', offline);
        window.addEventListener('pagehide', cleanup, {once:true});
        window.addEventListener('pageshow', event => {
            if (event.persisted && panel.dataset.state === 'loading') location.replace(retry.href);
        });
        tick = setInterval(() => {
            const seconds = Math.floor((performance.now() - start) / 1000);
            elapsed.textContent = '已等待 ' + seconds + ' 秒';
            slow.hidden = seconds < 12;
            if (seconds >= 120) finish('等待已超过 2 分钟。可以重新读取，或返回选择其他战斗。');
        }, 1000);
        poll = setTimeout(readStage, 800);
        if (navigator.onLine) loadDocument(); else offline();
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
