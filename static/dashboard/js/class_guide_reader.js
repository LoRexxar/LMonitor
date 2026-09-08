(() => {
  'use strict';
  window.initializeGuideReader = (root = document) => {
  root.querySelectorAll('[data-copy]:not([data-initialized])').forEach(button => { button.dataset.initialized = '1'; button.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(button.closest('section, .guide-code').querySelector('pre').textContent); button.textContent = '已复制'; }
    catch { button.textContent = '复制失败，请选择代码手动复制'; }
  }); });
  };
  window.initializeGuideReader();
  window.revealGuideHeading = target => {
    if (!target) return;
    const parents = []; for (let node = target.parentElement; node; node = node.parentElement) parents.unshift(node);
    parents.forEach(node => {
      if (node.matches('details')) node.open = true;
    });
    target.scrollIntoView({block:'start',behavior:'smooth'});
  };
  document.querySelectorAll('.reading-toc a').forEach(link => link.addEventListener('click', event => {
    const target = document.getElementById(link.hash.slice(1));
    if (target) { event.preventDefault(); window.revealGuideHeading(target); history.replaceState(null, '', link.hash); }
  }));
})();
