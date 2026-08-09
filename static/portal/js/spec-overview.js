(() => {
  "use strict";

  const root = document.getElementById("spec-overview");
  if (!root) return;

  const cards = root.querySelectorAll("[data-spec-module]");
  const scenarioControl = document.getElementById("spec-overview-scenario");
  const profileControl = document.getElementById("spec-overview-profile");
  const resetControl = document.getElementById("spec-overview-reset");
  const moduleRequests = new WeakMap();
  const numberFormat = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 });
  const dateFormat = new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  }

  function asArray(value) { return Array.isArray(value) ? value : []; }
  function firstArray(payload, keys) {
    for (const key of keys) {
      const direct = payload?.[key];
      const nested = payload?.data?.[key];
      if (Array.isArray(direct)) return direct;
      if (Array.isArray(nested)) return nested;
    }
    return [];
  }
  function value(row, keys, fallback = "—") {
    for (const key of keys) {
      if (row?.[key] !== undefined && row?.[key] !== null && row?.[key] !== "") return row[key];
    }
    return fallback;
  }
  function metric(raw, suffix = "") {
    const numeric = Number(raw);
    return Number.isFinite(numeric) ? `${numberFormat.format(numeric)}${suffix}` : String(raw ?? "—");
  }

  const simcReasons = {
    panel_not_public: "基准面板未公开",
    dimension_not_configured: "该专精或场景尚未配置",
    no_comparable_baseline_results: "暂无满足同一冻结条件的基准结果",
    incomplete_frozen_identity: "结果缺少完整冻结身份，暂不参与排名",
  };

  function simcAudit(entry) {
    const versions = entry?.resource_versions || {};
    const identity = [
      ["Profile", versions.profile], ["Template", versions.template],
      ["Backend", versions.backend], ["APL", versions.apl],
    ].filter((item) => item[1]).map((item) => `${item[0]} ${String(item[1]).slice(0, 12)}`);
    const source = entry?.source_result_id ? `结果 #${entry.source_result_id}` : "来源结果未提供";
    return [source, ...identity].join(" · ");
  }

  function rankingList(rows, describe, { ranked = true } = {}) {
    const list = node(ranked ? "ol" : "ul", "spec-module-list");
    rows.slice(0, 20).forEach((row, index) => {
      const description = describe(row, index);
      const item = node("li", `spec-module-row${ranked ? "" : " spec-module-row--fact"}${description.className ? ` ${description.className}` : ""}`);
      const copy = node(description.detail_url ? "a" : "span", "spec-module-copy");
      if (description.detail_url) copy.href = description.detail_url;
      copy.append(node("strong", "", description.title), node("small", "", description.detail));
      if (description.audit) {
        const audit = node("details", "spec-module-audit");
        audit.append(node("summary", "", "查看冻结配置"), node("small", "", description.audit));
        copy.append(audit);
      }
      if (ranked) item.append(node("span", "spec-module-rank", `#${value(row, ["rank"], index + 1)}`));
      item.append(copy, node("span", "spec-module-metric", description.metric));
      list.append(item);
    });
    return list;
  }

  function positiveNumber(raw) {
    const numeric = Number(raw);
    return Number.isFinite(numeric) && numeric > 0 ? numeric : null;
  }

  function sampleDetail(row, extras = []) {
    const count = positiveNumber(value(row, ["sample_size", "count"], null));
    return [...extras.filter(Boolean), count ? `样本 n=${numberFormat.format(count)}` : "样本未提供", count && count < 20 ? "样本有限" : ""].filter(Boolean).join(" · ");
  }

  function overviewDetailUrl(kind, id) {
    const className = root.dataset.className;
    const specName = root.dataset.specName;
    if (!className || !specName || !id) return "";
    return `/portal/spec/${encodeURIComponent(className)}/${encodeURIComponent(specName)}/${kind}/?${kind === "dungeons" ? "dungeon_id" : "boss_id"}=${encodeURIComponent(id)}`;
  }

  function renderPlayers(payload) {
    const players = firstArray(payload, ["players", "items"]);
    if (!players.length) return null;
    return rankingList(players, (player) => ({
      title: value(player, ["character_name", "name"]),
      detail: [
        [value(player, ["realm"], ""), String(value(player, ["region"], "")).toUpperCase()].filter(Boolean).join(" · "),
        positiveNumber(player.item_level) ? `装等 ${metric(player.item_level)}` : "",
        value(player, ["guild_name"], ""),
      ].filter(Boolean).join(" · "),
      metric: `${metric(value(player, ["score", "rating"]))} M+ 评分`,
      detail_url: value(player, ["detail_url"], ""),
    }));
  }

  function renderMythicPlus(payload) {
    const dungeons = firstArray(payload, ["dungeons", "items"]);
    if (!dungeons.length) return null;
    return rankingList(dungeons, (dungeon) => ({
      title: value(dungeon, ["dungeon_name", "name", "short_name"]),
      detail: sampleDetail(dungeon, [
        positiveNumber(dungeon?.keystone?.avg) ? `平均 +${metric(dungeon.keystone.avg)}` : "",
        value(dungeon?.clear_time, ["median_fmt"], ""),
      ]),
      metric: `中位 DPS ${metric(value(dungeon?.dps, ["median", "avg"], "—"))}`,
      detail_url: overviewDetailUrl("dungeons", dungeon.dungeon_id),
    }), { ranked: false });
  }

  function renderRaid(payload) {
    const zones = firstArray(payload, ["zone_groups"]);
    const bosses = firstArray(payload, ["bosses", "items"]);
    if (!zones.length && !bosses.length) return null;
    const container = node("div", "spec-module-zone-list");
    const groups = zones.length ? zones : [{ bosses }];
    groups.forEach((zone) => {
      const zoneBosses = asArray(zone?.bosses);
      if (!zoneBosses.length) return;
      const group = node("section", "spec-module-zone");
      const zoneName = value(zone, ["zone_cn", "zone_name", "name"], "首领数据");
      group.append(node("h3", "spec-module-zone-title", zoneName));
      group.append(rankingList(zoneBosses, (boss) => ({
        title: value(boss, ["boss_name", "name"]),
        detail: sampleDetail(boss, [value(boss?.kill_time, ["median_fmt"], "")]),
        metric: `中位 DPS ${metric(value(boss?.dps, ["median", "avg"], "—"))}`,
        detail_url: overviewDetailUrl("raid", boss.boss_id),
      }), { ranked: false }));
      container.append(group);
    });
    return container.childElementCount ? container : null;
  }

  function renderSimc(payload) {
    const aplRankings = firstArray(payload?.apl, ["apl_rankings", "rankings", "items"]);
    const specRankings = firstArray(payload?.crossSpec, ["spec_rankings", "rankings", "items"]);
    if (!aplRankings.length && !specRankings.length) return null;
    const currentSpec = root.dataset.simcSpec || "";
    const current = specRankings.find((entry) => entry?.spec_key === currentSpec);
    const fragment = node("div", "simc-conclusion");
    if (aplRankings.length) {
      const best = aplRankings[0];
      const conclusion = node("section", "simc-conclusion-hero");
      conclusion.append(node("p", "simc-conclusion-kicker", "当前选择的模拟"));
      conclusion.append(node("h3", "", `${value(best, ["scenario_label", "scenario"], "当前场景")} · ${value(best, ["profile_label"], "Profile 未提供")}`));
      conclusion.append(node("p", "simc-conclusion-apl", `APL：${value(best, ["apl_label", "label", "name"], "未命名 APL")}`));
      conclusion.append(node("strong", "simc-conclusion-dps", metric(value(best, ["dps", "value"]), " DPS")));
      conclusion.append(node("small", "simc-conclusion-rank", `本专精 APL 排名第 ${value(best, ["rank"], 1)} / ${aplRankings.length}`));
      const audit = node("details", "spec-module-audit");
      audit.append(node("summary", "", "查看冻结配置"), node("small", "", simcAudit(best)));
      conclusion.append(audit);
      fragment.append(conclusion);
    }
    if (aplRankings.length > 1) {
      const aplSection = node("section", "simc-secondary");
      aplSection.append(node("h3", "", "本专精其他 APL"));
      aplSection.append(rankingList(aplRankings.slice(1), (entry) => ({
        title: value(entry, ["apl_label", "label", "name"]),
        detail: `${value(entry, ["scenario_label", "scenario"])} · ${value(entry, ["profile_label"], "Profile 未提供")}`,
        audit: simcAudit(entry),
        metric: metric(value(entry, ["dps", "value"]), " DPS"),
      })));
      fragment.append(aplSection);
    }
    if (current) {
      const crossSection = node("section", "simc-secondary");
      const currentRank = specRankings.indexOf(current) + 1;
      crossSection.append(node("h3", "", `同场景位置 · 第 ${currentRank} / ${specRankings.length}`));
      crossSection.append(node("p", "simc-secondary-context", `${value(current, ["scenario_label", "scenario"], "当前场景")} · 各专精使用各自标准 Profile，仅作位置对比`));
      const peers = node("div", "simc-peer-strip");
      [current, ...specRankings.filter((entry) => entry !== current)].slice(0, 5).forEach((entry) => peers.append(node("span", entry === current ? "simc-peer simc-peer--current" : "simc-peer", `${value(entry, ["spec_label", "label", "spec_name", "spec_key"])} · ${metric(value(entry, ["dps", "value"]))}`)));
      crossSection.append(peers);
      fragment.append(crossSection);
    }
    return fragment;
  }

  const renderers = {
    "players": renderPlayers,
    "mythic-plus": renderMythicPlus,
    "raid": renderRaid,
    "simc": renderSimc,
  };

  function selectedDimension() {
    return {
      scenario: scenarioControl?.value || root.dataset.simcDefaultScenario || "",
      profile: profileControl?.value || root.dataset.simcDefaultProfile || "",
    };
  }

  function updateSimcEndpoints() {
    const selected = selectedDimension();
    cards.forEach((card) => {
      if (card.dataset.specModule !== "simc") return;
      const endpoint = new URL(card.dataset.aplEndpoint, window.location.origin);
      endpoint.searchParams.set("scenario", selected.scenario);
      endpoint.searchParams.set("profile", selected.profile);
      card.dataset.aplEndpoint = `${endpoint.pathname}?${endpoint.searchParams.toString()}`;
      const cross = new URL(card.dataset.crossSpecEndpoint, window.location.origin);
      cross.searchParams.set("scenario", selected.scenario);
      card.dataset.crossSpecEndpoint = `${cross.pathname}?${cross.searchParams.toString()}`;
    });
  }

  function readJsonScript(id) {
    const script = document.getElementById(id);
    if (!script) return [];
    try {
      const parsed = JSON.parse(script.textContent || "[]");
      return Array.isArray(parsed) ? parsed : [];
    } catch (_) {
      return [];
    }
  }

  function formatSimcParams(params) {
    const values = params && typeof params === "object" ? params : {};
    const targets = positiveNumber(values.desired_targets) || 1;
    const seconds = positiveNumber(values.max_time) || 300;
    return `${numberFormat.format(targets)} 目标 · ${numberFormat.format(seconds)} 秒`;
  }

  function updateSimcContext(scenarios, profiles) {
    const selected = selectedDimension();
    const scenario = scenarios.find((item) => item.key === selected.scenario);
    const profile = profiles.find((item) => item.key === selected.profile);
    cards.forEach((card) => {
      const context = card.querySelector("[data-simc-context]");
      if (!context) return;
      if (card.dataset.specModule === "simc") {
        context.textContent = `场景：${scenario?.label || selected.scenario || "未配置"}（${formatSimcParams(scenario?.detail)}） · Profile：${profile?.label || selected.profile || "未配置"}`;
      } else {
        context.textContent = `场景：${scenario?.label || selected.scenario || "未配置"}（${formatSimcParams(scenario?.detail)}） · 跨专精使用各专精配置的标准 Profile`;
      }
    });
  }

  function renderDimensionControls() {
    if (!scenarioControl || !profileControl) return;
    const scenarios = readJsonScript("spec-overview-scenarios");
    const profiles = readJsonScript("spec-overview-profiles");
    scenarios.forEach((item) => scenarioControl.append(node("option", "", item.label || item.key)));
    profiles.forEach((item) => profileControl.append(node("option", "", item.label || item.key)));
    scenarios.forEach((item, index) => { scenarioControl.options[index].value = item.key; });
    profiles.forEach((item, index) => { profileControl.options[index].value = item.key; });
    scenarioControl.value = root.dataset.simcDefaultScenario || scenarioControl.value;
    profileControl.value = root.dataset.simcDefaultProfile || profileControl.value;
    updateSimcContext(scenarios, profiles);
    const reload = () => {
      updateSimcContext(scenarios, profiles);
      updateSimcEndpoints();
      cards.forEach((card) => {
        if (card.dataset.specModule === "simc") loadModule(card);
      });
    };
    scenarioControl.addEventListener("change", reload);
    profileControl.addEventListener("change", reload);
    resetControl?.addEventListener("click", () => {
      scenarioControl.value = root.dataset.simcDefaultScenario || scenarioControl.value;
      profileControl.value = root.dataset.simcDefaultProfile || profileControl.value;
      reload();
    });
  }

  function updatedAt(payload) {
    const raw = payload?.updated_at || payload?.data?.updated_at || payload?.result_updated_at;
    if (!raw) return "未提供";
    const parsed = new Date(raw);
    return Number.isNaN(parsed.getTime()) ? String(raw) : dateFormat.format(parsed);
  }

  async function loadModule(card) {
    const previousRequest = moduleRequests.get(card);
    previousRequest?.abort();
    const controller = new AbortController();
    moduleRequests.set(card, controller);
    const endpoint = card.dataset.endpoint;
    const state = card.querySelector("[data-module-state]");
    const content = card.querySelector("[data-module-content]");
    const updated = card.querySelector("[data-module-updated-at]");
    card.setAttribute("data-state", "loading");
    card.setAttribute("aria-busy", "true");
    state.setAttribute("role", "status");
    state.textContent = "加载中…";
    try {
      let payload;
      if (card.dataset.specModule === "simc") {
        const bodies = await Promise.all([card.dataset.aplEndpoint, card.dataset.crossSpecEndpoint].map((url) => fetch(url, {
          credentials: "same-origin", headers: { Accept: "application/json" }, signal: controller.signal,
        }).then((response) => response.ok ? response.json() : ({ status: "not_ready", reason: "request_failed" }))));
        payload = { apl: bodies[0], crossSpec: bodies[1] };
        payload.updated_at = bodies.map((body) => body?.updated_at).filter(Boolean).sort().pop();
        if (bodies.every((body) => body?.status === "not_ready")) {
          card.setAttribute("data-state", "empty");
          state.hidden = false;
          state.textContent = simcReasons[bodies[0].reason] || "暂无数据";
          content.replaceChildren();
          content.hidden = true;
          updated.textContent = updatedAt(bodies[0]);
          return;
        }
      } else {
        const response = await fetch(endpoint, {
          credentials: "same-origin", headers: { Accept: "application/json" }, signal: controller.signal,
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        payload = await response.json();
      }
      if (payload?.status === "not_ready") {
        card.setAttribute("data-state", "empty");
        state.hidden = false;
        state.textContent = simcReasons[payload.reason] || "暂无数据";
        content.replaceChildren();
        content.hidden = true;
        updated.textContent = updatedAt(payload);
        return;
      }
      const rendered = renderers[card.dataset.specModule]?.(payload);
      updated.textContent = updatedAt(payload);
      if (!rendered) {
        card.setAttribute("data-state", "empty");
        state.textContent = "暂无数据";
        content.replaceChildren();
        content.hidden = true;
        return;
      }
      content.replaceChildren(rendered);
      content.hidden = false;
      state.hidden = true;
      card.setAttribute("data-state", "ready");
    } catch (error) {
      if (error?.name === "AbortError") return;
      card.setAttribute("data-state", "error");
      state.hidden = false;
      state.setAttribute("role", "alert");
      state.textContent = "加载失败，请稍后重试；其他模块不受影响。";
      content.replaceChildren();
      content.hidden = true;
    } finally {
      if (moduleRequests.get(card) === controller) {
        card.setAttribute("aria-busy", "false");
      }
    }
  }

  renderDimensionControls();
  cards.forEach(loadModule);
})();
