/* 深链接复用 SimC 原有资料编辑器，所有写入仍由原接口校验权限。 */
(() => {
    'use strict';
    let opened = '';
    window.openSimcReferenceResource = async params => {
        const kind = params.get('simc_resource');
        const panels = {profile: 'profiles', talent: 'talent-strings', apl: 'apl'};
        if (!panels[kind] || params.get('section') !== 'simc-workflow') return;
        const identity = params.toString();
        if (opened === identity) return;
        const workbench = document.getElementById('simc-workbench');
        if (!workbench || !document.querySelector('[data-dashboard-section="simc-workflow"]')) return;
        opened = identity;
        window.switchSimcWorkbenchL1Tab('workflow', panels[kind]);
        let origin = document.getElementById('simc-reference-origin');
        if (!origin) {
            origin = document.createElement('aside');
            origin.id = 'simc-reference-origin';
            origin.className = 'simc-reference-origin';
            origin.setAttribute('aria-label', '前台数据维护上下文');
            const copy = document.createElement('div');
            const title = document.createElement('strong');
            title.textContent = '正在维护前台使用的 SimC 资料';
            const description = document.createElement('p');
            description.textContent = '修改会影响引用这份资料的组合。保存后返回管理页，检查公开范围并更新模拟结果。';
            copy.append(title, description);
            origin.append(copy, document.createElement('a'));
            workbench.prepend(origin);
        }
        const page = params.get('reference_return') === 'results' ? 'results' : 'publication';
        const target = new URL(`/dashboard/frontend-simulations/${page}/`, location.origin);
        const config = params.get('reference_config');
        if (/^[1-9]\d{0,17}$/.test(config || '')) target.searchParams.set('config', config);
        const back = origin.querySelector('a');
        back.href = target.pathname + target.search;
        back.textContent = page === 'results' ? '返回模拟结果' : '返回公开配置';
        try {
            const id = params.get('simc_id');
            if (id && !/^[1-9]\d{0,17}$/.test(id)) throw new Error('资料编号无效，请返回管理页重新打开。');
            if (!id) {
                await window.loadSimcSpecOptions();
                if (kind === 'apl') await window.loadSimcWorkbenchApl();
                const selector = {profile: 'simc-wb-profile-spec-filter', talent: 'simc-talent-string-spec-filter', apl: 'simc-apl-spec-filter'}[kind];
                const select = document.getElementById(selector);
                const spec = params.get('simc_spec');
                if (select && Array.from(select.options).some(option => option.value === spec)) {
                    select.value = spec;
                    select.dispatchEvent(new Event('change', {bubbles: true}));
                }
                return;
            }
            if (kind === 'profile') await window.simcWbEditProfile(id);
            if (kind === 'apl') await window.simcWorkbenchEditApl(id);
            if (kind === 'talent') {
                const response = await fetch(`/api/simc-talent-string/${id}/`, {credentials: 'same-origin'});
                const payload = await response.json();
                if (!response.ok || !payload.success) throw new Error(payload.error || '天赋资料不可用');
                if (!payload.data.can_edit) throw new Error('当前账号没有编辑这份共享天赋的权限。');
                window.simcTalentStringOpenEditor(payload.data);
            }
        } catch (error) {
            window.showMessage(error.message || '资料编辑器加载失败，请返回管理页重试。', 'error');
        }
    };
})();
