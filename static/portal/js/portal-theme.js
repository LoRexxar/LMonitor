(() => {
    const storageKey = 'lmonitor-portal-theme';
    const root = document.documentElement;

    const applyTheme = (theme) => {
        const isDark = theme === 'dark';
        root.classList.toggle('portal-theme-dark', isDark);
        root.style.colorScheme = isDark ? 'dark' : 'light';

        const toggle = document.getElementById('portal-theme-toggle');
        if (!toggle) return;
        toggle.setAttribute('aria-pressed', String(isDark));
        toggle.setAttribute('aria-label', isDark ? '切换为浅色模式' : '切换为深色模式');
        toggle.title = isDark ? '切换为浅色模式' : '切换为深色模式';
    };

    const savedTheme = window.localStorage.getItem(storageKey);
    applyTheme(savedTheme === 'dark' ? 'dark' : 'light');

    document.addEventListener('DOMContentLoaded', () => {
        applyTheme(root.classList.contains('portal-theme-dark') ? 'dark' : 'light');
        document.getElementById('portal-theme-toggle')?.addEventListener('click', () => {
            const nextTheme = root.classList.contains('portal-theme-dark') ? 'light' : 'dark';
            window.localStorage.setItem(storageKey, nextTheme);
            applyTheme(nextTheme);
        });
    });
})();
