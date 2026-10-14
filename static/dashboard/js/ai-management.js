(() => {
    'use strict';

    function init() {
        const root = document.getElementById('ai-management');
        if (!root) return;
        const $ = id => root.querySelector(`#${id}`);
        const endpoint = '/api/dashboard/ai-providers/';
        const dialog = $('ai-editor');
        const form = $('ai-editor-form');
        let providers = [];
        let editing = null;
        let loaded = false;
        let loading = false;
        let saving = false;
        let stale = false;

        function node(tag, text, className) {
            const element = document.createElement(tag);
            if (text !== undefined) element.textContent = String(text);
            if (className) element.className = className;
            return element;
        }
        function message(text, error = false) {
            $('ai-message').textContent = text;
            $('ai-message').className = error ? 'ai-message ai-error' : 'ai-message';
        }
        function date(value) {
            if (!value) return '暂无记录';
            const parsed = new Date(value);
            return Number.isNaN(parsed.getTime()) ? '时间未知' : parsed.toLocaleString('zh-CN');
        }
        function source(value) {
            return {settings: 'settings 默认', database: '后台覆盖', cleared: '已显式清除'}[value] || '未知来源';
        }
        function keyInfo(provider) {
            return `${provider.has_api_key ? '已配置（不显示密钥）' : '未配置'} · ${source(provider.api_key_source)}`;
        }
        function csrf() {
            if (typeof window.getCSRFToken === 'function') return window.getCSRFToken();
            return document.querySelector('meta[name="csrf-token"]')?.content
                || document.querySelector('[name=csrfmiddlewaretoken]')?.value || '';
        }
        async function api(key, payload) {
            const options = {method: payload ? 'PATCH' : 'GET', credentials: 'same-origin', cache: 'no-store', headers: {Accept: 'application/json'}};
            if (payload) {
                const token = csrf();
                if (!token) throw new Error('无法获取 CSRF Token，请刷新页面后重试。');
                options.headers['X-CSRFToken'] = token;
                options.headers['Content-Type'] = 'application/json';
                options.body = JSON.stringify(payload);
                // Keep no second copy of the entered secret in a retained object.
                delete payload.api_key;
            }
            try {
                const response = await fetch(key ? `${endpoint}${encodeURIComponent(key)}/` : endpoint, options);
                if (response.status === 409) {
                    const error = new Error('配置已被其他管理员修改。请刷新配置并重新编辑，不会覆盖新配置。');
                    error.conflict = true;
                    throw error;
                }
                if (!response.ok) {
                    const errors = {400: '配置未通过校验，请检查优先级、模型、URL 与超时。', 401: '登录已过期，请重新登录。', 403: '无权管理 AI 配置，或 CSRF 校验失败。', 404: '供应商配置不存在，请刷新状态。'};
                    const error = new Error(errors[response.status] || `请求失败（HTTP ${response.status}），请稍后重试。`);
                    error.status = response.status;
                    throw error;
                }
                const result = await response.json();
                if (result.status !== 'success' || !Array.isArray(result.data?.providers)) throw new Error('服务端返回的数据格式不正确。');
                return result.data;
            } finally {
                delete options.body;
                if (payload) delete payload.api_key;
            }
        }
        function controls() {
            $('ai-refresh').disabled = loading || saving;
            $('ai-save').disabled = saving || loading || stale || !editing;
            $('ai-reset').disabled = saving || loading || stale || !editing;
            $('ai-reload-editor').disabled = saving || loading;
            $('ai-editor-close').disabled = saving;
            root.querySelectorAll('[data-ai-edit]').forEach(button => { button.disabled = loading || saving || !loaded; });
            form.querySelectorAll('input').forEach(input => { input.disabled = saving || loading; });
            $('ai-api-key').disabled = saving || loading || $('ai-clear-key').checked;
        }
        function badge(parent, text, tone = '') {
            parent.appendChild(node('span', text, `ai-badge${tone ? ` ai-badge-${tone}` : ''}`));
        }
        function detail(list, label, value) {
            list.append(node('dt', label), node('dd', value === undefined || value === null || value === '' ? '—' : value));
        }
        function render(data) {
            // Only the existing providers are editable. No duplicate config defaults.
            providers = [...data.providers]
                .sort((a, b) => Number(a.priority) - Number(b.priority) || a.provider_key.localeCompare(b.provider_key));
            const providerName = key => providers.find(provider => provider.provider_key === key)?.name || key || '无';
            const backups = providers.filter(provider => provider.enabled && provider.effective && provider.scopes?.includes('general') && provider.provider_key !== data.preferred_provider);
            const summaries = [['通用 AI 首选', providerName(data.preferred_provider)], ['通用有效备用', backups.map(provider => provider.name).join(' → ') || '无'], ['文章翻译首选', providerName(data.preferred_translation_provider)], ['最近实际成功', providerName(data.last_successful_provider)]];
            $('ai-summary').replaceChildren(...summaries.map(([label, value]) => {
                const card = node('div', undefined, 'ai-summary-card');
                card.append(node('span', label), node('strong', value));
                return card;
            }));
            const cards = providers.map(provider => {
                const state = provider.state || {};
                const card = node('article', undefined, 'ai-provider');
                const header = node('div', undefined, 'ai-provider-header');
                const heading = node('div');
                heading.append(node('h3', provider.name), node('p', provider.provider_key, 'ai-hint'));
                const edit = node('button', '编辑配置', 'ai-button');
                edit.type = 'button';
                edit.setAttribute('data-ai-edit', '');
                edit.addEventListener('click', () => openEditor(provider.provider_key));
                header.append(heading, edit);
                const badges = node('div', undefined, 'ai-badges');
                badge(badges, provider.enabled ? '已启用' : '已停用', provider.enabled ? 'blue' : '');
                badge(badges, `优先级 ${provider.priority}`);
                badge(badges, provider.effective ? '配置完整' : '配置不完整', provider.effective ? '' : 'amber');
                if (provider.provider_key === data.preferred_provider) badge(badges, '首选', 'blue');
                else if (provider.enabled && provider.effective && provider.scopes?.includes('general')) badge(badges, '通用备用', 'blue');
                if (provider.enabled && provider.effective && !provider.scopes?.includes('general')) badge(badges, provider.provider_key === data.preferred_translation_provider ? '翻译首选' : '翻译备用', 'blue');
                if (provider.provider_key === data.last_successful_provider) badge(badges, '最近实际成功', 'green');
                const config = node('dl');
                detail(config, '适用范围', provider.scope_label);
                detail(config, '模型', provider.model);
                detail(config, 'Base URL', provider.base_url);
                detail(config, '请求超时', provider.request_timeout_seconds == null ? null : `${provider.request_timeout_seconds} 秒`);
                detail(config, 'API Key', keyInfo(provider));
                detail(config, '配置来源', source(provider.config_source));
                detail(config, '配置版本', provider.version);
                const callHeading = node('p', '最近真实调用', 'ai-call-heading');
                const callBadge = node('div', undefined, 'ai-badges');
                // Never infer healthy from enabled/effective; unknown codes use the
                // backend label (or 未验证), not an invented successful probe.
                const tone = {
                    success: 'green', healthy: 'green', failure: 'red', failed: 'red', error: 'red',
                    authentication_error: 'red', certificate_error: 'red', connection_error: 'red',
                    provider_error: 'red', response_error: 'red', rate_limited: 'amber',
                    invalid: 'amber', unconfigured: 'amber', incomplete: 'amber',
                }[state.code] || '';
                badge(callBadge, state.label || '未验证', tone);
                const calls = node('dl');
                detail(calls, '最近结束', date(state.last_finished_at));
                detail(calls, '最近成功', date(state.last_success_at));
                detail(calls, '最近失败', date(state.last_failure_at));
                detail(calls, '最近模型', state.last_model);
                detail(calls, '延迟', state.latency_ms == null ? null : `${state.latency_ms} ms`);
                detail(calls, 'HTTP 状态', state.last_http_status);
                detail(calls, '错误代码', state.last_error_code);
                card.append(header, badges, config, callHeading, callBadge, calls);
                return card;
            });
            $('ai-providers').replaceChildren(...(cards.length ? cards : [node('p', '服务端没有返回可管理的供应商。', 'ai-hint')]));
            $('ai-refreshed-at').textContent = `状态读取时间：${date(new Date().toISOString())}；调用时间以服务端记录为准。`;
        }
        async function load() {
            if (loading || saving) return false;
            loading = true;
            controls();
            message('正在读取配置与调用记录…');
            try {
                const data = await api();
                render(data);
                loaded = true;
                message('已读取最新配置与实际调用记录，未发起模型测试。');
                return true;
            } catch (_) {
                // Do not log full responses, credentials, or transport objects.
                loaded = false;
                message('无法读取 AI 配置与状态，请确认登录及超级管理员权限后重试。现有展示可能已过期。', true);
                return false;
            } finally {
                loading = false;
                controls();
            }
        }
        function clearEditor() {
            form.reset();
            form.querySelectorAll('input').forEach(input => input.setCustomValidity(''));
            $('ai-api-key').value = '';
            editing = null;
            stale = false;
            $('ai-editor-error').textContent = '';
            $('ai-reload-editor').hidden = true;
            controls();
        }
        function closeEditor() {
            dialog.close();
            clearEditor();
        }
        function openEditor(key) {
            if (!loaded || saving || loading) return;
            const provider = providers.find(item => item.provider_key === key);
            if (!provider) return;
            clearEditor();
            editing = {key: provider.provider_key, version: provider.version};
            $('ai-editor-title').textContent = `编辑 ${provider.name}`;
            $('ai-editor-source').textContent = `当前配置：${source(provider.config_source)} · 版本 ${provider.version}`;
            $('ai-enabled').checked = provider.enabled === true;
            $('ai-priority').value = provider.priority;
            $('ai-timeout').value = provider.request_timeout_seconds ?? '';
            $('ai-model').value = provider.model || '';
            $('ai-base-url').value = provider.base_url || '';
            // Snapshot only non-secret form values; null defaults display empty
            // but must not become explicit empty overrides on unrelated edits.
            editing.original = {
                enabled: $('ai-enabled').checked,
                priority: $('ai-priority').value,
                request_timeout_seconds: $('ai-timeout').value,
                model: $('ai-model').value,
                base_url: $('ai-base-url').value,
            };
            $('ai-key-source').textContent = `${keyInfo(provider)}。保存空密钥会保留原值；保存失败后需重新输入新密钥。`;
            controls();
            if (!dialog.open) dialog.showModal();
        }
        async function save(reset = false) {
            if (saving || loading || stale || !editing) return;
            let payload = {version: editing.version};
            if (reset) payload.reset = true;
            else {
                const fields = {enabled: 'ai-enabled', priority: 'ai-priority',
                    request_timeout_seconds: 'ai-timeout', model: 'ai-model', base_url: 'ai-base-url'};
                for (const [field, id] of Object.entries(fields)) {
                    const input = $(id);
                    const value = field === 'enabled' ? input.checked : input.value;
                    input.setCustomValidity('');
                    if (value === editing.original[field]) continue;
                    let valid = true;
                    if (field === 'priority') valid = value !== '' && Number.isInteger(Number(value)) && Number(value) >= 0 && Number(value) <= 100000;
                    if (field === 'request_timeout_seconds') valid = value !== '' && Number.isFinite(Number(value)) && Number(value) > 0 && Number(value) <= 600;
                    if (field === 'model') valid = value.length > 0 && value.length <= 255 && !/[\s\p{C}]/u.test(value);
                    if (field === 'base_url') {
                        try {
                            const url = new URL(value);
                            valid = value.length <= 500 && !/\s/.test(value) && url.protocol === 'https:' && !!url.hostname
                                && !url.username && !url.password && !value.includes('?') && !value.includes('#');
                        } catch (_) { valid = false; }
                    }
                    if (!valid) input.setCustomValidity('请输入合法的配置值；模型与 URL 修改后不能为空。');
                    if (!input.reportValidity()) {
                        $('ai-api-key').value = '';
                        return;
                    }
                    payload[field] = ['priority', 'request_timeout_seconds'].includes(field) ? Number(value) : value;
                }
                if ($('ai-clear-key').checked && $('ai-api-key').value) {
                    $('ai-editor-error').textContent = '不能同时设置和清除密钥。';
                    $('ai-api-key').value = '';
                    return;
                }
                if ($('ai-clear-key').checked) payload.clear_api_key = true;
                else if ($('ai-api-key').value) payload.api_key = $('ai-api-key').value;
            }
            if (reset && !window.confirm('恢复该供应商的 settings 默认配置？后台覆盖与数据库密钥将移除，随后继承 settings 配置。')) return;
            if (!reset && $('ai-clear-key').checked && !window.confirm('确认清除此供应商的 API Key？将不再继承 settings 密钥，可能导致该供应商不可用。')) return;
            const key = editing.key;
            $('ai-api-key').value = '';
            saving = true;
            $('ai-editor-error').textContent = '';
            controls();
            let changed = false;
            try {
                await api(key, payload);
                changed = true;
                loaded = false;
                closeEditor();
            } catch (error) {
                stale = error.conflict === true;
                $('ai-editor-error').textContent = error.message;
                // A transport, server or invalid-response failure can follow a
                // committed write. Require a reload instead of replaying it.
                if (!error.conflict && ![400, 401, 403, 404].includes(error.status)) {
                    stale = true;
                    $('ai-editor-error').textContent = '保存结果无法确认。请刷新配置后重新编辑，勿重复提交。';
                }
                $('ai-reload-editor').hidden = !stale;
            } finally {
                if (payload) delete payload.api_key;
                payload = null;
                $('ai-api-key').value = '';
                saving = false;
                controls();
            }
            if (changed) {
                // Read back the exact collection after a state-changing PATCH.
                const verified = await load();
                message(verified ? '配置已保存并重新读取确认。未进行模型测试。' : '保存请求成功，但重新读取失败；请刷新状态确认实际配置。', !verified);
            }
        }
        $('ai-refresh').addEventListener('click', () => { void load(); });
        form.addEventListener('submit', event => { event.preventDefault(); void save(); });
        $('ai-reset').addEventListener('click', () => { void save(true); });
        $('ai-editor-close').addEventListener('click', () => { if (!saving) closeEditor(); });
        dialog.addEventListener('cancel', event => { if (saving) event.preventDefault(); });
        dialog.addEventListener('close', clearEditor);
        $('ai-clear-key').addEventListener('change', () => { $('ai-api-key').value = ''; controls(); });
        $('ai-reload-editor').addEventListener('click', async () => {
            if (!editing || saving || loading) return;
            if (!window.confirm('刷新会丢弃此表单的未保存修改，并重新读取最新配置。继续？')) return;
            const key = editing.key;
            $('ai-api-key').value = '';
            if (await load()) openEditor(key);
        });
        const activate = () => {
            const active = root.classList.contains('active') || root.style.display === 'block';
            if (!active && dialog.open && !saving) closeEditor();
            if (active && !loaded && !loading && !saving) void load();
        };
        new MutationObserver(activate).observe(root, {attributes: true, attributeFilter: ['class', 'style']});
        controls();
        activate();
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once: true});
    else init();
})();
