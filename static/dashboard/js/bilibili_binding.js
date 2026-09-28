(() => {
    'use strict';
    const root = document.getElementById('bilibili-widget');
    if (!root) return;
    const byId = id => document.getElementById(id);
    const registration = root.dataset.mode === 'register';
    let challenge = null;
    let busy = false;
    function message(text, error = false) {
        byId('bb-message').textContent = text || '';
        byId('bb-message').className = error ? 'bb-error' : '';
    }
    async function request(data) {
        const response = await fetch('/auth/bilibili/api/', {
            method: data ? 'POST' : 'GET', credentials: 'same-origin',
            headers: {'Content-Type': 'application/json', 'X-CSRFToken': document.querySelector('meta[name="csrf-token"]')?.content || ''},
            ...(data ? {body: JSON.stringify(data)} : {}),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.message || '操作失败，请刷新重试。');
        return result;
    }
    async function refresh() {
        const result = await request();
        const config = result.config;
        const binding = result.binding;
        challenge = result.challenge;
        const valid = challenge && ['pending', 'manual', 'verified'].includes(challenge.status);
        const active = binding?.active;
        byId('bb-intro').textContent = config.enabled
            ? `${registration && config.required ? '注册前必须完成验证。' : ''}${config.instructions}`
            : '管理员尚未开放 B 站绑定。';
        byId('bb-binding').hidden = !binding;
        if (binding) byId('bb-binding').textContent = `B 站 UID ${binding.uid}${binding.nickname ? `（${binding.nickname}）` : ''} · ${active ? '已验证绑定' : '绑定已撤销，可重新验证同一 UID'}。验证时间：${new Date(binding.verified_at).toLocaleString('zh-CN')}`;
        byId('bb-start').hidden = !config.enabled || active || valid;
        byId('bb-challenge').hidden = !config.enabled || active || !valid;
        byId('bb-proof-id').value = valid && challenge.status === 'verified' ? challenge.id : '';
        if (binding) { byId('bb-uid').value = binding.uid; byId('bb-uid').readOnly = true; }
        if (valid) {
            byId('bb-target').textContent = challenge.uid;
            byId('bb-dynamic').href = challenge.dynamic_url;
            byId('bb-code').textContent = challenge.code;
            byId('bb-expiry').textContent = `有效至 ${new Date(challenge.expires_at).toLocaleString('zh-CN')}。验证并完成绑定后可自行删除评论。`;
            const verified = challenge.status === 'verified';
            byId('bb-check').hidden = verified;
            byId('bb-manual').hidden = verified || challenge.status === 'manual';
            byId('bb-confirm').hidden = !verified || registration;
            byId('bb-proof').hidden = !verified;
            byId('bb-proof').textContent = `已验证 UID ${challenge.uid}${challenge.nickname ? `（${challenge.nickname}）` : ''}。${registration ? '现在可以提交注册表单。' : '请确认这是你的账号后完成绑定。'}`;
        }
        if (challenge && !valid && !active) message(({expired: '申请已过期，请重新获取口令。', cancelled: '申请已取消或配置已更新，请重新发起。', rejected: '人工核验未通过，请检查后重新申请。'})[challenge.status] || '');
        else if (valid) message(registration && challenge.status === 'verified' ? '验证通过，请提交下方注册表单。' : challenge.message);
        else message(active ? '绑定已完成。' : '');
    }
    async function run(action) {
        if (busy) return;
        busy = true;
        root.querySelectorAll('button').forEach(button => { button.disabled = true; });
        message('正在处理…');
        try {
            if (action === 'refresh') await refresh();
            else {
                await request({action, uid: byId('bb-uid').value.trim(), challenge_id: challenge?.id});
                await refresh();
            }
        } catch (error) { message(error.message || '网络异常，请稍后重试。', true); }
        finally { busy = false; root.querySelectorAll('button').forEach(button => { button.disabled = false; }); }
    }
    ['create', 'check', 'manual', 'confirm', 'cancel', 'refresh'].forEach(action => byId(`bb-${action}`).addEventListener('click', () => run(action)));
    byId('bb-copy').addEventListener('click', async () => {
        try { await navigator.clipboard.writeText(challenge.code); message('口令已复制，请使用指定 UID 发表一级评论。'); }
        catch (_) { message('无法自动复制，请手动选择并复制上方完整口令。', true); }
    });
    run('refresh');
})();
