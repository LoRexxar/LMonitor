(() => {
    'use strict';
    const $ = id => document.getElementById(id);
    const endpoint = '/api/dashboard/bilibili-binding/';
    let reviewing = null;
    let revoking = null;
    let busy = false;
    function message(text, error = false) { $('ba-message').textContent = text; $('ba-message').className = error ? 'bb-error' : ''; }
    async function api(data) {
        const response = await fetch(endpoint + (data ? '' : `?search=${encodeURIComponent($('ba-search').value.trim())}`), {
            method: data ? 'POST' : 'GET', credentials: 'same-origin',
            headers: {'Content-Type': 'application/json', 'X-CSRFToken': document.querySelector('meta[name="csrf-token"]').content},
            ...(data ? {body: JSON.stringify(data)} : {}),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.message || '操作失败');
        return result;
    }
    function cell(row, text) { const td = document.createElement('td'); td.textContent = text; row.appendChild(td); return td; }
    function link(parent, url, title) { const a = document.createElement('a'); a.href = url; a.textContent = title; a.target = '_blank'; a.rel = 'noopener noreferrer'; parent.appendChild(a); }
    function button(parent, title, callback) { const b = document.createElement('button'); b.type = 'button'; b.className = 'bb-secondary'; b.textContent = title; b.addEventListener('click', callback); parent.appendChild(b); }
    const dates = value => new Date(value).toLocaleString('zh-CN');
    const states = {pending: '待检查', manual: '待人工核验', verified: '已核验，待用户确认', consumed: '已完成绑定', rejected: '已驳回', cancelled: '已失效 / 取消', expired: '已过期'};
    async function load(settings = false) {
        const data = await api();
        if (settings) {
            const c = data.config;
            $('ba-enabled').checked = c.enabled; $('ba-required').checked = c.require_for_registration;
            $('ba-url').value = c.dynamic_url; $('ba-minutes').value = c.challenge_minutes;
            $('ba-interval').value = c.check_interval_seconds; $('ba-pages').value = c.max_comment_pages;
            $('ba-instructions').value = c.instructions;
        }
        $('ba-bindings').replaceChildren();
        data.bindings.forEach(item => {
            const row = document.createElement('tr');
            cell(row, item.username); link(cell(row, ''), item.profile_url, `${item.uid} ${item.nickname}`);
            cell(row, `${item.active ? '有效' : '已撤销'} · ${dates(item.verified_at)}${item.revoke_reason ? `；${item.revoke_reason}` : ''}`);
            const actions = cell(row, ''); link(actions, item.evidence_url, '验证评论');
            if (item.active) button(actions, '撤销', () => {
                revoking = item; $('ba-revoke-target').textContent = `${item.username} ↔ UID ${item.uid}`;
                $('ba-revoke-note').value = ''; $('ba-revoke-error').textContent = ''; $('ba-revoke-dialog').showModal();
            });
            $('ba-bindings').appendChild(row);
        });
        $('ba-challenges').replaceChildren();
        data.challenges.forEach(item => {
            const row = document.createElement('tr'); cell(row, `${item.username} / ${item.uid}`);
            const code = cell(row, item.code); code.appendChild(document.createElement('br')); link(code, item.dynamic_url, '指定动态');
            cell(row, `${states[item.status] || item.status} · ${dates(item.expires_at)}`);
            const action = cell(row, item.review_note ? `${item.reviewer}：${item.review_note}` : '');
            if (item.status === 'manual') button(action, '人工核验', () => {
                reviewing = item; $('ba-review-target').textContent = `${item.username} / UID ${item.uid}`;
                $('ba-review-code').textContent = item.code; $('ba-review-link').href = item.dynamic_url;
                $('ba-comment-id').value = ''; $('ba-review-note').value = ''; $('ba-checked').checked = false;
                $('ba-review-error').textContent = ''; $('ba-review-dialog').showModal();
            });
            $('ba-challenges').appendChild(row);
        });
        for (const id of ['ba-bindings', 'ba-challenges']) {
            if (!$(id).children.length) { const row = document.createElement('tr'); cell(row, '暂无记录').colSpan = 4; $(id).appendChild(row); }
        }
    }
    async function run(work, errorId) {
        if (busy) return;
        busy = true;
        document.querySelectorAll('button').forEach(b => { b.disabled = true; });
        try { await work(); }
        catch (error) { if (errorId) $(errorId).textContent = error.message; else message(error.message || '网络异常', true); }
        finally { busy = false; document.querySelectorAll('button').forEach(b => { b.disabled = false; }); }
    }
    $('ba-settings').addEventListener('submit', event => {
        event.preventDefault(); run(async () => {
            const result = await api({action: 'save', config: {
                enabled: $('ba-enabled').checked, require_for_registration: $('ba-required').checked,
                dynamic_url: $('ba-url').value.trim(), instructions: $('ba-instructions').value.trim(),
                challenge_minutes: Number($('ba-minutes').value), check_interval_seconds: Number($('ba-interval').value),
                max_comment_pages: Number($('ba-pages').value),
            }});
            await load(true); message(result.message);
        });
    });
    $('ba-probe').addEventListener('click', () => run(async () => { message('正在测试读取…'); const result = await api({action: 'probe', dynamic_url: $('ba-url').value.trim()}); message(result.message); }));
    $('ba-refresh').addEventListener('click', () => run(async () => { await load(); message('记录已刷新。'); }));
    async function review(action) {
        await run(async () => {
            const result = await api({action, challenge_id: reviewing.id, comment_id: $('ba-comment-id').value.trim(), checked: $('ba-checked').checked, note: $('ba-review-note').value.trim()});
            $('ba-review-dialog').close(); await load(); message(result.message);
        }, 'ba-review-error');
    }
    $('ba-review-form').addEventListener('submit', event => { event.preventDefault(); review('approve'); });
    $('ba-reject').addEventListener('click', () => review('reject'));
    $('ba-review-close').addEventListener('click', () => $('ba-review-dialog').close());
    $('ba-revoke-close').addEventListener('click', () => $('ba-revoke-dialog').close());
    $('ba-revoke-form').addEventListener('submit', event => { event.preventDefault(); run(async () => {
        const result = await api({action: 'revoke', binding_id: revoking.id, note: $('ba-revoke-note').value.trim()});
        $('ba-revoke-dialog').close(); await load(); message(result.message);
    }, 'ba-revoke-error'); });
    run(async () => { await load(true); message('设置与记录已加载。'); });
})();
