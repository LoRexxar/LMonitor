(() => {
    const main = document.querySelector('main[data-base]');
    const selection = document.getElementById('log-selection');
    if (selection) {
        const refresh = () => {
            const count = selection.querySelectorAll('[data-log-choice]:checked, input[type=hidden][data-log-choice]').length;
            selection.querySelector('output').textContent = '已选 ' + count + ' 份日志';
            selection.querySelector('[value=battle]').disabled = !count;
            selection.querySelector('[data-clear-selection]').hidden = !count;
        };
        selection.addEventListener('change', refresh);
        window.addEventListener('pageshow', refresh);
        refresh();
        const sync = selection.querySelector('[data-sync-url]');
        if (sync) bindPublicDirectory(sync);
        if (location.hash === '#saved-collections') document.getElementById('saved-collections').open = true;
    }
    function bindPublicDirectory(sync) {
        const button = sync.querySelector('[name=refresh]');
        const apply = sync.querySelector('[data-show-updates]');
        const status = sync.querySelector('[data-sync-status]');
        const form = sync.closest('form');
        let interacted = false;
        for (const event of ['pointerdown', 'keydown', 'input', 'change']) {
            form.addEventListener(event, e => {if (!sync.contains(e.target)) interacted = true;});
        }
        const canReplace = () => !interacted && !form.querySelector(
            '[data-log-choice]:checked, input[type=hidden][data-log-choice], .library-row:hover, .library-row:focus-within');
        let pending, poll, deadline;

        function stop() {
            clearTimeout(poll);
            clearTimeout(deadline);
            pending?.abort();
            pending = null;
            button.disabled = false;
        }
        async function check(force = false) {
            if (pending) return;
            const request = new AbortController();
            pending = request;
            button.disabled = true;
            status.textContent = '正在检查更新…';
            deadline = setTimeout(() => {
                stop();
                status.textContent = '检查更新超时，仍可打开已保存的报告。';
            }, 60000);
            async function read(polling) {
                try {
                    const response = await fetch(sync.dataset.syncUrl, {
                        method:'POST', credentials:'same-origin', signal:request.signal,
                        headers:{'X-CSRFToken':document.querySelector('[name=csrfmiddlewaretoken]').value},
                        body:new URLSearchParams(polling ? {poll:polling} : {force:force ? '1' : '0'})
                    });
                    if (!response.ok || !response.headers.get('Content-Type')?.includes('application/json')) throw Error();
                    const data = await response.json();
                    if (request.signal.aborted) return;
                    if (data.pending) {
                        poll = setTimeout(() => read(data.pending), 1800);
                        return;
                    }
                    let changed = data.revision !== sync.dataset.revision;
                    if (changed && canReplace()) {
                        const url = new URL(location.href);
                        url.searchParams.delete('refresh');
                        url.searchParams.set('source', sync.dataset.source);
                        const result = await fetch(url, {signal:request.signal, credentials:'same-origin', cache:'no-store'});
                        if (!result.ok || result.redirected) throw Error();
                        const next = new DOMParser().parseFromString(await result.text(), 'text/html');
                        const nextRows = next.querySelector('[data-directory-rows]');
                        const nextSync = next.querySelector('[data-sync-url]');
                        if (request.signal.aborted) return;
                        if (canReplace() && nextRows && nextSync?.dataset.revision === data.revision) {
                            form.querySelector('[data-directory-rows]').replaceWith(nextRows);
                            sync.dataset.revision = data.revision;
                            changed = false;
                        }
                    }
                    apply.hidden = !changed;
                    status.textContent = changed ? '目录有更新，当前列表仍可使用。' : '已是最新';
                    if (!changed) {
                        const time = sync.querySelector('time');
                        time.dateTime = data.updated;
                        time.textContent = data.label;
                    }
                } catch {
                    if (request.signal.aborted) return;
                    status.textContent = '暂时无法更新，仍可打开已保存的报告。';
                }
                stop();
            }
            read(false);
        }
        button.addEventListener('click', event => {event.preventDefault(); check(true);});
        window.addEventListener('pagehide', stop);
        window.addEventListener('pageshow', event => {if (event.persisted) check();});
        check(sync.hasAttribute('data-force'));
    }
    function showResumeLinks() {
        for (const link of document.querySelectorAll('a[data-resume-key]')) {
            try {
                const query = sessionStorage.getItem(link.dataset.resumeKey);
                if (query?.startsWith('?')) {
                    const url = new URL(link.href);
                    url.search = query;
                    link.href = url;
                    link.hidden = false;
                }
            } catch { /* 无会话存储时仍可从目录打开。 */ }
        }
    }
    showResumeLinks();
    window.addEventListener('pageshow', showResumeLinks);
    if (!main) return;
    const token = main.querySelector('[name=csrfmiddlewaretoken]').value;
    const notice = document.getElementById('notice');
    const current = location.pathname;
    async function api(url, method = 'GET', body) {
        const response = await fetch(url, {
            method, body, headers: {'X-CSRFToken': token}, credentials: 'same-origin',
        });
        if (!response.headers.get('Content-Type')?.includes('application/json')) {
            throw new Error('请求未完成，请确认登录状态后重新打开日志。');
        }
        const data = await response.json();
        if (!response.ok) throw new Error(data.message || '请求失败，请重试。');
        return data;
    }

    document.getElementById('retry')?.addEventListener('click', async () => {
        try {
            await api(current + 'finish/', 'POST');
            location.reload();
        } catch (error) { notice.textContent = error.message; }
    });
    if (['running', 'queued'].includes(main.dataset.state) && !document.getElementById('retry')) {
        setTimeout(() => location.reload(), 5000);
    }

    const ai = document.getElementById('highlights');
    ai?.addEventListener('click', async () => {
        const status = document.getElementById('ai-status');
        ai.disabled = true;
        status.textContent = '正在选择重点事件…';
        try {
            const result = await api(current + 'highlights/', 'POST', new URLSearchParams({
                fight: ai.dataset.fight, death: ai.dataset.death,
            }));
            document.querySelectorAll('#evidence details').forEach(row => {
                const selected = result.lines.includes(Number(row.id.slice(5)));
                row.classList.toggle('highlight', selected);
                row.open = selected;
            });
            status.textContent = '已展开 ' + result.lines.length + ' 条重点记录。';
        } catch (error) { status.textContent = error.message; }
        finally { ai.disabled = false; }
    });
})();
