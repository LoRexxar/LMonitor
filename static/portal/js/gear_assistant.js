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
    generate: byId("assistant-generate"), rerun: byId("assistant-rerun"), editConfig: byId("assistant-edit-config"),
    latestJob: byId("assistant-latest-job"), viewJob: byId("assistant-view-job"),
    fixedCount: byId("assistant-fixed-count"), fixedSummary: byId("assistant-fixed-summary"), resultLockSummary: byId("assistant-result-lock-summary"),
    equipmentList: byId("assistant-equipment-list"), currentPane: byId("assistant-current-pane"), ownedPane: byId("assistant-owned-pane"),
    currentTabCount: byId("assistant-current-tab-count"), ownedTabCount: byId("assistant-owned-tab-count"),
    ownedClear: byId("assistant-clear-owned"), allowMythicLastTwo: byId("assistant-allow-mythic-last-two"),
    sourcePreference: byId("assistant-source-preference"),
    configView: byId("assistant-config-view"), resultsView: byId("assistant-results-view"),
    catalogStatus: byId("assistant-catalog-status"), results: byId("assistant-results"), explanation: byId("assistant-explanation"),
    importSimc: byId("assistant-import-simc"), simcDialog: byId("assistant-simc-dialog"), simcInput: byId("assistant-simc-input"),
    simcSubmit: byId("assistant-simc-submit"), simcMessage: byId("assistant-simc-message"), toastRoot: byId("assistant-toast-root"),
  };
  const STAT_LABELS = {crit: "暴击", haste: "急速", mastery: "精通", versatility: "全能"};
  const DRAFT_KEY = "wowdaily:gear-assistant:draft:v1";
  const JOB_KEY = `wowdaily:gear-assistant:job:v2:${page.dataset.jobOwner}`;
  const JOB_ID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;
  let builderBootstrap = null;
  let assistantData = null;
  let currentState = null;
  let libraryTab = "current";
  let lockedSlots = new Set();
  let workbenchMode = "config";
  let lastPlans = [];
  let resultContext = null;
  let ownedClearing = false;
  let ownedRevision = 0;
  let pendingJob = null;
  let latestJob = null;
  let polling = false;
  let initialized = false;
  let assistantGeneration = 0;
  let assistantReady = false;
  let hideResultTooltip = () => {};

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (character) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"})[character]);
  }
  function csrfHeaders() {
    const token = document.querySelector('input[name="csrfmiddlewaretoken"]')?.value || "";
    return token ? {"X-CSRFToken": token} : {};
  }
  async function requestJson(url, options = {}) {
    const response = await fetch(url, {...options, headers: {Accept: "application/json", ...(options.headers || {})}});
    let payload = {};
    try { payload = await response.json(); } catch (_error) { /* 统一错误 */ }
    if (!response.ok || payload.success === false) {
      throw Object.assign(new Error(payload.error || `请求失败（${response.status}）`), {status: response.status});
    }
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
  function currentEquipmentEntries() {
    return Object.entries(currentState?.equipment || {}).filter(([, value]) => value?.item);
  }
  function resetLockedSlots() {
    lockedSlots = new Set(currentEquipmentEntries().map(([slot]) => slot));
  }
  function lockedEquipmentPayload() {
    const ref = entry => entry ? {variant: {id: entry.variant?.id}} : undefined;
    return Object.fromEntries(currentEquipmentEntries().filter(([slot]) => lockedSlots.has(slot)).map(([slot, entry]) => [slot, {
      ...ref(entry), variant_id: entry.variant_id, selectedStats: entry.selectedStats || entry.selected_stats,
      gems: (entry.gems || []).map(ref).filter(Boolean), enchant: ref(entry.enchant),
      embellishment: ref(entry.embellishment), addedSocket: entry.addedSocket || entry.added_socket,
    }]));
  }
  function renderLibraryTabs() {
    document.querySelectorAll("[data-library-tab]").forEach((button) => {
      const active = button.dataset.libraryTab === libraryTab;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-selected", String(active));
    });
    els.currentPane.hidden = libraryTab !== "current";
    els.ownedPane.hidden = libraryTab !== "owned";
  }
  function equipmentIcon(entry) {
    return entry?.item?.icon_url
      ? `<img class="assistant-equipment-icon" src="${escapeHtml(entry.item.icon_url)}" alt="" loading="lazy">`
      : '<span class="assistant-equipment-icon assistant-equipment-icon--empty"></span>';
  }
  function renderEquipment() {
    const entries = currentState?.equipment || {};
    const slots = builderBootstrap?.slots || [];
    const equippedCount = currentEquipmentEntries().length;
    const fixedCount = [...lockedSlots].filter((slot) => entries[slot]?.item).length;
    els.currentTabCount.textContent = String(equippedCount);
    els.fixedCount.textContent = String(fixedCount);
    els.fixedSummary.textContent = `锁定 ${fixedCount} 件 · ${equippedCount - fixedCount} 件可替换`;
    els.resultLockSummary.textContent = `锁定 ${fixedCount} 件`;
    els.equipmentList.innerHTML = slots.map((slotRow) => {
      const slot = slotRow.key;
      const entry = entries[slot];
      const equipped = Boolean(entry?.item);
      const locked = equipped && lockedSlots.has(slot);
      const itemLevel = entry?.itemLevel || entry?.variant?.item_level || 0;
      return `<div class="assistant-equipment-row${equipped ? " is-equipped" : " is-empty"}${locked ? " is-locked" : " is-unlocked"}" data-equipment-slot="${escapeHtml(slot)}">
        ${equipmentIcon(entry)}
        <span class="assistant-equipment-copy"><small>${escapeHtml(slotRow.label)}</small><strong>${escapeHtml(entry?.item?.name || "空装备格")}</strong>${equipped ? `<em>${itemLevel ? `${itemLevel} 装等 · ` : ""}${locked ? "保持当前装备" : "允许模拟器替换"}</em>` : "<em>由模拟器选择装备</em>"}</span>
        ${equipped ? `<button type="button" class="assistant-slot-lock${locked ? " is-locked" : ""}" data-toggle-slot-lock="${escapeHtml(slot)}" aria-pressed="${String(locked)}" aria-label="${locked ? `取消锁定${escapeHtml(slotRow.label)}` : `重新锁定${escapeHtml(slotRow.label)}`}" title="${locked ? "取消锁定，允许模拟器替换" : "重新锁定，保留当前装备"}">${locked ? '<span aria-hidden="true">×</span><b>取消锁定</b>' : '<span aria-hidden="true">↺</span><b>重新锁定</b>'}</button>` : '<span class="assistant-slot-open">待补齐</span>'}
      </div>`;
    }).join("");
  }
  function markResultsStale() {
    if (workbenchMode !== "results" || !lastPlans.length) return;
    els.resultLockSummary.textContent = `${els.resultLockSummary.textContent} · 条件已修改`;
    els.resultLockSummary.classList.add("is-stale");
  }
  function setWorkbench(mode, {focus = false} = {}) {
    workbenchMode = mode === "results" ? "results" : "config";
    els.configView.hidden = workbenchMode !== "config";
    els.resultsView.hidden = workbenchMode !== "results";
    if (focus && window.matchMedia("(max-width: 760px)").matches) {
      document.querySelector(".assistant-workbench-panel")?.scrollIntoView({behavior: "smooth", block: "start"});
    }
  }
  function renderOwned() {
    const rows = assistantData?.owned_items || [];
    els.ownedTabCount.textContent = String(rows.length);
    els.ownedClear.disabled = ownedClearing || !rows.length;
    els.ownedClear.textContent = ownedClearing ? "清空中…" : "清空备选装备";
    if (!rows.length) {
      els.ownedList.innerHTML = '<div class="assistant-list-empty">还没有备选装备。可在职业配装器点击“加入备选”，或导入 SimC 背包。</div>';
      return;
    }
    els.ownedList.innerHTML = rows.map((row) => {
      const icon = row.item?.icon_url ? `<img class="assistant-owned-icon" src="${escapeHtml(row.item.icon_url)}" alt="" loading="lazy">` : '<span class="assistant-owned-icon"></span>';
      return `<div class="assistant-owned-row" data-owned-id="${row.id}">${icon}<span class="assistant-owned-copy"><strong>${escapeHtml(row.name)}</strong><small>${escapeHtml(row.slot_label)} · ${row.item_level || "未知装等"}${row.quantity > 1 ? ` · ×${row.quantity}` : ""}</small></span><button type="button" class="assistant-owned-delete" data-delete-owned="${row.id}" aria-label="移除备选装备">×</button></div>`;
    }).join("");
  }
  async function clearOwnedItems() {
    if (ownedClearing || !assistantData?.owned_items?.length) return;
    if (els.generate.disabled || els.simcSubmit.disabled) {
      toast("请等待当前模拟或导入完成后再清空。", true);
      return;
    }
    if (!window.confirm("确定清空当前账号的全部备选装备（所有槽位）？此操作不可撤销，不会清空当前配装、锁定选择或已保存方案。")) return;
    ownedClearing = true;
    ownedRevision += 1;
    const controls = [els.generate, els.rerun, els.importSimc, els.simcSubmit, els.classSelect, els.specSelect];
    const disabled = controls.map((control) => control.disabled);
    controls.forEach((control) => { control.disabled = true; });
    renderOwned();
    try {
      const payload = await requestJson(endpoints.owned, {
        method: "DELETE", headers: {"Content-Type": "application/json", ...csrfHeaders()},
        body: JSON.stringify({confirm_clear: true}),
      });
      ownedRevision += 1;
      assistantData.owned_items = [];
      markResultsStale();
      toast(`已清空 ${payload.deleted_count || 0} 条备选装备记录。`);
    } catch (error) {
      toast(error.message, true);
    } finally {
      ownedClearing = false;
      controls.forEach((control, index) => { control.disabled = disabled[index]; });
      renderOwned();
    }
  }
  function renderFlasks() {
    els.flask.innerHTML = (assistantData?.flasks || []).map((row) => `<option value="${escapeHtml(row.key)}">${escapeHtml(row.name)}</option>`).join("");
    els.flask.value = "auto";
  }
  async function loadAssistantData() {
    const generation = ++assistantGeneration;
    const origin = currentState;
    const revision = ownedRevision;
    const isCurrent = () => generation === assistantGeneration && currentState === origin && !polling;
    assistantReady = false;
    els.generate.disabled = els.rerun.disabled = true;
    renderLatestJob();
    try {
      const refreshed = await WowGearState.refresh(origin, requestJson);
      if (!isCurrent()) return;
      const payload = await requestJson(`${endpoints.bootstrap}?class=${encodeURIComponent(origin.className)}&spec=${encodeURIComponent(origin.specName)}`);
      if (!isCurrent()) return;
      currentState = refreshed;
      renderEquipment();
      if (ownedClearing || revision !== ownedRevision) payload.owned_items = assistantData?.owned_items || [];
      assistantData = payload;
      renderOwned();
      renderFlasks();
      els.catalogStatus.textContent = assistantData.catalog?.available ? `${assistantData.catalog.season_name || "当前赛季"} · 装备目录已就绪` : "装备目录尚未同步";
      assistantReady = true;
      if (initialized) els.generate.disabled = els.rerun.disabled = false;
      renderLatestJob();
    } catch (error) {
      if (!isCurrent()) return;
      if (!initialized) throw error;
      els.catalogStatus.textContent = "装备资料加载失败，请重新选择职业或专精。";
      toast(error.message, true);
    }
  }
  function targetPayload() {
    return Object.fromEntries(Object.keys(STAT_LABELS).map((key) => [key, Number(byId(`target-${key}`).value || 0)]));
  }
  function renderPlans(plans) {
    if (!plans?.length) return;
    hideResultTooltip();
    els.results.innerHTML = WowGearAssistantResults.render(plans, builderBootstrap?.slots || [], builderBootstrap || {});
  }
  function activeLatestJob() {
    return ["queued", "running"].includes(latestJob?.state);
  }
  function renderLatestJob() {
    const id = latestJob?.id;
    els.latestJob.hidden = !id;
    els.viewJob.textContent = id || "";
    els.viewJob.disabled = !id || !initialized || !assistantReady || polling;
  }
  function syncJobButtons() {
    const follow = Boolean(pendingJob) || activeLatestJob();
    const followLabel = pendingJob?.body ? "确认上次提交" : "继续查看";
    els.generate.textContent = follow ? followLabel : "开始模拟";
    els.rerun.textContent = follow ? followLabel : "再次模拟";
    els.editConfig.disabled = Boolean(pendingJob);
    renderLatestJob();
  }
  async function refreshLatestJob() {
    try {
      const {job} = await requestJson(endpoints.optimize, {cache: "no-store", signal: AbortSignal.timeout(5000)});
      latestJob = JOB_ID.test(job?.id || "") ? job : null;
      if (pendingJob && (!pendingJob.body || pendingJob.id === latestJob?.id)) rememberJob(null);
    } catch (_error) { /* An unavailable history lookup must not block generation. */ }
  }
  function rememberJob(job) {
    pendingJob = job;
    try {
      if (job) sessionStorage.setItem(JOB_KEY, JSON.stringify(job));
      else sessionStorage.removeItem(JOB_KEY);
    } catch (_error) { /* The current page can still poll. */ }
    renderLatestJob();
  }
  function restoreJob() {
    try {
      const saved = JSON.parse(sessionStorage.getItem(JOB_KEY));
      if (JOB_ID.test(saved?.id || "") && Number.isFinite(saved.createdAt)) pendingJob = saved;
    } catch (_error) { /* No recoverable job. */ }
  }
  function acknowledgeJob(id) {
    if (pendingJob?.id === id && pendingJob.body) rememberJob({id, createdAt: pendingJob.createdAt});
  }
  async function submitJob(job) {
    const receipt = await requestJson(endpoints.optimize, {
      method: "POST", headers: {"Content-Type": "application/json", ...csrfHeaders()},
      body: job.body, signal: AbortSignal.timeout(10000),
    });
    if (receipt.job?.id !== job.id) throw new Error("任务回执不匹配");
    latestJob = receipt.job;
    acknowledgeJob(job.id);
  }
  function jobStatus(message, description) {
    els.results.innerHTML = `<div class="assistant-empty-state" role="status"><strong>${escapeHtml(message)}</strong><span>${escapeHtml(description)}</span></div>`;
    els.explanation.textContent = description;
  }
  async function pollJob(id) {
    let errors = 0;
    let missing = 0;
    const deadline = Date.now() + 12 * 60_000;
    while (Date.now() < deadline) {
      try {
        const {job} = await requestJson(`${endpoints.optimize}${encodeURIComponent(id)}/`, {
          cache: "no-store", signal: AbortSignal.timeout(10000),
        });
        if (job?.id !== id) throw new Error("任务回执不匹配");
        latestJob = {id, state: job.state};
        acknowledgeJob(id);
        if (["done", "failed"].includes(job.state)) return job;
        if (!["queued", "running"].includes(job.state)) throw new Error("无法识别任务状态");
        errors = 0;
        missing = 0;
        jobStatus(job.state === "queued" ? "排队中" : "正在搜索组合", "后台正在处理，回来后可点击任务编号继续查看。");
      } catch (error) {
        if ([401, 403].includes(error.status) || (error.status === 404 && ++missing >= 3)) throw error;
        if (++errors >= 4) throw new Error("暂时无法查询任务，请点击继续查看。不会重复提交。");
      }
      await new Promise(resolve => window.setTimeout(resolve, errors ? Math.min(8000, 2000 * errors) : 2000));
    }
    throw new Error("已暂停轮询，点击继续查看可查询同一任务。");
  }
  async function generate({lookupOnly = false} = {}) {
    if (!initialized || !assistantReady || polling || ownedClearing || els.simcSubmit.disabled) return;
    let requestId = lookupOnly ? latestJob?.id : pendingJob?.id || (activeLatestJob() ? latestJob.id : null);
    if (lookupOnly && !requestId) return;
    const isNew = !requestId;
    polling = true;
    renderLatestJob();
    hideResultTooltip();
    lastPlans = [];
    resultContext = null;
    els.generate.disabled = true;
    els.rerun.disabled = true;
    const controls = [els.classSelect, els.specSelect, els.importSimc, els.simcSubmit, els.editConfig,
      els.flask, els.gems, els.lockGems, els.enchants, els.lockEnchants, els.ai, els.allowMythicLastTwo,
      els.sourcePreference, ...Object.keys(STAT_LABELS).map(key => byId(`target-${key}`))];
    const disabled = controls.map(control => control.disabled);
    controls.forEach(control => { control.disabled = true; });
    els.generate.textContent = "正在模拟…";
    els.rerun.textContent = "正在模拟…";
    els.resultLockSummary.classList.remove("is-stale");
    setWorkbench("results", {focus: true});
    jobStatus(isNew ? "正在提交任务" : "正在查询任务", "辅助配装在后台执行，不需要保持同一个网络请求。");
    try {
      if (isNew) {
        requestId = crypto.randomUUID();
        const body = JSON.stringify({
          request_id: requestId, class_name: currentState.className, spec_name: currentState.specName,
          equipment: lockedEquipmentPayload(), target: targetPayload(), flask: els.flask.value,
          include_gems: els.gems.checked, lock_gems: els.lockGems.checked, include_enchants: els.enchants.checked,
          lock_enchants: els.lockEnchants.checked, use_ai: els.ai.checked, allow_mythic_last_two: els.allowMythicLastTwo.checked,
          source_preference: els.sourcePreference.value,
        });
        rememberJob({id: requestId, body, createdAt: Date.now()});
        try {
          await submitJob(pendingJob);
        } catch (error) {
          if ([400, 401, 403, 409, 413].includes(error.status)) {
            rememberJob(null);
            throw error;
          }
          // If the POST committed but the connection was lost, this ID still resolves.
        }
      }
      let job;
      try {
        job = await pollJob(requestId);
      } catch (error) {
        // A late commit can follow a 404. Never create a replacement UUID.
        if (lookupOnly || isNew || error.status !== 404 || pendingJob?.id !== requestId || !pendingJob.body || Date.now() - pendingJob.createdAt >= 12 * 3600_000) throw error;
        try {
          await submitJob(pendingJob);
        } catch (submitError) {
          // A definitive payload rejection permits editing and a new submission.
          if ([400, 413].includes(submitError.status)) rememberJob(null);
          throw submitError;
        }
        job = await pollJob(requestId);
      }
      if (pendingJob?.id === requestId) rememberJob(null);
      if (job.state === "failed") throw new Error(job.error || "无法生成辅助配装方案。");
      const payload = job.result;
      if (!Array.isArray(payload?.plans) || !payload.plans.length) throw new Error("任务没有返回配装方案。");
      const classRow = builderBootstrap?.classes?.find(row => row.key === job.class_name);
      const specName = classRow?.specs?.find(row => row.key === job.spec_name)?.name || job.spec_name;
      els.resultLockSummary.textContent = `${classRow?.name || job.class_name} · ${specName} · 锁定 ${payload.fixed_slots?.length || 0} 件`;
      lastPlans = payload.plans || [];
      resultContext = {className: job.class_name, specName: job.spec_name, batchKey: payload.catalog?.batch_key};
      renderPlans(lastPlans);
      els.explanation.textContent = payload.explanation || "方案计算完成。";
      toast(`${isNew ? "已生成" : "已读取"} ${lastPlans.length} 套方案。`);
    } catch (error) {
      const uncertain = pendingJob?.id === requestId && pendingJob?.body && Date.now() - pendingJob.createdAt < 12 * 3600_000;
      if (error.status === 404 && !uncertain) {
        if (latestJob?.id === requestId) latestJob = null;
        if (pendingJob?.id === requestId) rememberJob(null);
        await refreshLatestJob();
      } else if (error.status === 409) {
        if (pendingJob?.id === requestId) rememberJob(null);
        await refreshLatestJob();
      }
      const message = error.status === 404 && uncertain ? "尚未确认后台已收到任务。点击确认上次提交会沿用原任务编号。" : error.message;
      jobStatus(lookupOnly || pendingJob?.id === requestId ? "暂时无法查询任务" : "未生成方案", message);
      toast(message, true);
    } finally {
      polling = false;
      controls.forEach((control, index) => { control.disabled = disabled[index]; });
      els.generate.disabled = false;
      els.rerun.disabled = false;
      syncJobButtons();
    }
  }
  function applyPlan(key) {
    const plan = lastPlans.find((row) => row.key === key);
    if (!plan || !resultContext) return;
    const {className, specName, batchKey} = resultContext;
    const draft = loadState(className, specName);
    const next = {...draft, className, specName, equipment: plan.equipment || {}, batchKey: batchKey || draft.batchKey, mode: "equipment", viewMode: "editor"};
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
      els.simcMessage.textContent = `已加入 ${saved.count || 0} 条备选装备记录。`;
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
    hideResultTooltip = WowGearAssistantResults.bindTooltips(els.results);
    els.classSelect.addEventListener("change", async () => {
      if (pendingJob) { syncSelectors(); return; }
      const classRow = (builderBootstrap.classes || []).find((row) => row.key === els.classSelect.value);
      currentState = loadState(classRow.key, classRow.specs?.[0]?.key || "");
      lastPlans = [];
      resetLockedSlots();
      syncSelectors(); renderEquipment(); setWorkbench("config"); await loadAssistantData();
    });
    els.specSelect.addEventListener("change", async () => {
      if (pendingJob) { syncSelectors(); return; }
      currentState = loadState(els.classSelect.value, els.specSelect.value);
      lastPlans = [];
      resetLockedSlots();
      syncSelectors(); renderEquipment(); setWorkbench("config"); await loadAssistantData();
    });
    document.querySelectorAll("[data-library-tab]").forEach((button) => button.addEventListener("click", () => {
      libraryTab = button.dataset.libraryTab;
      renderLibraryTabs();
    }));
    els.equipmentList.addEventListener("click", (event) => {
      if (pendingJob) return;
      const button = event.target.closest("[data-toggle-slot-lock]");
      if (!button) return;
      const slot = button.dataset.toggleSlotLock;
      if (lockedSlots.has(slot)) lockedSlots.delete(slot);
      else lockedSlots.add(slot);
      renderEquipment();
      markResultsStale();
    });
    document.querySelectorAll("[data-lock-action]").forEach((button) => button.addEventListener("click", () => {
      if (pendingJob) return;
      lockedSlots = button.dataset.lockAction === "all"
        ? new Set(currentEquipmentEntries().map(([slot]) => slot))
        : new Set();
      renderEquipment();
      markResultsStale();
    }));
    els.ownedClear.addEventListener("click", clearOwnedItems);
    els.ownedList.addEventListener("click", async (event) => {
      if (ownedClearing || polling) return;
      const button = event.target.closest("[data-delete-owned]");
      if (!button) return;
      try {
        await requestJson(`${endpoints.owned}${button.dataset.deleteOwned}/`, {method: "DELETE", headers: csrfHeaders()});
        assistantData.owned_items = assistantData.owned_items.filter((row) => String(row.id) !== button.dataset.deleteOwned);
        renderOwned(); toast("已从备选装备中移除。");
      } catch (error) { toast(error.message, true); }
    });
    els.allowMythicLastTwo.addEventListener("change", markResultsStale);
    els.sourcePreference.addEventListener("change", markResultsStale);
    els.generate.addEventListener("click", generate);
    els.rerun.addEventListener("click", generate);
    els.viewJob.addEventListener("click", () => generate({lookupOnly: true}));
    els.editConfig.addEventListener("click", () => { if (!polling && !pendingJob) setWorkbench("config", {focus: true}); });
    els.results.addEventListener("click", (event) => { const button = event.target.closest("[data-apply-plan]"); if (button) applyPlan(button.dataset.applyPlan); });
    els.importSimc.addEventListener("click", () => { if (!pendingJob) els.simcDialog.showModal(); });
    els.simcSubmit.addEventListener("click", importSimc);
  }
  async function initialize() {
    restoreJob();
    try {
      builderBootstrap = await requestJson(endpoints.builderBootstrap);
      currentState = readDraft();
      document.addEventListener("visibilitychange", async () => {
        if (document.hidden || polling || !assistantReady) return;
        const origin = currentState;
        const generation = assistantGeneration;
        try {
          const updated = await WowGearState.refresh(origin, requestJson);
          if (currentState === origin && generation === assistantGeneration && !polling && assistantReady) {
            currentState = updated;
            renderEquipment();
          }
        }
        catch (error) { toast(`装备资料刷新失败，已保留当前配装：${error.message}`); }
      });
      resetLockedSlots();
      syncSelectors(); renderEquipment(); renderLibraryTabs(); setWorkbench("config");
      await Promise.all([loadAssistantData(), refreshLatestJob()]);
      bindEvents();
      initialized = true;
      els.classSelect.disabled = els.specSelect.disabled = false;
      els.generate.disabled = els.rerun.disabled = false;
      syncJobButtons();
    } catch (error) {
      els.catalogStatus.textContent = "初始化失败";
      els.resultLockSummary.textContent = "初始化失败";
      els.results.innerHTML = `<div class="assistant-empty-state"><strong>页面初始化失败</strong><span>${escapeHtml(error.message)}</span></div>`;
      setWorkbench("results");
    } finally {
      page.setAttribute("aria-busy", "false");
    }
  }
  initialize();
})();
