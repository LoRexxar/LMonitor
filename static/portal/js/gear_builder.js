(() => {
  "use strict";

  const page = document.querySelector(".gear-builder-page");
  if (!page) return;

  const endpoints = {
    bootstrap: page.dataset.bootstrapUrl,
    catalog: page.dataset.catalogUrl,
    enhancements: page.dataset.enhancementsUrl,
    crafted: page.dataset.craftedUrl,
    simc: page.dataset.simcUrl,
  };
  const els = Object.fromEntries([
    "gear-class-select", "gear-spec-select", "gear-catalog-status", "gear-slot-list",
    "gear-equipped-count", "gear-mobile-count", "gear-browser-title", "gear-mode-equipment",
    "gear-mode-enhancement", "gear-equipment-browser", "gear-enhancement-browser",
    "gear-search-input", "gear-source-filter", "gear-candidate-list", "gear-load-more",
    "gear-embellishment-list", "gear-gem-list", "gear-enchant-list", "gear-socket-summary",
    "gear-add-socket-option", "gear-add-socket", "gear-add-socket-copy", "gear-stats-context",
    "gear-detail-panel", "gear-detail-content", "gear-detail-close", "gear-stat-grid",
    "gear-effect-list", "gear-save-status", "gear-import-simc", "gear-copy-share", "gear-clear",
    "gear-simc-dialog", "gear-simc-input", "gear-simc-submit", "gear-simc-message", "gear-toast-root",
    "gear-view-editor", "gear-view-preview", "gear-preview", "gear-preview-left", "gear-preview-right",
    "gear-preview-season", "gear-preview-count", "gear-preview-spec-icon", "gear-preview-spec-fallback", "gear-preview-class",
    "gear-preview-spec", "gear-preview-item-level", "gear-preview-progress-bar", "gear-preview-progress-copy",
    "gear-preview-stats", "gear-preview-effects",
  ].map((id) => [id.replace(/^gear-/, "").replaceAll("-", "_"), document.getElementById(id)]));

  const STAT_LABELS = {
    strength: "力量", agility: "敏捷", intellect: "智力", stamina: "耐力", armor: "护甲",
    bonus_armor: "额外护甲", crit: "暴击", haste: "急速", mastery: "精通", versatility: "全能",
    leech: "吸血", avoidance: "闪避", speed: "速度", weapon_dps: "武器秒伤",
    min_damage: "最低伤害", max_damage: "最高伤害",
  };
  const SUMMARY_STATS = ["crit", "haste", "mastery", "versatility"];
  const STAT_COLORS = {
    strength: "#cf2f2f", agility: "#1e9a50", intellect: "#3978d9", stamina: "#7d59c4",
    crit: "#ed7b2d", haste: "#24a7bd", mastery: "#7c3aed", versatility: "#c59d28",
  };
  const SECONDARY_STATS = new Set(["crit", "haste", "mastery", "versatility"]);
  const BASE_SECONDARY_PERCENTAGES = Object.freeze({
    crit: 5,
    mastery: 8,
  });
  const ADDITIONAL_SOCKET_SLOTS = new Set(["head", "wrists", "waist"]);
  const PREVIEW_LEFT_SLOTS = ["head", "neck", "shoulders", "back", "chest", "wrists", "hands", "waist"];
  const PREVIEW_RIGHT_SLOTS = ["legs", "feet", "finger1", "finger2", "trinket1", "trinket2", "main_hand", "off_hand"];
  const SOURCE_LABELS = {
    mythic_plus: "大秘境", great_vault: "宏伟宝库", raid: "团队副本", delve: "地下堡",
    crafted: "专业制造", profession: "专业制造", bonus_roll: "额外掉落",
  };
  const SOURCE_PLACE_FALLBACKS = {
    mythic_plus: "当前大秘境", great_vault: "宏伟宝库", raid: "当前团队副本",
    delve: "当前赛季地下堡", crafted: "专业制造", profession: "专业制造",
  };

  let bootstrap = null;
  let candidates = [];
  let candidateTotal = 0;
  let candidatePage = 1;
  let candidateLoading = false;
  let searchTimer = 0;
  let enhancementGroups = {embellishments: [], gems: [], enchants: []};
  let state = freshState();

  function freshState() {
    return {
      version: 1,
      className: "Warrior",
      specName: "Fury",
      batchKey: "",
      selectedSlot: "head",
      mode: "equipment",
      viewMode: "editor",
      mobileView: "browser",
      equipment: {},
    };
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
    })[character]);
  }

  function number(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function formatNumber(value) {
    const parsed = number(value);
    return Number.isInteger(parsed)
      ? new Intl.NumberFormat("zh-CN").format(parsed)
      : new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 2}).format(parsed);
  }

  function iconMarkup(item, className = "gear-item-icon") {
    return item?.icon_url
      ? `<img class="${className}" src="${escapeHtml(item.icon_url)}" alt="" loading="lazy">`
      : `<span class="${className} gear-slot-placeholder" aria-hidden="true">◇</span>`;
  }

  function tooltipText(item, variant) {
    const lines = [];
    if (variant?.item_level) lines.push(`物品等级 ${variant.item_level}`);
    Object.entries(variant?.stats || {}).forEach(([key, value]) => lines.push(`+${formatNumber(value)} ${STAT_LABELS[key] || key}`));
    (variant?.effects || []).forEach((effect) => lines.push(effectText(effect)));
    if (item?.description) lines.push(item.description);
    return lines.filter(Boolean).join("\n");
  }

  function tooltipAttrs(item, variant) {
    const text = tooltipText(item, variant);
    return text
      ? ` tabindex="0" data-wow-item-tooltip="${escapeHtml(text)}" data-wow-item-tooltip-name="${escapeHtml(item?.name || "物品")}"`
      : "";
  }

  function effectText(effect) {
    if (typeof effect === "string") return effect;
    return effect?.description_zh || effect?.description || effect?.name_zh || effect?.name || "";
  }

  function sourceText(variant) {
    const rows = Array.isArray(variant?.sources) ? variant.sources : [];
    if (!rows.length) return "来源待补全";
    return rows.slice(0, 2).map((row) => {
      if (typeof row === "string") return row;
      const type = row.type_zh || SOURCE_LABELS[row.type] || "其他来源";
      const instance = row.instance_zh || "";
      const encounter = row.encounter_zh || row.boss_zh || "";
      const profession = row.profession_zh || "";
      const difficulty = row.difficulty_zh || "";
      const parts = [type, instance, encounter, profession, difficulty].filter(Boolean);
      if (parts.length === 1 && SOURCE_PLACE_FALLBACKS[row.type]) parts.push(SOURCE_PLACE_FALLBACKS[row.type]);
      return [...new Set(parts)].join(" · ");
    }).join("\n");
  }

  function sourceMarkup(variant) {
    return sourceText(variant).split("\n").map((line) => `<span>${escapeHtml(line)}</span>`).join("");
  }

  function statMarkup(stats, limit = 4) {
    const rows = Object.entries(stats || {}).filter(([key, value]) => SECONDARY_STATS.has(key) && number(value)).slice(0, limit);
    return rows.length
      ? rows.map(([key, value]) => `<span>${escapeHtml(STAT_LABELS[key] || key)} ${formatNumber(value)}</span>`).join("")
      : "<span>无常驻绿字</span>";
  }

  function variantLabel(variant) {
    if (!variant) return "未知变体";
    if (variant.type === "crafted_equipment") {
      return `制造 ${variant.crafting_quality ? `${variant.crafting_quality}星` : ""} · ${variant.item_level}`;
    }
    const rank = variant.track_rank ? ` ${variant.track_rank}/${variant.track_max_rank || variant.track_rank}` : "";
    return `${variant.track_label || variant.track || "装备"}${rank} · ${variant.item_level || "-"}`;
  }

  function storageKey(className = state.className, specName = state.specName) {
    return `wowdaily:gear-builder:v1:${className}:${specName}`;
  }

  function compactItem(item) {
    if (!item) return null;
    return {
      item_id: item.item_id,
      name: item.name,
      name_en: item.name_en || "",
      description: item.description || "",
      icon: item.icon || "",
      icon_url: item.icon_url || "",
      quality: item.quality || 0,
      slot: item.slot || "",
      armor_type: item.armor_type || "",
      weapon_type: item.weapon_type || "",
      unique_group: item.unique_group || "",
      simc_token: item.simc_token || "",
    };
  }

  function normalizeState(raw) {
    const next = freshState();
    if (!raw || typeof raw !== "object") return next;
    next.className = String(raw.className || next.className);
    next.specName = String(raw.specName || next.specName);
    next.batchKey = String(raw.batchKey || "");
    next.selectedSlot = String(raw.selectedSlot || next.selectedSlot);
    next.mode = raw.mode === "enhancement" ? "enhancement" : "equipment";
    next.viewMode = raw.viewMode === "preview" ? "preview" : "editor";
    next.mobileView = ["slots", "browser", "stats"].includes(raw.mobileView) ? raw.mobileView : "browser";
    next.equipment = raw.equipment && typeof raw.equipment === "object" ? raw.equipment : {};
    return next;
  }

  function loadStored(className, specName) {
    try {
      return normalizeState(JSON.parse(localStorage.getItem(storageKey(className, specName)) || "null"));
    } catch (_error) {
      return normalizeState(null);
    }
  }

  function persist() {
    state.batchKey = bootstrap?.catalog?.batch_key || state.batchKey || "";
    localStorage.setItem(storageKey(), JSON.stringify(state));
    els.save_status.textContent = `已保存到浏览器 · ${new Date().toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit"})}`;
  }

  function toast(message, error = false) {
    const node = document.createElement("div");
    node.className = `gear-toast${error ? " is-error" : ""}`;
    node.textContent = message;
    els.toast_root.append(node);
    window.setTimeout(() => node.remove(), 3200);
  }

  async function requestJson(url, options = {}) {
    const response = await fetch(url, {headers: {"Accept": "application/json", ...(options.headers || {})}, ...options});
    let payload = {};
    try { payload = await response.json(); } catch (_error) { /* 使用统一错误 */ }
    if (!response.ok || payload.success === false) throw new Error(payload.error || `请求失败（${response.status}）`);
    return payload;
  }

  function selectedEntry() {
    return state.equipment[state.selectedSlot] || null;
  }

  function slotFamily(slot = state.selectedSlot) {
    return bootstrap?.slots?.find((row) => row.key === slot)?.family || slot;
  }

  function socketRule(slot = state.selectedSlot) {
    const family = slotFamily(slot);
    if (!ADDITIONAL_SOCKET_SLOTS.has(family)) return null;
    return (bootstrap?.rules?.socket_additions || []).find((row) => row.slot === slot || row.slot === family) || null;
  }

  function socketCapacity(entry = selectedEntry()) {
    if (!entry) return 0;
    const nativeCount = Number(entry.variant?.socket_count || 0);
    return nativeCount + (entry.addedSocket ? Number(socketRule()?.max_additional || 0) : 0);
  }

  function primaryStatKey() {
    const identity = `${state.className}:${state.specName}`;
    const intellect = new Set([
      "Paladin:Holy", "Priest:Discipline", "Priest:Holy", "Priest:Shadow",
      "Shaman:Elemental", "Shaman:Restoration", "Mage:Arcane", "Mage:Fire", "Mage:Frost",
      "Warlock:Affliction", "Warlock:Demonology", "Warlock:Destruction", "Monk:Mistweaver",
      "Druid:Balance", "Druid:Restoration", "Evoker:Devastation", "Evoker:Preservation", "Evoker:Augmentation",
    ]);
    const agility = new Set([
      "Hunter", "Rogue", "DemonHunter", "Shaman:Enhancement", "Monk:Brewmaster",
      "Monk:Windwalker", "Druid:Feral", "Druid:Guardian",
    ]);
    if (intellect.has(identity)) return "intellect";
    if (agility.has(state.className) || agility.has(identity)) return "agility";
    return "strength";
  }

  function equippedCount() {
    return Object.values(state.equipment).filter((entry) => entry?.item).length;
  }

  function classPayload() {
    return bootstrap?.classes?.find((row) => row.key === state.className) || bootstrap?.classes?.[0];
  }

  function specPayload() {
    const currentClass = classPayload();
    return currentClass?.specs?.find((row) => row.key === state.specName) || currentClass?.specs?.[0];
  }

  function syncSelectors() {
    els.class_select.innerHTML = (bootstrap?.classes || []).map((row) => `<option value="${escapeHtml(row.key)}">${escapeHtml(row.name)}</option>`).join("");
    if (!bootstrap?.classes?.some((row) => row.key === state.className)) state.className = bootstrap?.classes?.[0]?.key || "Warrior";
    els.class_select.value = state.className;
    const currentClass = classPayload();
    els.spec_select.innerHTML = (currentClass?.specs || []).map((row) => `<option value="${escapeHtml(row.key)}">${escapeHtml(row.name)}</option>`).join("");
    if (!currentClass?.specs?.some((row) => row.key === state.specName)) state.specName = currentClass?.specs?.[0]?.key || "";
    els.spec_select.value = state.specName;
  }

  function renderCatalogStatus() {
    const catalog = bootstrap?.catalog || {};
    els.catalog_status.classList.toggle("is-ready", Boolean(catalog.available));
    els.catalog_status.classList.toggle("is-error", !catalog.available);
    els.catalog_status.textContent = catalog.available
      ? `${catalog.season?.name || "当前赛季"} · ${catalog.game_build || "正式服"}`
      : "装备目录尚未同步";
  }

  function compactNames(rows) {
    const counts = new Map();
    (rows || []).forEach((row) => {
      const name = row?.item?.name;
      if (name) counts.set(name, (counts.get(name) || 0) + 1);
    });
    return [...counts].map(([name, count]) => `${name}${count > 1 ? `×${count}` : ""}`).join("、");
  }

  function enhancementSummary(entry) {
    if (!entry) return "";
    const parts = [];
    if (entry.embellishment?.item?.name) parts.push(`美化：${entry.embellishment.item.name}`);
    const gems = compactNames(entry.gems);
    if (gems) parts.push(`宝石：${gems}`);
    if (entry.enchant?.item?.name) parts.push(`附魔：${entry.enchant.item.name}`);
    return parts.join(" · ");
  }

  function renderSlots() {
    els.slot_list.innerHTML = (bootstrap?.slots || []).map((slot) => {
      const entry = state.equipment[slot.key];
      const item = entry?.item;
      const active = slot.key === state.selectedSlot;
      const enhancements = enhancementSummary(entry);
      return `<button type="button" class="gear-slot-row${active ? " is-active" : ""}${item ? "" : " is-empty"}" data-slot="${escapeHtml(slot.key)}" role="option" aria-selected="${active}">
        ${item ? iconMarkup(item, "gear-slot-icon") : '<span class="gear-slot-placeholder" aria-hidden="true">◇</span>'}
        <span class="gear-slot-copy"><span class="gear-slot-label">${escapeHtml(slot.label)}</span><span class="gear-slot-item">${escapeHtml(item?.name || "未选择")}</span>${enhancements ? `<small class="gear-slot-enhancements" title="${escapeHtml(enhancements)}">${escapeHtml(enhancements)}</small>` : ""}</span>
        <span class="gear-slot-level">${entry?.variant?.item_level || entry?.itemLevel || ""}</span>
      </button>`;
    }).join("");
    const count = equippedCount();
    els.equipped_count.textContent = `${count}/16`;
    els.mobile_count.textContent = `${count}/16`;
    const label = bootstrap?.slots?.find((slot) => slot.key === state.selectedSlot)?.label || state.selectedSlot;
    els.browser_title.textContent = `${label} · ${selectedEntry()?.item?.name || "未选择"}`;
  }

  function renderMode() {
    const enhancement = state.mode === "enhancement";
    els.mode_equipment.classList.toggle("is-active", !enhancement);
    els.mode_equipment.setAttribute("aria-selected", String(!enhancement));
    els.mode_enhancement.classList.toggle("is-active", enhancement);
    els.mode_enhancement.setAttribute("aria-selected", String(enhancement));
    els.equipment_browser.hidden = enhancement;
    els.enhancement_browser.hidden = !enhancement;
  }

  function renderCandidates() {
    if (candidateLoading && !candidates.length) {
      els.candidate_list.innerHTML = '<div class="gear-loading-state">正在读取当前槽位的装备…</div>';
      return;
    }
    if (!bootstrap?.catalog?.available) {
      els.candidate_list.innerHTML = '<div class="gear-empty-state"><div><strong>装备目录尚未就绪</strong>请先运行装备目录同步命令并激活通过审计的批次。</div></div>';
      els.load_more.hidden = true;
      return;
    }
    if (!candidates.length) {
      els.candidate_list.innerHTML = '<div class="gear-empty-state"><div><strong>没有匹配装备</strong>尝试清空搜索词或切换来源。</div></div>';
      els.load_more.hidden = true;
      return;
    }
    const current = selectedEntry();
    els.candidate_list.innerHTML = candidates.map((item) => {
      const variant = current?.item?.item_id === item.item_id
        ? item.variants.find((row) => row.id === current.variant?.id) || item.variants[0]
        : item.variants[0];
      const equipped = current?.item?.item_id === item.item_id && current?.variant?.id === variant?.id;
      return `<div class="gear-candidate-row${equipped ? " is-equipped" : ""}" role="button"${equipped ? ' aria-current="true"' : ""} data-select-item="${item.item_id}" data-variant-id="${variant.id}"${tooltipAttrs(item, variant)}>
        <div class="gear-candidate-name">${iconMarkup(item)}<span class="gear-candidate-copy"><strong class="gear-candidate-title">${escapeHtml(item.name)}</strong><small class="gear-candidate-subtitle">${escapeHtml(equipped ? "当前装备" : item.armor_type || item.weapon_type || (variant.type === "crafted_equipment" ? "制造装备" : "装备"))}</small></span></div>
        <div class="gear-candidate-level">${variant.item_level || "-"}</div>
        <div class="gear-track gear-track--${escapeHtml(variant.track || "crafted")}">${escapeHtml(variant.type === "crafted_equipment" ? `制造 ${variant.crafting_quality || ""}星` : `${variant.track_label || variant.track || "-"} ${variant.track_rank ? `${variant.track_rank}/${variant.track_max_rank}` : ""}`)}</div>
        <div class="gear-source-copy">${sourceMarkup(variant)}</div>
        <div class="gear-stat-copy">${statMarkup(variant.stats)}</div>
      </div>`;
    }).join("");
    els.load_more.hidden = candidates.length >= candidateTotal;
    els.load_more.textContent = candidateLoading ? "加载中…" : `加载更多（${candidates.length}/${candidateTotal}）`;
  }

  async function loadCandidates(reset = true) {
    if (candidateLoading) return;
    if (reset) { candidatePage = 1; candidates = []; candidateTotal = 0; }
    candidateLoading = true;
    renderCandidates();
    const params = new URLSearchParams({
      class: state.className,
      spec: state.specName,
      slot: state.selectedSlot,
      source: els.source_filter.value,
      q: els.search_input.value.trim(),
      page: String(candidatePage),
      page_size: "60",
    });
    try {
      const payload = await requestJson(`${endpoints.catalog}?${params}`);
      candidates = reset ? payload.items : candidates.concat(payload.items || []);
      candidateTotal = payload.total || 0;
    } catch (error) {
      if (reset) candidates = [];
      toast(error.message, true);
    } finally {
      candidateLoading = false;
      renderCandidates();
    }
  }

  function optionRow(item, kind) {
    const variant = item.variants?.[0];
    const entry = selectedEntry();
    const selectedCount = kind === "gem"
      ? (entry?.gems || []).filter((row) => Number(row.variant?.id) === Number(variant?.id)).length
      : Number(entry?.[kind]?.variant?.id) === Number(variant?.id) ? 1 : 0;
    const description = [...new Set([item.description, statMarkupText(variant?.stats), ...(variant?.effects || []).map(effectText)].filter(Boolean))]
      .join(" · ") || "无常驻属性说明";
    return `<label class="gear-option-row"${tooltipAttrs(item, variant)}>
      <input class="gear-option-check" type="checkbox" data-add-enhancement="${kind}" data-item-id="${item.item_id}" data-variant-id="${variant?.id || ""}"${selectedCount ? " checked" : ""}>
      <span class="gear-option-copy"><strong class="gear-option-name">${escapeHtml(item.name)}${selectedCount > 1 ? ` ×${selectedCount}` : ""}</strong><small class="gear-option-stat" title="${escapeHtml(description)}">${escapeHtml(description)}</small></span>
    </label>`;
  }

  function statMarkupText(stats) {
    return Object.entries(stats || {}).filter(([, value]) => number(value)).slice(0, 2)
      .map(([key, value]) => `${STAT_LABELS[key] || key} ${formatNumber(value)}`).join(" · ");
  }

  function renderOptionGroup(element, rows, kind, emptyText) {
    element.innerHTML = rows?.length
      ? rows.map((item) => optionRow(item, kind)).join("")
      : `<div class="gear-option-empty">${escapeHtml(emptyText)}</div>`;
  }

  async function loadEnhancements() {
    const entry = selectedEntry();
    const variantId = entry?.variant?.id || "";
    els.embellishment_list.innerHTML = els.gem_list.innerHTML = els.enchant_list.innerHTML = '<div class="gear-option-empty">正在读取兼容选项…</div>';
    const params = new URLSearchParams({class: state.className, spec: state.specName, slot: state.selectedSlot});
    if (variantId) params.set("variant_id", String(variantId));
    try {
      const payload = await requestJson(`${endpoints.enhancements}?${params}`);
      enhancementGroups = payload.groups || {embellishments: [], gems: [], enchants: []};
      renderOptionGroup(els.embellishment_list, enhancementGroups.embellishments, "embellishment", entry?.variant?.type === "crafted_equipment" ? "当前制造装备没有兼容美化。" : "美化只能应用到制造装备。" );
      renderOptionGroup(els.gem_list, enhancementGroups.gems, "gem", entry ? "当前装备没有可用插槽或宝石。" : "请先为该槽位选择装备。" );
      renderOptionGroup(els.enchant_list, enhancementGroups.enchants, "enchant", entry ? "当前槽位没有永久附魔。" : "请先为该槽位选择装备。" );
      const rule = entry ? socketRule() : null;
      els.add_socket_option.hidden = !entry || !rule;
      els.add_socket.checked = Boolean(entry?.addedSocket && rule);
      if (rule) {
        const itemName = bootstrap?.rules?.add_socket_item?.name || bootstrap?.rules?.add_socket_item?.itemName || "当前赛季插槽物品";
        els.add_socket_copy.textContent = `${itemName} · 最多增加 ${Number(rule.max_additional || 1)} 个`;
      }
      const socketCount = socketCapacity(entry);
      els.socket_summary.textContent = socketCount ? `${(entry.gems || []).length}/${socketCount} 个插槽` : "当前装备无插槽";
    } catch (error) {
      renderOptionGroup(els.embellishment_list, [], "embellishment", error.message);
      renderOptionGroup(els.gem_list, [], "gem", error.message);
      renderOptionGroup(els.enchant_list, [], "enchant", error.message);
      els.add_socket_option.hidden = true;
    }
  }

  function findCandidate(itemId) {
    return candidates.find((item) => Number(item.item_id) === Number(itemId));
  }

  function validateEquipment(variant, targetSlot) {
    const furyTitanGrip = state.className === "Warrior" && state.specName === "Fury";
    if (targetSlot === "off_hand" && state.equipment.main_hand?.variant?.metadata?.two_handed && !furyTitanGrip) {
      return "主手已装备双手武器，不能同时装备副手。";
    }
    const group = variant.unique_group;
    const limit = Number(variant.max_equipped || 0);
    if (group && limit) {
      const conflicts = [];
      Object.entries(state.equipment).forEach(([slot, entry]) => {
        if (slot === targetSlot || !entry) return;
        if (entry.variant?.unique_group === group) conflicts.push(entry.item?.name || slotLabel(slot));
        if (entry.embellishment?.variant?.unique_group === group) conflicts.push(entry.embellishment.item?.name || slotLabel(slot));
      });
      if (conflicts.length >= limit) return `“${group}”最多只能装备 ${limit} 件；与 ${conflicts.join("、")} 冲突。`;
    }
    return "";
  }

  async function addItem(item, variant) {
    const error = validateEquipment(variant, state.selectedSlot);
    if (error) { toast(error, true); return; }
    const current = selectedEntry();
    if (Number(current?.item?.item_id) === Number(item.item_id) && Number(current?.variant?.id) === Number(variant.id)) {
      openDetail();
      return;
    }
    const replacing = Boolean(current);
    const furyTitanGrip = state.className === "Warrior" && state.specName === "Fury";
    if (state.selectedSlot === "main_hand" && variant.metadata?.two_handed && state.equipment.off_hand && !furyTitanGrip) {
      delete state.equipment.off_hand;
      toast("已装备双手武器，副手装备已移除。")
    }
    const entry = {
      item: compactItem(item),
      variant: structuredClone(variant),
      selectedStats: [],
      resolvedStats: null,
      resolvedEffects: null,
      embellishment: null,
      gems: [],
      enchant: null,
      addedSocket: false,
      external: false,
    };
    if (variant.type === "crafted_equipment") {
      const pool = variant.crafting_options?.stat_pool || ["crit", "haste", "mastery", "versatility"];
      const count = Number(variant.crafting_options?.stat_count || 2);
      entry.selectedStats = pool.slice(0, count);
    }
    state.equipment[state.selectedSlot] = entry;
    if (entry.selectedStats.length) await resolveCraftedEntry(entry);
    persist();
    renderAll();
    openDetail();
    toast(`${item.name} 已${replacing ? "替换" : "装备"}到${slotLabel(state.selectedSlot)}`);
  }

  function slotLabel(slot) {
    return bootstrap?.slots?.find((row) => row.key === slot)?.label || slot;
  }

  function compactEnhancement(item, variant) {
    return {item: compactItem(item), variant: structuredClone(variant)};
  }

  function validateEnhancement(kind, variant) {
    const entry = selectedEntry();
    if (!entry) return "请先选择装备。";
    if (kind === "embellishment" && entry.variant?.type !== "crafted_equipment") return "美化只能应用到制造装备。";
    if (kind === "gem" && !socketCapacity(entry)) return "请先为当前装备增加宝石插槽。";
    if (kind === "embellishment" && variant.unique_group && variant.max_equipped) {
      const replacingSame = entry.embellishment?.variant?.unique_group === variant.unique_group;
      const conflicts = [];
      Object.values(state.equipment).forEach((row) => {
        if (!row) return;
        if (row.variant?.is_intrinsic_embellishment && row.variant?.unique_group === variant.unique_group) conflicts.push(row.item?.name || "自带美化装备");
        if ((!replacingSame || row !== entry) && row.embellishment?.variant?.unique_group === variant.unique_group) conflicts.push(row.embellishment.item?.name || "已应用美化");
      });
      if (conflicts.length >= Number(variant.max_equipped)) return `该美化分组最多可使用 ${variant.max_equipped} 件；与 ${conflicts.join("、")} 冲突。`;
    }
    return "";
  }

  async function applyEnhancement(kind, item, variant) {
    const error = validateEnhancement(kind, variant);
    if (error) { toast(error, true); return; }
    const entry = selectedEntry();
    const value = compactEnhancement(item, variant);
    if (kind === "embellishment") {
      entry.embellishment = value;
      if (entry.selectedStats?.length) await resolveCraftedEntry(entry);
    } else if (kind === "gem") {
      const capacity = socketCapacity(entry);
      entry.gems = Array.isArray(entry.gems) ? entry.gems : [];
      if (entry.gems.length < capacity) entry.gems.push(value);
      else entry.gems[capacity - 1] = value;
    } else if (kind === "enchant") {
      entry.enchant = value;
    }
    persist();
    renderAll();
    if (state.mode === "enhancement") loadEnhancements();
    toast(`${item.name} 已应用到${slotLabel(state.selectedSlot)}`);
  }

  async function resolveCraftedEntry(entry) {
    if (!entry?.variant?.id || entry.variant.type !== "crafted_equipment") return;
    try {
      const payload = await requestJson(endpoints.crafted, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          variant_id: entry.variant.id,
          selected_stats: entry.selectedStats || [],
          embellishment_variant_id: entry.embellishment?.variant?.id || null,
          class_name: state.className,
          spec_name: state.specName,
        }),
      });
      entry.resolvedStats = payload.resolved_stats || null;
      entry.resolvedEffects = payload.effects || null;
    } catch (error) {
      toast(error.message, true);
    }
  }

  function renderDetail() {
    const entry = selectedEntry();
    if (!entry) {
      els.detail_content.innerHTML = '<div class="gear-detail-empty">从候选列表选择装备后，可在这里切换品级、制造绿字、查看属性与强化。</div>';
      return;
    }
    const item = entry.item;
    const variant = entry.variant;
    const candidate = findCandidate(item.item_id);
    const availableVariants = candidate?.variants || [variant];
    const stats = entry.resolvedStats || variant.stats || {};
    const source = sourceText(variant).replaceAll("\n", " · ");
    const crafting = variant.type === "crafted_equipment" ? craftingFields(entry) : "";
    const applied = appliedMarkup(entry);
    const effects = entry.resolvedEffects || variant.effects || [];
    els.detail_content.innerHTML = `
      <div class="gear-detail-identity"${tooltipAttrs(item, variant)}>${iconMarkup(item)}<div><h3 class="gear-detail-name">${escapeHtml(item.name)}</h3><div class="gear-detail-meta">装等 ${variant.item_level || entry.itemLevel || "-"}<br>${escapeHtml(source)}</div></div></div>
      <div class="gear-detail-fields">
        <label class="gear-detail-field"><span>${variant.type === "crafted_equipment" ? "品质/装等" : "品级/等级"}</span><select id="gear-detail-variant">${availableVariants.map((row) => `<option value="${row.id}"${row.id === variant.id ? " selected" : ""}>${escapeHtml(variantLabel(row))}</option>`).join("")}</select></label>
        ${crafting}
      </div>
      <div class="gear-detail-stats">${Object.entries(stats).filter(([, value]) => number(value)).map(([key, value]) => `<div class="gear-detail-stat"><span>${escapeHtml(STAT_LABELS[key] || key)}</span><span>${formatNumber(value)}</span></div>`).join("") || '<span class="gear-no-effects">该变体没有可直接累加的静态属性。</span>'}</div>
      ${applied}
      <div class="gear-detail-effects"><h3>装备特效</h3>${effects.length ? effects.map((effect) => `<div class="gear-effect-line">${escapeHtml(effectText(effect))}</div>`).join("") : '<span class="gear-no-effects">无触发型特效</span>'}</div>
      <div class="gear-detail-actions"><button type="button" class="gear-btn" data-open-enhancements>配置强化</button><button type="button" class="gear-btn gear-btn--danger-quiet" data-remove-item>移除装备</button></div>`;
  }

  function craftingFields(entry) {
    const pool = entry.variant.crafting_options?.stat_pool || ["crit", "haste", "mastery", "versatility"];
    const count = Number(entry.variant.crafting_options?.stat_count || 2);
    return Array.from({length: count}, (_unused, index) => `<label class="gear-detail-field"><span>制造绿字 ${index + 1}</span><select data-crafted-stat="${index}">${pool.map((key) => `<option value="${escapeHtml(key)}"${entry.selectedStats?.[index] === key ? " selected" : ""}>${escapeHtml(STAT_LABELS[key] || key)}</option>`).join("")}</select></label>`).join("");
  }

  function appliedMarkup(entry) {
    const rows = [];
    if (entry.embellishment) rows.push(["embellishment", `美化：${entry.embellishment.item.name}`]);
    (entry.gems || []).forEach((gem, index) => rows.push([`gem:${index}`, `宝石 ${index + 1}：${gem.item.name}`]));
    if (entry.enchant) rows.push(["enchant", `附魔：${entry.enchant.item.name}`]);
    if (!rows.length) return '<div class="gear-applied"><h3>已应用强化</h3><span class="gear-no-effects">尚未选择美化、宝石或附魔</span></div>';
    return `<div class="gear-applied"><h3>已应用强化</h3>${rows.map(([key, label]) => `<div class="gear-applied-chip"><span>${escapeHtml(label)}</span><button type="button" data-remove-enhancement="${escapeHtml(key)}">移除</button></div>`).join("")}</div>`;
  }

  function addStats(target, stats) {
    Object.entries(stats || {}).forEach(([key, value]) => {
      if (number(value)) target[key] = number(target[key]) + number(value);
    });
  }

  function totalsAndEffects() {
    const totals = {};
    const effects = [];
    let equipped = 0;
    let missingStats = 0;
    Object.values(state.equipment).forEach((entry) => {
      if (!entry) return;
      equipped += 1;
      const equipmentStats = entry.resolvedStats || entry.variant?.stats || {};
      if (!Object.values(equipmentStats).some((value) => number(value))) missingStats += 1;
      addStats(totals, equipmentStats);
      (entry.gems || []).forEach((gem) => addStats(totals, gem.variant?.stats));
      addStats(totals, entry.enchant?.variant?.stats);
      addStats(totals, entry.embellishment?.variant?.stats);
      const entryEffects = entry.resolvedEffects || entry.variant?.effects || [];
      entryEffects.forEach((effect) => effects.push({slot: entry.item?.name || "装备", text: effectText(effect)}));
      if (!entry.resolvedEffects) (entry.embellishment?.variant?.effects || []).forEach((effect) => effects.push({slot: entry.embellishment.item.name, text: effectText(effect)}));
      (entry.enchant?.variant?.effects || []).forEach((effect) => effects.push({slot: entry.enchant.item.name, text: effectText(effect)}));
    });
    return {totals, effects: effects.filter((row) => row.text), equipped, missingStats};
  }

  function secondaryPercentage(key, value) {
    if (!SECONDARY_STATS.has(key)) return null;
    const conversion = bootstrap?.rules?.secondary_stat_conversion?.[`${state.className}:${state.specName}`] || {};
    const perPercent = number(conversion[`${key}_per_percent`]);
    if (!perPercent) return null;
    const coefficient = key === "mastery" ? number(conversion.mastery_coefficient) || 1 : 1;
    const basePercent = number(BASE_SECONDARY_PERCENTAGES[key]);
    const ratingPercent = number(value) / perPercent;
    return (basePercent + ratingPercent) * coefficient;
  }

  function secondaryPercentageTitle(key) {
    if (key === "crit") return "含 5% 基础暴击";
    if (key === "mastery") return "含 8% 基础精通，并应用当前专精精通系数";
    return "无增益状态下按固定比例换算";
  }

  function renderStats() {
    const {totals, effects, equipped, missingStats} = totalsAndEffects();
    const keys = [primaryStatKey(), ...SUMMARY_STATS];
    const max = Math.max(1, ...keys.map((key) => number(totals[key])));
    els.stat_grid.innerHTML = keys.map((key) => {
      const value = number(totals[key]);
      const percent = secondaryPercentage(key, value);
      const percentageMarkup = percent === null
        ? ""
        : `<small class="gear-stat-percent" title="${escapeHtml(secondaryPercentageTitle(key))}">/ ${formatNumber(percent)}%</small>`;
      return `<div class="gear-stat-card"><span class="gear-stat-label">${escapeHtml(STAT_LABELS[key] || "属性")}</span><strong class="gear-stat-value">${formatNumber(value)}${percentageMarkup}</strong><span class="gear-stat-bar" style="--stat-progress:${Math.max(value ? 8 : 0, value / max * 100)}%;--stat-color:${STAT_COLORS[key] || "#64748b"}"></span></div>`;
    }).join("");
    els.effect_list.innerHTML = effects.length
      ? effects.map((row) => `<div class="gear-effect-line"><strong>${escapeHtml(row.slot)}：</strong>${escapeHtml(row.text)}</div>`).join("")
      : '<span class="gear-no-effects">当前配装没有触发型特效。</span>';
    els.stats_context.textContent = missingStats
      ? `已装备 ${equipped}/16 · ${missingStats} 件缺少静态属性数据；百分比仍包含 5% 基础暴击与 8% 基础精通`
      : `已装备 ${equipped}/16 · 含 5% 基础暴击；8% 基础精通与装备精通一并乘以当前专精系数`;
  }

  function previewSlotMarkup(slotKey) {
    const slot = bootstrap?.slots?.find((row) => row.key === slotKey) || {key: slotKey, label: slotKey};
    const entry = state.equipment[slotKey];
    const item = entry?.item;
    const variant = entry?.variant;
    const enhancements = enhancementSummary(entry);
    const tooltip = item ? tooltipAttrs(item, variant) : "";
    return `<button type="button" class="gear-preview-slot${item ? "" : " is-empty"}" data-preview-slot="${escapeHtml(slotKey)}"${tooltip}>
      ${item ? iconMarkup(item, "gear-preview-slot-icon") : '<span class="gear-preview-slot-icon gear-preview-slot-placeholder" aria-hidden="true">◇</span>'}
      <span class="gear-preview-slot-copy">
        <span class="gear-preview-slot-label">${escapeHtml(slot.label)}</span>
        <strong>${escapeHtml(item?.name || "未装备")}</strong>
        ${enhancements ? `<small title="${escapeHtml(enhancements)}">${escapeHtml(enhancements)}</small>` : ""}
      </span>
      <span class="gear-preview-slot-level">${variant?.item_level || entry?.itemLevel || "—"}</span>
    </button>`;
  }

  function renderPreview() {
    if (!els.preview) return;
    const currentClass = classPayload() || {};
    const currentSpec = specPayload() || {};
    const {totals, effects, equipped} = totalsAndEffects();
    const levels = Object.values(state.equipment)
      .map((entry) => number(entry?.variant?.item_level || entry?.itemLevel))
      .filter((value) => value > 0);
    const averageLevel = levels.length ? Math.round(levels.reduce((sum, value) => sum + value, 0) / levels.length) : 0;
    const completion = Math.min(100, equipped / 16 * 100);
    const catalog = bootstrap?.catalog || {};
    els.preview_left.innerHTML = PREVIEW_LEFT_SLOTS.map(previewSlotMarkup).join("");
    els.preview_right.innerHTML = PREVIEW_RIGHT_SLOTS.map(previewSlotMarkup).join("");
    els.preview_class.textContent = currentClass.name || state.className;
    els.preview_spec.textContent = currentSpec.name || state.specName;
    els.preview_spec_fallback.textContent = String(currentSpec.name || state.specName || "专").slice(0, 1);
    const specIconUrl = currentSpec.icon || "";
    if (!specIconUrl) els.preview_spec_icon.removeAttribute("src");
    else if (els.preview_spec_icon.getAttribute("src") !== specIconUrl) els.preview_spec_icon.src = specIconUrl;
    const specIconLoaded = Boolean(specIconUrl && els.preview_spec_icon.complete && els.preview_spec_icon.naturalWidth);
    els.preview_spec_icon.alt = specIconUrl ? `${currentSpec.name || state.specName}专精图标` : "";
    els.preview_spec_icon.hidden = !specIconLoaded;
    els.preview_spec_fallback.hidden = specIconLoaded;
    els.preview_item_level.textContent = String(averageLevel || 0);
    els.preview_count.textContent = `${equipped} / 16`;
    els.preview_season.textContent = `${catalog.season?.name || "当前赛季"} · ${catalog.game_build || "正式服"}`;
    els.preview_progress_bar.style.width = `${completion}%`;
    els.preview_progress_copy.textContent = equipped ? `已完成 ${equipped} 个装备槽位` : "尚未选择装备";
    const statKeys = [primaryStatKey(), ...SUMMARY_STATS];
    els.preview_stats.innerHTML = statKeys.map((key) => {
      const value = number(totals[key]);
      const percent = secondaryPercentage(key, value);
      return `<div class="gear-preview-stat"><span>${escapeHtml(STAT_LABELS[key] || key)}</span><strong>${formatNumber(value)}</strong>${percent === null ? "" : `<small title="${escapeHtml(secondaryPercentageTitle(key))}">${formatNumber(percent)}%</small>`}</div>`;
    }).join("");
    els.preview_effects.innerHTML = effects.length
      ? effects.map((row) => `<div><strong>${escapeHtml(row.slot)}</strong><span>${escapeHtml(row.text)}</span></div>`).join("")
      : '<span class="gear-preview-empty-effects">当前配装没有触发型特效。</span>';
  }

  function renderView() {
    const preview = state.viewMode === "preview";
    document.querySelectorAll("[data-gear-editor]").forEach((node) => { node.hidden = preview; });
    els.preview.hidden = !preview;
    els.view_editor.classList.toggle("is-active", !preview);
    els.view_editor.setAttribute("aria-selected", String(!preview));
    els.view_preview.classList.toggle("is-active", preview);
    els.view_preview.setAttribute("aria-selected", String(preview));
  }

  function renderMobileView() {
    page.dataset.mobileView = state.mobileView;
    document.querySelectorAll("[data-mobile-view]").forEach((button) => button.classList.toggle("is-active", button.dataset.mobileView === state.mobileView));
  }

  function renderAll() {
    renderSlots();
    renderMode();
    renderDetail();
    renderStats();
    renderPreview();
    renderView();
    renderMobileView();
    renderCandidates();
  }

  function openDetail() {
    els.detail_panel.classList.add("is-open");
  }

  function closeDetail() {
    els.detail_panel.classList.remove("is-open");
  }

  async function switchVariant(variantId) {
    const entry = selectedEntry();
    const item = findCandidate(entry?.item?.item_id);
    const variant = item?.variants?.find((row) => Number(row.id) === Number(variantId));
    if (!entry || !variant) return;
    const error = validateEquipment(variant, state.selectedSlot);
    if (error) { toast(error, true); renderDetail(); return; }
    entry.variant = structuredClone(variant);
    entry.resolvedStats = null;
    entry.resolvedEffects = null;
    entry.gems = (entry.gems || []).slice(0, socketCapacity(entry));
    if (variant.type === "crafted_equipment") {
      const pool = variant.crafting_options?.stat_pool || ["crit", "haste", "mastery", "versatility"];
      entry.selectedStats = (entry.selectedStats || []).filter((key) => pool.includes(key)).slice(0, Number(variant.crafting_options?.stat_count || 2));
      while (entry.selectedStats.length < Number(variant.crafting_options?.stat_count || 2)) {
        entry.selectedStats.push(pool.find((key) => !entry.selectedStats.includes(key)) || pool[0]);
      }
      await resolveCraftedEntry(entry);
    } else {
      entry.selectedStats = [];
      entry.embellishment = null;
    }
    persist();
    renderAll();
  }

  async function changeCraftedStat(index, value) {
    const entry = selectedEntry();
    if (!entry) return;
    const selected = [...(entry.selectedStats || [])];
    selected[index] = value;
    if (new Set(selected).size !== selected.length) {
      toast("制造装备的两项绿字不能相同。", true);
      renderDetail();
      return;
    }
    entry.selectedStats = selected;
    await resolveCraftedEntry(entry);
    persist();
    renderAll();
  }

  function removeEnhancement(key) {
    const entry = selectedEntry();
    if (!entry) return;
    if (key === "embellishment") entry.embellishment = null;
    else if (key === "enchant") entry.enchant = null;
    else if (key.startsWith("gem:")) entry.gems.splice(Number(key.split(":")[1]), 1);
    if (entry.variant?.type === "crafted_equipment" && entry.selectedStats?.length) resolveCraftedEntry(entry).then(() => { persist(); renderAll(); });
    else { persist(); renderAll(); }
  }

  function bytesToBase64Url(bytes) {
    let binary = "";
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
  }

  function base64UrlToBytes(value) {
    const normalized = value.replaceAll("-", "+").replaceAll("_", "/");
    const binary = atob(normalized + "=".repeat((4 - normalized.length % 4) % 4));
    return Uint8Array.from(binary, (character) => character.charCodeAt(0));
  }

  async function encodeShare(payload) {
    const bytes = new TextEncoder().encode(JSON.stringify(payload));
    if ("CompressionStream" in window) {
      const stream = new Blob([bytes]).stream().pipeThrough(new CompressionStream("gzip"));
      return `z${bytesToBase64Url(new Uint8Array(await new Response(stream).arrayBuffer()))}`;
    }
    return `j${bytesToBase64Url(bytes)}`;
  }

  async function decodeShare(code) {
    const prefix = code[0];
    const bytes = base64UrlToBytes(code.slice(1));
    if (prefix === "z") {
      if (!("DecompressionStream" in window)) throw new Error("当前浏览器不支持解压该分享链接。")
      const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
      return JSON.parse(new TextDecoder().decode(await new Response(stream).arrayBuffer()));
    }
    if (prefix !== "j") throw new Error("分享链接版本无法识别。")
    return JSON.parse(new TextDecoder().decode(bytes));
  }

  async function copyShare() {
    const code = await encodeShare(state);
    const url = new URL(location.href);
    url.search = "";
    url.searchParams.set("code", code);
    if (url.toString().length > Number(bootstrap?.rules?.max_share_length || 8000)) {
      toast("分享链接过长，请先移除部分目录外装备。", true);
      return;
    }
    await navigator.clipboard.writeText(url.toString());
    toast("自包含分享链接已复制。")
  }

  async function restoreShareIfPresent() {
    const code = new URLSearchParams(location.search).get("code");
    if (!code) return false;
    try {
      state = normalizeState(await decodeShare(code));
      toast("已从分享链接恢复配装。")
      return true;
    } catch (error) {
      toast(error.message || "分享链接解析失败。", true);
      return false;
    }
  }

  async function importSimc() {
    const profile = els.simc_input.value.trim();
    if (!profile) { els.simc_message.textContent = "请先粘贴 SimC Profile。"; return; }
    els.simc_submit.disabled = true;
    els.simc_message.textContent = "正在解析…";
    try {
      const payload = await requestJson(endpoints.simc, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({profile}),
      });
      if (payload.identity?.class_name && payload.identity?.spec_name) {
        const viewMode = state.viewMode;
        state = loadStored(payload.identity.class_name, payload.identity.spec_name);
        state.className = payload.identity.class_name;
        state.specName = payload.identity.spec_name;
        state.viewMode = viewMode;
        syncSelectors();
      }
      (payload.equipment || []).forEach((row) => {
        if (!row.slot) return;
        const item = row.item || {item_id: row.item_id, name: row.name || `物品 #${row.item_id}`};
        state.equipment[row.slot] = {
          item: compactItem(item),
          variant: row.variant || {id: null, item_level: row.item_level, stats: {}, effects: [], type: "external", bonus_ids: row.bonus_ids || []},
          itemLevel: row.item_level || 0,
          selectedStats: row.crafted_stats || [],
          resolvedStats: null,
          resolvedEffects: null,
          embellishment: null,
          gems: (row.gems || []).map((gem) => gem.item && gem.variant ? compactEnhancement(gem.item, gem.variant) : {item: {item_id: gem.item_id, name: gem.name}, variant: gem.variant || {stats: {}, effects: []}, external: true}),
          enchant: row.enchant ? (row.enchant.item && row.enchant.variant ? compactEnhancement(row.enchant.item, row.enchant.variant) : {item: {item_id: row.enchant.item_id, name: row.enchant.name}, variant: row.enchant.variant || {stats: {}, effects: []}, external: true}) : null,
          addedSocket: Boolean((row.gems || []).length > Number(row.variant?.socket_count || 0) && socketRule(row.slot)),
          external: Boolean(row.external),
          rawValue: row.raw_value || "",
        };
      });
      persist();
      renderAll();
      await loadCandidates(true);
      els.simc_dialog.close();
      els.simc_message.textContent = "";
      toast(`已导入 ${payload.equipment?.length || 0} 个装备槽位。`);
      (payload.warnings || []).slice(0, 3).forEach((warning) => toast(warning, true));
    } catch (error) {
      els.simc_message.textContent = error.message;
    } finally {
      els.simc_submit.disabled = false;
    }
  }

  function bindEvents() {
    els.preview_spec_icon.addEventListener("load", () => {
      els.preview_spec_icon.hidden = false;
      els.preview_spec_fallback.hidden = true;
    });
    els.preview_spec_icon.addEventListener("error", () => {
      els.preview_spec_icon.hidden = true;
      els.preview_spec_fallback.hidden = false;
    });
    els.view_editor.addEventListener("click", () => {
      state.viewMode = "editor";
      persist();
      renderView();
    });
    els.view_preview.addEventListener("click", () => {
      state.viewMode = "preview";
      persist();
      renderPreview();
      renderView();
    });
    els.preview.addEventListener("click", async (event) => {
      const slot = event.target.closest("[data-preview-slot]")?.dataset.previewSlot;
      if (!slot) return;
      state.selectedSlot = slot;
      state.viewMode = "editor";
      state.mobileView = "browser";
      persist();
      renderAll();
      if (state.mode === "enhancement") await loadEnhancements(); else await loadCandidates(true);
    });
    els.class_select.addEventListener("change", async () => {
      const className = els.class_select.value;
      const selectedClass = bootstrap.classes.find((row) => row.key === className);
      const specName = selectedClass?.specs?.[0]?.key || "";
      const viewMode = state.viewMode;
      state = loadStored(className, specName);
      state.className = className;
      state.specName = specName;
      state.viewMode = viewMode;
      syncSelectors(); persist(); renderAll(); await loadCandidates(true);
    });
    els.spec_select.addEventListener("change", async () => {
      const viewMode = state.viewMode;
      state = loadStored(state.className, els.spec_select.value);
      state.className = els.class_select.value;
      state.specName = els.spec_select.value;
      state.viewMode = viewMode;
      persist(); renderAll(); await loadCandidates(true);
    });
    els.slot_list.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-slot]");
      if (!button) return;
      state.selectedSlot = button.dataset.slot;
      state.mobileView = "browser";
      persist(); renderAll();
      if (state.mode === "equipment") await loadCandidates(true); else await loadEnhancements();
    });
    els.mode_equipment.addEventListener("click", async () => { state.mode = "equipment"; persist(); renderMode(); await loadCandidates(true); });
    els.mode_enhancement.addEventListener("click", async () => { state.mode = "enhancement"; persist(); renderMode(); await loadEnhancements(); });
    els.source_filter.addEventListener("change", () => loadCandidates(true));
    els.search_input.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = window.setTimeout(() => loadCandidates(true), 250); });
    els.load_more.addEventListener("click", () => { candidatePage += 1; loadCandidates(false); });
    els.candidate_list.addEventListener("click", (event) => {
      const row = event.target.closest(".gear-candidate-row[data-select-item]");
      if (!row) return;
      const item = findCandidate(row.dataset.selectItem);
      const variant = item?.variants?.find((candidate) => Number(candidate.id) === Number(row.dataset.variantId));
      if (item && variant) addItem(item, variant);
    });
    els.candidate_list.addEventListener("keydown", (event) => {
      if (!['Enter', ' '].includes(event.key)) return;
      const row = event.target.closest(".gear-candidate-row[data-select-item]");
      if (!row) return;
      event.preventDefault();
      row.click();
    });
    els.enhancement_browser.addEventListener("change", (event) => {
      const control = event.target.closest("[data-add-enhancement]");
      if (!control) return;
      const groupKey = control.dataset.addEnhancement;
      const root = control.closest(".gear-option-list");
      const sourceRows = root === els.embellishment_list ? enhancementGroups.embellishments : root === els.gem_list ? enhancementGroups.gems : enhancementGroups.enchants;
      const item = sourceRows?.find((row) => Number(row.item_id) === Number(control.dataset.itemId));
      const variant = item?.variants?.find((row) => Number(row.id) === Number(control.dataset.variantId));
      if (!control.checked) {
        if (groupKey === "gem") {
          const index = (selectedEntry()?.gems || []).findIndex((row) => Number(row.variant?.id) === Number(control.dataset.variantId));
          if (index >= 0) removeEnhancement(`gem:${index}`);
        } else {
          removeEnhancement(groupKey);
        }
        if (state.mode === "enhancement") loadEnhancements();
        return;
      }
      if (item && variant) applyEnhancement(groupKey, item, variant);
    });
    els.add_socket.addEventListener("change", () => {
      const entry = selectedEntry();
      if (!entry || !socketRule()) return;
      entry.addedSocket = els.add_socket.checked;
      entry.gems = (entry.gems || []).slice(0, socketCapacity(entry));
      persist();
      renderAll();
      loadEnhancements();
    });
    els.detail_content.addEventListener("change", (event) => {
      if (event.target.id === "gear-detail-variant") switchVariant(event.target.value);
      if (event.target.matches("[data-crafted-stat]")) changeCraftedStat(Number(event.target.dataset.craftedStat), event.target.value);
    });
    els.detail_content.addEventListener("click", (event) => {
      if (event.target.closest("[data-remove-item]")) { delete state.equipment[state.selectedSlot]; persist(); renderAll(); loadCandidates(true); }
      const remove = event.target.closest("[data-remove-enhancement]");
      if (remove) removeEnhancement(remove.dataset.removeEnhancement);
      if (event.target.closest("[data-open-enhancements]")) { state.mode = "enhancement"; persist(); renderMode(); loadEnhancements(); closeDetail(); }
    });
    document.querySelectorAll("[data-mobile-view]").forEach((button) => button.addEventListener("click", () => { state.mobileView = button.dataset.mobileView; persist(); renderMobileView(); }));
    els.detail_close.addEventListener("click", closeDetail);
    els.import_simc.addEventListener("click", () => els.simc_dialog.showModal());
    els.simc_submit.addEventListener("click", importSimc);
    els.copy_share.addEventListener("click", () => copyShare().catch((error) => toast(error.message, true)));
    els.clear.addEventListener("click", () => {
      if (!window.confirm("清空当前职业专精的全部配装？")) return;
      state.equipment = {}; persist(); renderAll(); loadCandidates(true); toast("当前配装已清空。")
    });
  }

  async function initialize() {
    try {
      const payload = await requestJson(endpoints.bootstrap);
      bootstrap = payload;
      const restored = await restoreShareIfPresent();
      if (!restored) state = loadStored(state.className, state.specName);
      if (!state.batchKey) state.batchKey = bootstrap.catalog?.batch_key || "";
      syncSelectors();
      renderCatalogStatus();
      bindEvents();
      renderAll();
      await loadCandidates(true);
      if (state.mode === "enhancement") await loadEnhancements();
    } catch (error) {
      els.catalog_status.textContent = "配装器初始化失败";
      els.catalog_status.classList.add("is-error");
      els.candidate_list.innerHTML = `<div class="gear-empty-state"><div><strong>页面初始化失败</strong>${escapeHtml(error.message)}</div></div>`;
      toast(error.message, true);
    } finally {
      page.setAttribute("aria-busy", "false");
    }
  }

  initialize();
})();
