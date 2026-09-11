(() => {
  'use strict';

  if (window.__guideReferenceTooltipInitialized) return;
  window.__guideReferenceTooltipInitialized = true;

  let activeTrigger = null;
  let tooltip = null;
  let inputMode = 'keyboard';

  function tooltipNode() {
    if (tooltip?.isConnected) return tooltip;
    tooltip = document.getElementById('guide-reference-tooltip');
    if (tooltip) return tooltip;
    tooltip = document.createElement('div');
    tooltip.className = 'guide-reference-tooltip';
    tooltip.id = 'guide-reference-tooltip';
    tooltip.setAttribute('role', 'tooltip');
    tooltip.hidden = true;
    tooltip.innerHTML = '<strong class="guide-reference-tooltip__title"></strong><div class="guide-reference-tooltip__body"></div>';
    document.body.appendChild(tooltip);
    return tooltip;
  }

  function hide(trigger = activeTrigger) {
    if (trigger && activeTrigger && trigger !== activeTrigger) return;
    if (activeTrigger?.isConnected) activeTrigger.removeAttribute('aria-describedby');
    activeTrigger = null;
    if (tooltip) tooltip.hidden = true;
  }

  function position(trigger) {
    if (!trigger?.isConnected) {
      hide();
      return;
    }
    const node = tooltipNode();
    const rect = trigger.getBoundingClientRect();
    const gap = 8;
    const margin = 8;
    const width = node.offsetWidth;
    const height = node.offsetHeight;
    let left = rect.left + (rect.width - width) / 2;
    left = Math.max(margin, Math.min(left, window.innerWidth - width - margin));
    let top = rect.bottom + gap;
    if (top + height > window.innerHeight - margin) {
      top = Math.max(margin, rect.top - height - gap);
    }
    top = Math.min(top, Math.max(margin, window.innerHeight - height - margin));
    node.style.left = `${Math.round(left)}px`;
    node.style.top = `${Math.round(top)}px`;
  }

  function show(trigger) {
    if (!trigger?.isConnected || !trigger.matches('.guide-ref[data-guide-tooltip]')) return;
    const node = tooltipNode();
    node.querySelector('.guide-reference-tooltip__title').textContent = trigger.dataset.tooltipTitle || trigger.textContent.trim();
    node.querySelector('.guide-reference-tooltip__body').textContent = trigger.dataset.tooltipBody || '';
    if (!node.querySelector('.guide-reference-tooltip__body').textContent) return;
    if (activeTrigger && activeTrigger !== trigger && activeTrigger.isConnected) {
      activeTrigger.removeAttribute('aria-describedby');
    }
    activeTrigger = trigger;
    trigger.setAttribute('aria-describedby', node.id);
    node.hidden = false;
    position(trigger);
  }

  function triggerFor(target) {
    return target instanceof Element ? target.closest('.guide-ref[data-guide-tooltip]') : null;
  }

  document.addEventListener('pointerdown', (event) => {
    inputMode = event.pointerType || 'mouse';
  });
  document.addEventListener('pointerover', (event) => {
    if (event.pointerType === 'mouse') show(triggerFor(event.target));
  });
  document.addEventListener('pointerout', (event) => {
    if (event.pointerType !== 'mouse') return;
    const trigger = triggerFor(event.target);
    if (trigger && !trigger.contains(event.relatedTarget) && !tooltip?.contains(event.relatedTarget)) hide(trigger);
  });
  document.addEventListener('focusin', (event) => {
    if (!['touch', 'pen'].includes(inputMode)) show(triggerFor(event.target));
  });
  document.addEventListener('focusout', (event) => {
    const trigger = triggerFor(event.target);
    if (trigger && !trigger.contains(event.relatedTarget) && !tooltip?.contains(event.relatedTarget)) hide(trigger);
  });
  document.addEventListener('click', (event) => {
    const trigger = triggerFor(event.target);
    if (!trigger) {
      if (!tooltip?.contains(event.target)) hide();
      return;
    }
    if (['touch', 'pen'].includes(inputMode)) {
      if (activeTrigger === trigger && tooltip && !tooltip.hidden) hide(trigger);
      else show(trigger);
    }
  });
  document.addEventListener('keydown', (event) => {
    inputMode = 'keyboard';
    if (event.key === 'Escape') hide();
  });
  window.addEventListener('resize', () => activeTrigger ? position(activeTrigger) : null);
  window.addEventListener('scroll', (event) => {
    if (!tooltip?.contains(event.target)) hide();
  }, true);

  window.GuideReferenceTooltip = { hide };
})();
