/* 发布页只维护选择状态；真实发布与模拟仍由服务端 POST 执行。 */
document.addEventListener('DOMContentLoaded', () => {
    const form = document.querySelector('[data-publication-form]');
    if (!form) return;
    const boxes = Array.from(form.querySelectorAll('input[type="checkbox"]'));
    const initial = boxes.map(input => input.checked).join(',');
    let dirty = false;
    const refresh = () => {
        const counts = {};
        form.querySelectorAll('[data-resource-section]').forEach(section => {
            const checked = section.querySelectorAll('input[type="checkbox"]:checked').length;
            counts[section.dataset.resourceSection] = checked;
            section.querySelector('[data-selection-count]').textContent = `${checked} / ${section.querySelectorAll('input[type="checkbox"]').length}`;
        });
        dirty = boxes.map(input => input.checked).join(',') !== initial;
        form.querySelector('[data-publication-summary]').textContent = `${counts.profiles || 0} 份装备 × ${counts.talents || 0} 份天赋 × ${counts.apls || 0} 份 APL · ${(counts.profiles || 0) * (counts.talents || 0) * (counts.apls || 0)} 个组合`;
        form.querySelector('[data-save-status]').textContent = dirty ? '有未保存的修改' : '公开范围已与保存状态一致';
    };
    form.addEventListener('change', refresh);
    form.addEventListener('submit', () => { dirty = false; });
    window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
    form.querySelectorAll('[data-resource-search]').forEach(input => {
        input.addEventListener('input', () => {
            const section = input.closest('[data-resource-section]');
            const query = input.value.trim().toLocaleLowerCase();
            let visible = 0;
            section.querySelectorAll('[data-resource-row]').forEach(row => {
                row.hidden = !row.dataset.resourceName.toLocaleLowerCase().includes(query);
                if (!row.hidden) visible++;
            });
            section.querySelector('[data-search-empty]').hidden = visible > 0;
        });
    });
    refresh();
});
