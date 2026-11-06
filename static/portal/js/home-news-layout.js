// Progressive enhancement: keep complete source cards and their DOM order.
(function () {
  'use strict';
  let initialized = false;

  function init() {
    if (initialized) return;
    initialized = true;
    const grid = document.getElementById('home-news-sources');
    if (!grid || typeof ResizeObserver !== 'function' || typeof requestAnimationFrame !== 'function') return;
    const cards = Array.from(grid.children);
    let pending = false;

    function setSpan(card, value) {
      if (card.style.getPropertyValue('--news-row-span') === value) return;
      if (value) card.style.setProperty('--news-row-span', value);
      else card.style.removeProperty('--news-row-span');
    }

    function layout() {
      pending = false;
      const style = getComputedStyle(grid);
      // Resolved CSS tracks are the breakpoint authority, not a second JS width.
      const columns = style.gridTemplateColumns.trim().split(/\s+/);
      if (columns.length < 2 || style.gridTemplateColumns === 'none') {
        grid.classList.remove('is-masonry');
        cards.forEach(card => setSpan(card, ''));
        return;
      }
      const gap = parseFloat(style.columnGap) || 0;
      // Read natural border boxes before writes; align-items:start prevents stretch.
      const spans = cards.map(card => {
        const height = card.getBoundingClientRect().height;
        return !card.hidden && height > 0 ? String(Math.ceil(height + gap)) : '';
      });
      cards.forEach((card, index) => setSpan(card, spans[index]));
      grid.classList.add('is-masonry');
    }

    function schedule() {
      if (pending) return;
      pending = true;
      requestAnimationFrame(layout);
    }

    const observer = new ResizeObserver(schedule);
    observer.observe(grid);
    cards.forEach(card => observer.observe(card));
    schedule();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once: true});
  else init();
})();
