/* 独立榜单页：普通链接负责导航，仅增强当前配置的键盘定位。 */
(() => {
    document.querySelector('[data-ranking-locate]')?.addEventListener('click', () => {
        document.querySelector('[data-ranking-current]')?.focus({preventScroll: true});
    });
})();
