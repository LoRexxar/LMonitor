(() => {
  "use strict";

  const root = document.getElementById("spec-overview");
  if (!root) return;

  const cards = root.querySelectorAll("[data-spec-module]");
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

  function rankingList(rows, describe) {
    const list = node("ol", "spec-module-list");
    rows.slice(0, 20).forEach((row, index) => {
      const description = describe(row, index);
      const item = node("li", "spec-module-row");
      const rank = node("span", "spec-module-rank", `#${value(row, ["rank"], index + 1)}`);
      const copy = node(description.detail_url ? "a" : "span", "spec-module-copy");
      if (description.detail_url) copy.href = description.detail_url;
      copy.append(node("strong", "", description.title), node("small", "", description.detail));
      item.append(rank, copy, node("span", "spec-module-metric", description.metric));
      list.append(item);
    });
    return list;
  }

  function renderPlayers(payload) {
    const players = firstArray(payload, ["players", "items"]);
    if (!players.length) return null;
    return rankingList(players, (player) => ({
      title: value(player, ["character_name", "name"]),
      detail: [value(player, ["realm"], ""), String(value(player, ["region"], "")).toUpperCase()].filter(Boolean).join(" · "),
      metric: metric(value(player, ["score", "rating"])),
      detail_url: value(player, ["detail_url"], ""),
    }));
  }

  function renderMythicPlus(payload) {
    const dungeons = firstArray(payload, ["dungeons", "items"]);
    if (!dungeons.length) return null;
    return rankingList(dungeons, (dungeon) => ({
      title: value(dungeon, ["dungeon_name", "name", "short_name"]),
      detail: `样本 ${value(dungeon, ["sample_size", "count"], "—")}`,
      metric: metric(value(dungeon?.score || dungeon?.dps || dungeon, ["median", "avg", "score", "dps"])),
    }));
  }

  function renderRaid(payload) {
    let bosses = firstArray(payload, ["bosses", "items"]);
    if (!bosses.length) {
      bosses = firstArray(payload, ["zone_groups"]).flatMap((zone) => asArray(zone?.bosses));
    }
    if (!bosses.length) return null;
    return rankingList(bosses, (boss) => ({
      title: value(boss, ["boss_name", "name"]),
      detail: `史诗难度 · 样本 ${value(boss, ["sample_size", "count"], "—")}`,
      metric: metric(value(boss?.dps || boss, ["median", "avg", "dps"]), " DPS"),
    }));
  }

  function renderSimcApl(payload) {
    const apl_rankings = firstArray(payload, ["apl_rankings", "rankings", "items"]);
    if (!apl_rankings.length) return null;
    return rankingList(apl_rankings, (entry) => ({
      title: value(entry, ["apl_label", "label", "name"]),
      detail: `${value(entry, ["scenario_label", "scenario", "profile_label"])} · ${simcAudit(entry)}`,
      metric: metric(value(entry, ["dps", "value"]), " DPS"),
    }));
  }

  function renderSimcCrossSpec(payload) {
    const spec_rankings = firstArray(payload, ["spec_rankings", "rankings", "items"]);
    if (!spec_rankings.length) return null;
    return rankingList(spec_rankings, (entry) => ({
      title: value(entry, ["spec_label", "label", "spec_name", "spec_key"]),
      detail: `${value(entry, ["scenario_label", "scenario", "profile_label"])} · ${simcAudit(entry)}`,
      metric: metric(value(entry, ["dps", "value"]), " DPS"),
    }));
  }

  const renderers = {
    "players": renderPlayers,
    "mythic-plus": renderMythicPlus,
    "raid": renderRaid,
    "simc-apl": renderSimcApl,
    "simc-cross-spec": renderSimcCrossSpec,
  };

  function updatedAt(payload) {
    const raw = payload?.updated_at || payload?.data?.updated_at || payload?.result_updated_at;
    if (!raw) return "未提供";
    const parsed = new Date(raw);
    return Number.isNaN(parsed.getTime()) ? String(raw) : dateFormat.format(parsed);
  }

  async function loadModule(card) {
    const endpoint = card.dataset.endpoint;
    const state = card.querySelector("[data-module-state]");
    const content = card.querySelector("[data-module-content]");
    const updated = card.querySelector("[data-module-updated-at]");
    card.setAttribute("data-state", "loading");
    card.setAttribute("aria-busy", "true");
    state.setAttribute("role", "status");
    state.textContent = "加载中…";
    try {
      const response = await fetch(endpoint, { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
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
    } catch (_) {
      card.setAttribute("data-state", "error");
      state.hidden = false;
      state.setAttribute("role", "alert");
      state.textContent = "加载失败，请稍后重试；其他模块不受影响。";
      content.replaceChildren();
      content.hidden = true;
    } finally {
      card.setAttribute("aria-busy", "false");
    }
  }

  cards.forEach(loadModule);
})();
