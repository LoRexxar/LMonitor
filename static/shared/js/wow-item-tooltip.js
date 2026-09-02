(() => {
  "use strict";

  if (window.__wowItemTooltipInitialized) return;
  window.__wowItemTooltipInitialized = true;

  const selector = "[data-wow-item-tooltip]";
  const template = document.createElement("template");
  template.innerHTML = '<div class="wow-item-tooltip" role="tooltip" hidden></div>';
  const tooltip = template.content.firstElementChild;
  let activeTrigger = null;

  function ensureTooltipMounted() {
    if (!tooltip.isConnected && document.body) document.body.append(tooltip);
  }

  function triggerFor(target) {
    return target instanceof Element ? target.closest(selector) : null;
  }

  function lines(value) {
    return String(value || "").replace(/\r\n?/g, "\n").split("\n")
      .map((line) => line.trim()).filter(Boolean);
  }

  function lineClass(line) {
    if (/^(?:装备：|使用：|被动：|效果：|Equip:|Use:|Passive:|Effect:)/i.test(line)) {
      return "wow-item-tooltip__effect";
    }
    if (/^\+\d/.test(line)) return "wow-item-tooltip__stat";
    return "wow-item-tooltip__line";
  }

  function position(trigger) {
    const rect = trigger.getBoundingClientRect();
    const gap = 8;
    const maxLeft = Math.max(gap, window.innerWidth - tooltip.offsetWidth - gap);
    tooltip.style.left = `${Math.min(maxLeft, Math.max(gap, rect.left))}px`;
    const below = rect.bottom + gap;
    const above = rect.top - tooltip.offsetHeight - gap;
    tooltip.style.top = `${below + tooltip.offsetHeight <= window.innerHeight || above < gap ? below : above}px`;
  }

  function show(trigger) {
    const description = String(trigger?.dataset.wowItemTooltip || "").trim();
    if (!description) return;
    ensureTooltipMounted();
    const name = String(trigger.dataset.wowItemTooltipName || trigger.textContent || "装备").trim();
    tooltip.replaceChildren(
      Object.assign(document.createElement("strong"), {className: "wow-item-tooltip__name", textContent: name}),
      ...lines(description).map((line) => Object.assign(document.createElement("span"), {
        className: lineClass(line), textContent: line,
      })),
    );
    activeTrigger?.removeAttribute("aria-describedby");
    activeTrigger = trigger;
    if (!tooltip.id) tooltip.id = "wow-item-tooltip";
    trigger.setAttribute("aria-describedby", tooltip.id);
    tooltip.hidden = false;
    position(trigger);
  }

  function hide(trigger = null) {
    if (trigger && trigger !== activeTrigger) return;
    activeTrigger?.removeAttribute("aria-describedby");
    activeTrigger = null;
    tooltip.hidden = true;
  }

  ensureTooltipMounted();
  if (!tooltip.isConnected) {
    window.addEventListener("DOMContentLoaded", ensureTooltipMounted, {once: true});
  }
  document.addEventListener("pointerover", (event) => {
    const trigger = triggerFor(event.target);
    if (trigger && !trigger.contains(event.relatedTarget)) show(trigger);
  });
  document.addEventListener("pointerout", (event) => {
    const trigger = triggerFor(event.target);
    if (trigger && !trigger.contains(event.relatedTarget)) hide(trigger);
  });
  document.addEventListener("focusin", (event) => {
    const trigger = triggerFor(event.target);
    if (trigger) show(trigger);
  });
  document.addEventListener("focusout", (event) => {
    const trigger = triggerFor(event.target);
    if (trigger && !trigger.contains(event.relatedTarget)) hide(trigger);
  });
  document.addEventListener("click", (event) => {
    const trigger = triggerFor(event.target);
    if (!trigger) {
      if (!tooltip.contains(event.target)) hide();
      return;
    }
    if (activeTrigger === trigger && !tooltip.hidden) hide(trigger);
    else show(trigger);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") hide();
  });
  window.addEventListener("resize", () => activeTrigger && position(activeTrigger));
  window.addEventListener("scroll", () => hide(), true);
})();
