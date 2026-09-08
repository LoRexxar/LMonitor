(() => {
  'use strict';
  window.initializeGuideReader = (root = document) => {
  root.querySelectorAll('[data-guide-tabs]:not([data-initialized])').forEach(group => {
    group.dataset.initialized = '1';
    const panels = Array.from(group.children).filter(node => node.matches('[data-guide-tab]'));
    if (!panels.length) return;
    const bar = document.createElement('div'); bar.className = 'guide-tab-bar'; bar.setAttribute('role', 'tablist');
    panels.forEach((panel, index) => {
      const button = document.createElement('button'); button.className = 'secondary'; button.type = 'button';
      button.textContent = panel.querySelector('.block-title')?.textContent || `方案 ${index + 1}`;
      button.id = `${panel.id}-tab`; button.setAttribute('role', 'tab'); button.setAttribute('aria-controls', panel.id);
      panel.setAttribute('role', 'tabpanel'); panel.setAttribute('aria-labelledby', button.id);
      const activate = () => panels.forEach((p, i) => { p.hidden = i !== index; bar.children[i].setAttribute('aria-selected', String(i === index)); bar.children[i].tabIndex = i === index ? 0 : -1; });
      button.addEventListener('click', activate);
      button.addEventListener('keydown', event => { if (['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(event.key)) { event.preventDefault(); const i = event.key === 'Home' ? 0 : event.key === 'End' ? panels.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + panels.length) % panels.length; bar.children[i].click(); bar.children[i].focus(); } });
      bar.append(button); panel.hidden = index !== 0; button.setAttribute('aria-selected', String(index === 0)); button.tabIndex = index === 0 ? 0 : -1;
    });
    group.prepend(bar);
  });
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
      if (node.matches('[role="tabpanel"]')) document.getElementById(node.getAttribute('aria-labelledby'))?.click();
    });
    target.scrollIntoView({block:'start',behavior:'smooth'});
  };
  document.querySelectorAll('.reading-toc a').forEach(link => link.addEventListener('click', event => {
    const target = document.getElementById(link.hash.slice(1));
    if (target) { event.preventDefault(); window.revealGuideHeading(target); history.replaceState(null, '', link.hash); }
  }));
})();
