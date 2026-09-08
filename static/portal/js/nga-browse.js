(() => {
    'use strict';
    const status = document.getElementById('nga-batch-status');
    if (!status || !window.fetch) return;
    let loading = false;
    document.addEventListener('submit', async (event) => {
        const form = event.target;
        if (form.id !== 'nga-swap-form') return;
        event.preventDefault();
        if (loading) return;
        loading = true;
        const button = form.querySelector('button');
        const current = document.getElementById('nga-batch');
        const url = new URL(form.action, window.location.href);
        url.search = new URLSearchParams(new FormData(form)).toString();
        url.hash = '';
        button.disabled = true;
        button.textContent = '正在加载…';
        current.setAttribute('aria-busy', 'true');
        status.textContent = '正在加载下一批帖子…';
        const controller = new AbortController();
        const timeout = window.setTimeout(() => controller.abort(), 15000);
        try {
            const response = await fetch(url, {
                headers: {'X-NGA-Batch': '1'}, signal: controller.signal,
                credentials: 'same-origin'
            });
            if (!response.ok) throw new Error('batch request failed');
            // Only our autoescaped server template supplies markup, never string-built post data.
            const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
            const next = doc.getElementById('nga-batch');
            if (!next || !next.dataset.url) throw new Error('invalid batch');
            const canonical = new URL(next.dataset.url, window.location.href);
            if (canonical.origin !== window.location.origin) throw new Error('invalid URL');
            // Replace the current history entry: detail/browser-back restores this batch.
            history.replaceState(null, '', canonical.pathname + canonical.search);
            current.replaceWith(next);
            status.textContent = '已换一批帖子。';
            const header = document.querySelector('.portal-header');
            const inset = header ? header.getBoundingClientRect().height + 16 : 100;
            next.style.scrollMarginTop = `${inset}px`;
            next.focus({preventScroll: true});
            next.scrollIntoView({block: 'start', behavior: 'auto'});
        } catch (_) {
            status.textContent = '加载失败，当前帖子已保留。请点击“换 20 个帖子”重试。';
        } finally {
            window.clearTimeout(timeout);
            loading = false;
            current.removeAttribute('aria-busy');
            button.disabled = false;
            button.textContent = '换 20 个帖子';
        }
    });
})();
