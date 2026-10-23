(() => {
  "use strict";

  if (window.__wowItemTooltipInitialized) return;
  window.__wowItemTooltipInitialized = true;

  const selector = "[data-wow-item-tooltip]";
  const template = document.createElement("template");
  template.innerHTML = '<div class="wow-item-tooltip" role="tooltip" hidden></div>';
  const tooltip = template.content.firstElementChild;
  let activeTrigger = null;
  let hideTimer = null;
  let lastPointerType = "";

  function cancelHide() {
    window.clearTimeout(hideTimer);
    hideTimer = null;
  }

  function scheduleHide() {
    cancelHide();
    hideTimer = window.setTimeout(() => hide(), 120);
  }

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

  function renderContent(container, {name, description}) {
    container.replaceChildren(
      Object.assign(document.createElement("strong"), {className: "wow-item-tooltip__name", textContent: name}),
      ...lines(description).map((line) => Object.assign(document.createElement("span"), {
        className: lineClass(line), textContent: line,
      })),
    );
  }

  // 悬停浮层和页面内的装备说明共用内容与语义标记，各自使用所在区域的配色。
  window.WowItemTooltip = Object.freeze({renderContent});

  function position(trigger) {
    const rect = trigger.getBoundingClientRect();
    const gap = 8;
    tooltip.style.maxHeight = `${Math.max(0, window.innerHeight - gap * 2)}px`;
    const maxLeft = Math.max(gap, window.innerWidth - tooltip.offsetWidth - gap);
    tooltip.style.left = `${Math.min(maxLeft, Math.max(gap, rect.left))}px`;
    const belowSpace = Math.max(0, window.innerHeight - rect.bottom - gap * 2);
    const aboveSpace = Math.max(0, rect.top - gap * 2);
    const useBelow = tooltip.offsetHeight <= belowSpace || (
      tooltip.offsetHeight > aboveSpace && belowSpace >= aboveSpace
    );
    // Keep the trigger clickable; constrain the roomier side and scroll within it.
    tooltip.style.maxHeight = `${useBelow ? belowSpace : aboveSpace}px`;
    const preferredTop = useBelow ? rect.bottom + gap : rect.top - tooltip.offsetHeight - gap;
    const maxTop = Math.max(gap, window.innerHeight - tooltip.offsetHeight - gap);
    tooltip.style.top = `${Math.min(maxTop, Math.max(gap, preferredTop))}px`;
  }

  function show(trigger) {
    cancelHide();
    const description = String(trigger?.dataset.wowItemTooltip || "").trim();
    if (!description) return;
    ensureTooltipMounted();
    const name = String(trigger.dataset.wowItemTooltipName || trigger.textContent || "装备").trim();
    renderContent(tooltip, {name, description});
    const quality = trigger.dataset.wowItemTooltipQuality;
    if (/^[0-7]$/.test(quality || "")) {
      const title = tooltip.querySelector(".wow-item-tooltip__name");
      title.classList.add(`quality-${quality}`);
      const coloredName = trigger.querySelector(`.quality-${quality}`);
      if (coloredName) title.style.color = getComputedStyle(coloredName).color;
    }
    activeTrigger?.removeAttribute("aria-describedby");
    activeTrigger = trigger;
    if (!tooltip.id) tooltip.id = "wow-item-tooltip";
    trigger.setAttribute("aria-describedby", tooltip.id);
    tooltip.hidden = false;
    tooltip.scrollTop = 0;
    position(trigger);
  }

  function hide(trigger = null) {
    cancelHide();
    if (trigger && trigger !== activeTrigger) return;
    activeTrigger?.removeAttribute("aria-describedby");
    activeTrigger = null;
    tooltip.hidden = true;
  }

  ensureTooltipMounted();
  if (!tooltip.isConnected) {
    window.addEventListener("DOMContentLoaded", ensureTooltipMounted, {once: true});
  }
  document.addEventListener("pointerdown", (event) => {
    lastPointerType = event.pointerType;
  });
  document.addEventListener("pointerover", (event) => {
    if (event.pointerType === "touch") return;
    const trigger = triggerFor(event.target);
    if (trigger && !trigger.contains(event.relatedTarget)) show(trigger);
  });
  document.addEventListener("pointerout", (event) => {
    if (event.pointerType === "touch") return;
    const trigger = triggerFor(event.target);
    if (trigger && !trigger.contains(event.relatedTarget) && !tooltip.contains(event.relatedTarget)) scheduleHide();
  });
  document.addEventListener("focusin", (event) => {
    if (lastPointerType === "touch") return;
    const trigger = triggerFor(event.target);
    if (trigger) show(trigger);
  });
  document.addEventListener("focusout", (event) => {
    if (lastPointerType === "touch") return;
    const trigger = triggerFor(event.target);
    if (trigger && !trigger.contains(event.relatedTarget) && !tooltip.contains(event.relatedTarget)) scheduleHide();
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
    lastPointerType = "";
    if (event.key === "Escape") hide();
  });
  window.addEventListener("resize", () => activeTrigger && position(activeTrigger));
  tooltip.addEventListener("pointerenter", cancelHide);
  tooltip.addEventListener("pointerleave", (event) => {
    if (event.pointerType === "touch") return;
    if (!activeTrigger?.contains(event.relatedTarget)) scheduleHide();
  });
  window.addEventListener("scroll", (event) => {
    if (!tooltip.contains(event.target)) hide();
  }, true);
})();
