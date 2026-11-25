(async () => {
    for (const button of document.querySelectorAll('.public-role button[name=refresh_spec]')) {
        const row = button.closest('.public-role');
        try {
            const data = new FormData(button.form);
            data.set(button.name, button.value);
            const response = await fetch(button.form.action, {
                method: 'POST', body: data, headers: {Accept: 'application/json'}
            });
            if (!response.ok) continue;
            const spec = await response.json();
            row.querySelector('[data-spec-label]').textContent = spec.label;
            if (spec.icon) {
                const icon = new Image(36, 36);
                icon.src = spec.icon;
                icon.alt = '';
                row.prepend(icon);
                button.hidden = true;
            }
        } catch {
            // 网络暂不可用时保留手动更新入口。
        }
    }
})();
