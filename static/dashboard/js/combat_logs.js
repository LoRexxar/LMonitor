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
        if (location.hash === '#saved-collections') document.getElementById('saved-collections').open = true;
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
