(() => {
    const section = document.getElementById('combat-upload-policy');
    const dialog = section?.querySelector('#policy-dialog');
    if (!dialog) return;

    const form = dialog.querySelector('form');
    const fields = form.querySelector('fieldset');
    const { scope, max_reports: limit, retention_days: days, full_action: action, inherit } = form.elements;
    let draft;

    function fill(values) {
        limit.value = values.limit;
        days.value = values.days;
        action.value = values.action;
    }

    section.querySelectorAll('.policy-edit').forEach(button => {
        button.addEventListener('click', () => {
            const values = button.dataset;
            form.reset();
            fields.disabled = false;
            scope.value = values.scope;
            fill(values);
            inherit.closest('label').hidden = values.custom !== 'true' || values.scope === 'default';
            const isDefault = values.scope === 'default';
            dialog.querySelector('h2').textContent = isDefault ? '编辑全站默认设置' : '编辑账号 · ' + values.account;
            dialog.querySelector('#policy-impact').textContent = isDefault
                ? '保存后，所有跟随默认设置的账号都会使用这些值。'
                : '保存的设置仅对该网站账号生效。';
            dialog.showModal();
            limit.focus();
        });
    });
    inherit.addEventListener('change', () => {
        if (inherit.checked) {
            draft = { limit: limit.value, days: days.value, action: action.value };
            fill(section.querySelector('.policy-edit[data-scope="default"]').dataset);
        } else {
            fill(draft);
        }
        fields.disabled = inherit.checked;
    });
    dialog.querySelector('[data-dismiss]').addEventListener('click', () => dialog.close());
    const saved = section.querySelector('.policy-saved');
    if (saved) saved.closest('td').querySelector('button').focus();

    let expanded;
    let pending;

    async function loadUsage(toggle, page = 1, focusHeading = false) {
        pending?.abort();
        const request = new AbortController();
        pending = request;
        const panel = document.getElementById(toggle.getAttribute('aria-controls'));
        const url = new URL(toggle.dataset.url, location.origin);
        url.searchParams.set('page', page);
        panel.setAttribute('aria-busy', 'true');
        panel.textContent = '正在读取使用详情…';
        try {
            const response = await fetch(url, { signal: request.signal, redirect: 'error' });
            if (!response.ok) throw new Error();
            const content = await response.text();
            if (request.signal.aborted) return;
            panel.innerHTML = content;
            if (focusHeading) panel.querySelector('h3')?.focus();
        } catch {
            if (request.signal.aborted) return;
            const retry = document.createElement('button');
            retry.type = 'button';
            retry.className = 'bb-secondary';
            retry.dataset.usagePage = page;
            retry.textContent = '重试';
            panel.replaceChildren('使用详情加载失败。', retry);
            if (focusHeading) retry.focus();
        } finally {
            if (!request.signal.aborted) panel.removeAttribute('aria-busy');
        }
    }

    section.addEventListener('click', event => {
        const pager = event.target.closest('[data-usage-page]');
        if (pager && expanded) {
            loadUsage(expanded, pager.dataset.usagePage, true);
            return;
        }
        const row = event.target.closest('.policy-account');
        if (!row || event.target.closest('.policy-action') || window.getSelection()?.toString()) return;
        const toggle = row.querySelector('.policy-usage-toggle');
        const previous = expanded;
        if (previous) {
            pending?.abort();
            previous.setAttribute('aria-expanded', 'false');
            previous.closest('tr').nextElementSibling.hidden = true;
            expanded = null;
        }
        if (previous === toggle) return;
        expanded = toggle;
        toggle.setAttribute('aria-expanded', 'true');
        row.nextElementSibling.hidden = false;
        loadUsage(toggle);
    });
})();
