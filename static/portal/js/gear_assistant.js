(() => {
  "use strict";

  const page = document.querySelector(".gear-assistant-page");
  if (!page) return;

  const endpoints = {
    builderBootstrap: page.dataset.builderBootstrapUrl,
    bootstrap: page.dataset.assistantBootstrapUrl,
    optimize: page.dataset.optimizeUrl,
    owned: page.dataset.ownedUrl,
    simc: page.dataset.simcUrl,
    builder: page.dataset.builderUrl,
  };
  const byId = (id) => document.getElementById(id);
  const els = {
    classSelect: byId("assistant-class"), specSelect: byId("assistant-spec"), ownedList: byId("assistant-owned-list"),
    flask: byId("assistant-flask"), gems: byId("assistant-gems"), lockGems: byId("assistant-lock-gems"),
    enchants: byId("assistant-enchants"), lockEnchants: byId("assistant-lock-enchants"), ai: byId("assistant-ai"),
    generate: byId("assistant-generate"), fixedCount: byId("assistant-fixed-count"), fixedChips: byId("assistant-fixed-chips"),
    catalogStatus: byId("assistant-catalog-status"), results: byId("assistant-results"), explanation: byId("assistant-explanation"),
    importSimc: byId("assistant-import-simc"), simcDialog: byId("assistant-simc-dialog"), simcInput: byId("assistant-simc-input"),
    simcSubmit: byId("assistant-simc-submit"), simcMessage: byId("assistant-simc-message"), toastRoot: byId("assistant-toast-root"),
  };
  const STAT_LABELS = {crit: "暴击", haste: "急速", mastery: "精通", versatility: "全能"};
  const DRAFT_KEY = "wowdaily:gear-assistant:draft:v1";
  let builderBootstrap = null;
  let assistantData = null;
  let currentState = null;
  let ownedFilter = "slot";
  let lastPlans = [];

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (character) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"})[character]);
  }
  function csrfHeaders() {
    const token = document.querySelector('input[name="csrfmiddlewaretoken"]')?.value || "";
    return token ? {"X-CSRFToken": token} : {};
  }
  async function requestJson(url, options = {}) {
    const response = await fetch(url, {headers: {Accept: "application/json", ...(options.headers || {})}, ...options});
    let payload = {};
    try { payload = await response.json(); } catch (_error) { /* 统一错误 */ }
    if (!response.ok || payload.success === false) throw new Error(payload.error || `请求失败（${response.status}）`);
    return payload;
  }
  function toast(message, isError = false) {
    const node = document.createElement("div");
    node.className = `assistant-toast${isError ? " is-error" : ""}`;
    node.textContent = message;
    els.toastRoot.append(node);
    window.setTimeout(() => node.remove(), 3600);
  }
  function stateKey(className, specName) { return `wowdaily:gear-builder:v1:${className}:${specName}`; }
  function readDraft() {
    try {
      const draft = JSON.parse(sessionStorage.getItem(DRAFT_KEY) || "null");
      if (draft?.className && draft?.specName) return draft;
    } catch (_error) { /* 回退本地配装 */ }
    const firstClass = builderBootstrap?.classes?.[0];
    const className = firstClass?.key || "Warrior";
    const specName = firstClass?.specs?.[0]?.key || "Fury";
    try {
      return JSON.parse(localStorage.getItem(stateKey(className, specName)) || "null") || {className, specName, equipment: {}, selectedSlot: "head"};
    } catch (_error) {
      return {className, specName, equipment: {}, selectedSlot: "head"};
    }
  }
  function loadState(className, specName) {
    try {
      return JSON.parse(localStorage.getItem(stateKey(className, specName)) || "null") || {version: 1, className, specName, equipment: {}, selectedSlot: "head", mode: "equipment", viewMode: "editor", mobileView: "browser"};
    } catch (_error) {
      return {version: 1, className, specName, equipment: {}, selectedSlot: "head", mode: "equipment", viewMode: "editor", mobileView: "browser"};
    }
  }
  function syncSelectors() {
    els.classSelect.innerHTML = (builderBootstrap?.classes || []).map((row) => `<option value="${escapeHtml(row.key)}">${escapeHtml(row.name)}</option>`).join("");
    els.classSelect.value = currentState.className;
    const selectedClass = (builderBootstrap?.classes || []).find((row) => row.key === currentState.className) || builderBootstrap?.classes?.[0];
    els.specSelect.innerHTML = (selectedClass?.specs || []).map((row) => `<option value="${escapeHtml(row.key)}">${escapeHtml(row.name)}</option>`).join("");
    if (![...els.specSelect.options].some((row) => row.value === currentState.specName)) currentState.specName = els.specSelect.options[0]?.value || "";
    els.specSelect.value = currentState.specName;
  }
  function renderFixed() {
    const entries = Object.entries(currentState?.equipment || {}).filter(([, value]) => value?.item);
    els.fixedCount.textContent = `${entries.length} / 16`;
    els.fixedChips.innerHTML = entries.length ? entries.map(([slot, entry]) => `<span>${escapeHtml((builderBootstrap?.slots || []).find((row) => row.key === slot)?.label || slot)} · ${escapeHtml(entry.item.name)}</span>`).join("") : "<span>暂无锁定装备</span>";
  }
  function renderOwned() {
    let rows = assistantData?.owned_items || [];
    if (ownedFilter === "slot") {
      const selectedSlots = new Set(Object.keys(currentState?.equipment || {}));
      if (selectedSlots.size) rows = rows.filter((row) => selectedSlots.has(row.slot));
    }
    if (!rows.length) {
      els.ownedList.innerHTML = '<div class="assistant-list-empty">还没有已有装备。可从职业配装器加入，或导入 SimC 背包。</div>';
      return;
    }
    els.ownedList.innerHTML = rows.map((row) => {
      const icon = row.item?.icon_url ? `<img class="assistant-owned-icon" src="${escapeHtml(row.item.icon_url)}" alt="" loading="lazy">` : '<span class="assistant-owned-icon"></span>';
      return `<div class="assistant-owned-row" data-owned-id="${row.id}">${icon}<span class="assistant-owned-copy"><strong>${escapeHtml(row.name)}</strong><small>${escapeHtml(row.slot_label)} · ${row.item_level || "未知装等"}${row.quantity > 1 ? ` · ×${row.quantity}` : ""}</small></span><button type="button" class="assistant-owned-delete" data-delete-owned="${row.id}" aria-label="移除已有装备">×</button></div>`;
    }).join("");
  }
  function renderFlasks() {
    els.flask.innerHTML = (assistantData?.flasks || []).map((row) => `<option value="${escapeHtml(row.key)}">${escapeHtml(row.name)}</option>`).join("");
    els.flask.value = "auto";
  }
  async function loadAssistantData() {
    assistantData = await requestJson(`${endpoints.bootstrap}?class=${encodeURIComponent(currentState.className)}&spec=${encodeURIComponent(currentState.specName)}`);
    renderOwned();
    renderFlasks();
    els.catalogStatus.textContent = assistantData.catalog?.available ? `${assistantData.catalog.season_name || "当前赛季"} · 装备目录已就绪` : "装备目录尚未同步";
  }
  function targetPayload() {
    return Object.fromEntries(Object.keys(STAT_LABELS).map((key) => [key, Number(byId(`target-${key}`).value || 0)]));
  }
  function missingMarkup(rows) {
    if (!rows?.length) return '<div class="assistant-plan-meta"><span>全部装备均已拥有或已锁定</span></div>';
    return `<details class="assistant-missing"><summary>缺失装备与来源（${rows.length}）</summary><div class="assistant-missing-list">${rows.map((row) => `<div class="assistant-missing-row"><span>${escapeHtml(row.slot_label)}</span><div><strong>${escapeHtml(row.name)} · ${row.item_level || "-"}</strong><small>${escapeHtml(row.source)}</small></div></div>`).join("")}</div></details>`;
  }
  function renderPlans(plans) {
    if (!plans?.length) return;
    const bestDistance = Math.min(...plans.map((row) => Number(row.distance)));
    els.results.innerHTML = plans.map((plan) => `<article class="assistant-plan${Number(plan.distance) === bestDistance ? " is-best" : ""}">
      <header class="assistant-plan-head"><span class="assistant-plan-title"><strong>${escapeHtml(plan.name)}</strong><small>${plan.equipped_count}/16 件 · 已有 ${plan.owned_count} 件</small></span><span class="assistant-plan-badge">偏差 ${plan.distance}${Number(plan.distance) === bestDistance ? " · 最接近" : ""}</span></header>
      <div class="assistant-plan-body"><div class="assistant-plan-stats">${Object.entries(STAT_LABELS).map(([key, label]) => `<div class="assistant-plan-stat"><span>${label}</span><strong>${Number(plan.percentages?.[key] || 0).toFixed(2)}%</strong></div>`).join("")}</div>
      <div class="assistant-plan-meta"><span>合剂：${escapeHtml(plan.flask?.name || "无")}</span><span>宝石与附魔已计入最终属性</span></div>${missingMarkup(plan.missing_items)}</div>
      <footer class="assistant-plan-actions"><button type="button" class="assistant-btn assistant-btn--primary" data-apply-plan="${escapeHtml(plan.key)}">应用到职业配装器</button></footer>
    </article>`).join("");
  }
  async function generate() {
    els.generate.disabled = true;
    els.generate.textContent = "正在组合装备…";
    els.results.innerHTML = '<div class="assistant-empty-state"><strong>正在搜索组合</strong><span>会依次计算已有优先、全装备池和仅地下城方案。</span></div>';
    try {
      const payload = await requestJson(endpoints.optimize, {
        method: "POST", headers: {"Content-Type": "application/json", ...csrfHeaders()},
        body: JSON.stringify({
          class_name: currentState.className, spec_name: currentState.specName, equipment: currentState.equipment || {},
          target: targetPayload(), flask: els.flask.value, include_gems: els.gems.checked,
          lock_gems: els.lockGems.checked, include_enchants: els.enchants.checked,
          lock_enchants: els.lockEnchants.checked, use_ai: els.ai.checked,
        }),
      });
      lastPlans = payload.plans || [];
      renderPlans(lastPlans);
      els.explanation.textContent = payload.explanation || "方案计算完成。";
      toast(`已生成 ${lastPlans.length} 套方案。`);
    } catch (error) {
      els.results.innerHTML = `<div class="assistant-empty-state"><strong>组合失败</strong><span>${escapeHtml(error.message)}</span></div>`;
      toast(error.message, true);
    } finally {
      els.generate.disabled = false;
      els.generate.textContent = "生成三套配装方案";
    }
  }
  function applyPlan(key) {
    const plan = lastPlans.find((row) => row.key === key);
    if (!plan) return;
    const next = {...currentState, equipment: plan.equipment || {}, batchKey: assistantData?.catalog?.batch_key || currentState.batchKey, mode: "equipment", viewMode: "editor"};
    localStorage.setItem(stateKey(next.className, next.specName), JSON.stringify(next));
    sessionStorage.setItem(DRAFT_KEY, JSON.stringify(next));
    window.location.href = endpoints.builder;
  }
  async function importSimc() {
    const profile = els.simcInput.value.trim();
    if (!profile) { els.simcMessage.textContent = "请先粘贴 SimC Profile。"; return; }
    els.simcSubmit.disabled = true;
    els.simcMessage.textContent = "正在解析已装备物品与背包区块…";
    try {
      const parsed = await requestJson(endpoints.simc, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({profile})});
      const items = (parsed.owned_equipment || []).filter((row) => row.item_id && row.slot).map((row) => ({
        variant_id: row.variant_id, item_id: row.item_id, slot: row.slot, item_level: row.item_level,
        bonus_ids: row.bonus_ids || [], selected_stats: row.crafted_stats || [], source: row.import_source || "simc_bag",
        name: row.name, enhancements: {gems: row.gems || [], enchant: row.enchant || null},
        snapshot: {name: row.name, stats: row.variant?.stats || {}, sources: row.variant?.sources || []},
      }));
      const saved = await requestJson(endpoints.owned, {method: "POST", headers: {"Content-Type": "application/json", ...csrfHeaders()}, body: JSON.stringify({items})});
      els.simcMessage.textContent = `已加入 ${saved.count || 0} 条已有装备记录。`;
      currentState.className = parsed.identity?.class_name || currentState.className;
      currentState.specName = parsed.identity?.spec_name || currentState.specName;
      syncSelectors();
      await loadAssistantData();
      toast(`已导入 ${saved.count || 0} 件装备。`);
    } catch (error) {
      els.simcMessage.textContent = error.message;
    } finally {
      els.simcSubmit.disabled = false;
    }
  }
  function bindEvents() {
    els.classSelect.addEventListener("change", async () => {
      const classRow = (builderBootstrap.classes || []).find((row) => row.key === els.classSelect.value);
      currentState = loadState(classRow.key, classRow.specs?.[0]?.key || "");
      syncSelectors(); renderFixed(); await loadAssistantData();
    });
    els.specSelect.addEventListener("change", async () => {
      currentState = loadState(els.classSelect.value, els.specSelect.value);
      syncSelectors(); renderFixed(); await loadAssistantData();
    });
    document.querySelectorAll("[data-owned-filter]").forEach((button) => button.addEventListener("click", () => {
      ownedFilter = button.dataset.ownedFilter;
      document.querySelectorAll("[data-owned-filter]").forEach((row) => row.classList.toggle("is-active", row === button));
      renderOwned();
    }));
    els.ownedList.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-delete-owned]");
      if (!button) return;
      try {
        await requestJson(`${endpoints.owned}${button.dataset.deleteOwned}/`, {method: "DELETE", headers: csrfHeaders()});
        assistantData.owned_items = assistantData.owned_items.filter((row) => String(row.id) !== button.dataset.deleteOwned);
        renderOwned(); toast("已从已有装备中移除。");
      } catch (error) { toast(error.message, true); }
    });
    els.generate.addEventListener("click", generate);
    els.results.addEventListener("click", (event) => { const button = event.target.closest("[data-apply-plan]"); if (button) applyPlan(button.dataset.applyPlan); });
    els.importSimc.addEventListener("click", () => els.simcDialog.showModal());
    els.simcSubmit.addEventListener("click", importSimc);
  }
  async function initialize() {
    try {
      builderBootstrap = await requestJson(endpoints.builderBootstrap);
      currentState = readDraft();
      syncSelectors(); renderFixed(); bindEvents();
      await loadAssistantData();
    } catch (error) {
      els.catalogStatus.textContent = "初始化失败";
      els.results.innerHTML = `<div class="assistant-empty-state"><strong>页面初始化失败</strong><span>${escapeHtml(error.message)}</span></div>`;
    } finally {
      page.setAttribute("aria-busy", "false");
    }
  }
  initialize();
})();
