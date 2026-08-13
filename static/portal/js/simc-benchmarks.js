(() => {
  "use strict";

  const LIST_URL = "/portal/api/simc-benchmarks/panels/";
  const numberFormat = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 });
  const dateTimeFormat = new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  });

  function formatResultUpdateTime(value) {
    if (!value) return "暂无数据";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "暂无数据" : dateTimeFormat.format(date);
  }

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  }

  function safeMarkdownHref(value) {
    try {
      const url = new URL(String(value || "").trim(), window.location.origin);
      return ["http:", "https:"].includes(url.protocol) ? url.href : "";
    } catch (_) { return ""; }
  }

  function renderInlineMarkdown(text) {
    const fragment = document.createDocumentFragment();
    const source = String(text || "");
    const tokenPattern = /(`[^`]+`|\[([^\]]+)\]\(([^)]+)\)|\*\*([^*]+)\*\*|__([^_]+)__|\*([^*]+)\*|_([^_]+)_)/g;
    let cursor = 0;
    for (const match of source.matchAll(tokenPattern)) {
      if (match.index > cursor) fragment.append(document.createTextNode(source.slice(cursor, match.index)));
      const token = match[0];
      if (token.startsWith("`")) fragment.append(node("code", "", token.slice(1, -1)));
      else if (match[2] !== undefined) {
        const href = safeMarkdownHref(match[3]);
        if (href) {
          const link = node("a", "", match[2]); link.href = href;
          if (new URL(href).origin !== window.location.origin) { link.target = "_blank"; link.rel = "noopener noreferrer"; }
          fragment.append(link);
        } else fragment.append(document.createTextNode(match[2]));
      } else if (match[4] !== undefined || match[5] !== undefined) fragment.append(node("strong", "", match[4] ?? match[5]));
      else fragment.append(node("em", "", match[6] ?? match[7]));
      cursor = match.index + token.length;
    }
    if (cursor < source.length) fragment.append(document.createTextNode(source.slice(cursor)));
    return fragment;
  }

  function renderMarkdownDescription(markdown, className = "") {
    const root = node("div", `simc-benchmark-markdown ${className}`.trim());
    const lines = String(markdown || "").replace(/\r\n?/g, "\n").split("\n");
    let paragraph = [];
    let list = null;
    let codeLines = null;
    const flushParagraph = () => {
      if (!paragraph.length) return;
      const block = node("p");
      paragraph.forEach((line, index) => { if (index) block.append(node("br")); block.append(renderInlineMarkdown(line)); });
      root.append(block); paragraph = [];
    };
    const flushList = () => { list = null; };
    lines.forEach((line) => {
      if (codeLines !== null) {
        if (/^\s*```/.test(line)) { const pre = node("pre"); pre.append(node("code", "", codeLines.join("\n"))); root.append(pre); codeLines = null; }
        else codeLines.push(line);
        return;
      }
      if (/^\s*```/.test(line)) { flushParagraph(); flushList(); codeLines = []; return; }
      const heading = line.match(/^\s*(#{1,4})\s+(.+)$/);
      const bullet = line.match(/^\s*[-*+]\s+(.+)$/);
      const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
      const quote = line.match(/^\s*>\s?(.*)$/);
      if (!line.trim()) { flushParagraph(); flushList(); return; }
      if (heading) { flushParagraph(); flushList(); const h = node(`h${heading[1].length + 1}`); h.append(renderInlineMarkdown(heading[2])); root.append(h); return; }
      if (bullet || ordered) {
        flushParagraph();
        const tag = bullet ? "ul" : "ol";
        if (!list || list.tagName.toLowerCase() !== tag) { list = node(tag); root.append(list); }
        const item = node("li"); item.append(renderInlineMarkdown((bullet || ordered)[1])); list.append(item); return;
      }
      if (quote) { flushParagraph(); flushList(); const block = node("blockquote"); block.append(renderInlineMarkdown(quote[1])); root.append(block); return; }
      flushList(); paragraph.push(line);
    });
    if (codeLines !== null) { const pre = node("pre"); pre.append(node("code", "", codeLines.join("\n"))); root.append(pre); }
    flushParagraph();
    return root;
  }

  function state(message, kind) {
    const element = node("div", `simc-benchmark-state simc-benchmark-state--${kind || "empty"}`, message);
    element.setAttribute("role", kind === "error" ? "alert" : "status");
    return element;
  }

  async function requestJson(url, options = {}) {
    const response = await fetch(url, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal: options.signal,
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (!data || typeof data !== "object") throw new Error("Invalid response");
    return data;
  }

  function validDps(value) {
    const numeric = Number(value);
    return Number.isFinite(numeric) && numeric >= 0 ? numeric : null;
  }

  function safeIconUrl(value) {
    if (!value) return null;
    try {
      const parsed = new URL(String(value), window.location.origin);
      return ["https:", "http:"].includes(parsed.protocol) ? parsed.href : null;
    } catch (_) { return null; }
  }

  function isBaseline(candidate) {
    return candidate && (candidate.type === "base" || candidate.type === "baseline"
      || candidate.key === "base" || candidate.key === "baseline");
  }
  function rawReportUrlForCoordinate(coordinate) {
    const candidates = Array.isArray(coordinate?.candidates) ? coordinate.candidates : [];
    const candidate = candidates.find(isBaseline) || candidates[0];
    return safeIconUrl(candidate?.raw_report_url);
  }
  function sortCandidates(candidates) { return candidates.slice().sort((a, b) => (validDps(b?.dps) ?? -1) - (validDps(a?.dps) ?? -1)); }

  function scenarioLabel(coordinate) {
    const name = coordinate?.labels?.scenario || coordinate?.scenario_key || "—";
    const detail = coordinate?.scenario_detail || {};
    const targets = Number(detail.desired_targets);
    const maxTime = Number(detail.max_time);
    if (!Number.isFinite(targets) || targets < 1 || !Number.isFinite(maxTime) || maxTime <= 0) return name;
    return `${name} · ${targets} 目标 · ${numberFormat.format(maxTime)} 秒`;
  }

  function comparisonText(candidate, candidates, scale) {
    const dps = validDps(candidate.dps);
    if (dps === null) return "无有效结果";
    const baseline = candidates.find(isBaseline);
    const baselineDps = baseline ? validDps(baseline.dps) : null;
    const highestText = scale.highest > 0 ? `${((dps / scale.highest) * 100).toFixed(1)}% · 最高 DPS` : "—";
    if (baselineDps !== null && baselineDps > 0) {
      const delta = ((dps - baselineDps) / baselineDps) * 100;
      return `${delta > 0 ? "+" : ""}${delta.toFixed(1)}% vs baseline · ${highestText}`;
    }
    return highestText;
  }

  function candidateGearLabel(candidate) {
    const label = String(candidate.label || candidate.key || "候选方案");
    const level = Number(candidate.item_level);
    if (!Number.isFinite(level) || level <= 0) return label;
    return label.replace(new RegExp(`(?:\\s*·\\s*|\\s+装等\\s*)${level}$`), "");
  }

  function groupGearCandidates(candidates) {
    const groups = new Map();
    candidates.forEach((candidate) => {
      const itemId = Number(candidate.item_id);
      const label = candidateGearLabel(candidate);
      const itemIdentity = Number.isFinite(itemId) && itemId > 0 ? `item-${itemId}` : `candidate-${candidate.key || label}`;
      const variantIdentity = candidate.item_variant_key || label;
      const key = `${itemIdentity}|${variantIdentity}`;
      if (!groups.has(key)) {
        groups.set(key, {
          key, label, icon_url: candidate.icon_url || "", effect: candidate.effect || "",
          source_label: candidate.source_label || "", variants: [],
        });
      }
      const group = groups.get(key);
      if (!group.effect && candidate.effect) group.effect = candidate.effect;
      if (!group.source_label && candidate.source_label) group.source_label = candidate.source_label;
      group.variants.push(candidate);
    });
    return Array.from(groups.values()).map((group) => {
      group.variants.sort((left, right) => Number(left.item_level || Number.MAX_SAFE_INTEGER) - Number(right.item_level || Number.MAX_SAFE_INTEGER));
      group.bestDps = Math.max(...group.variants.map((candidate) => validDps(candidate.dps) ?? 0));
      return group;
    }).sort((left, right) => right.bestDps - left.bestDps);
  }

  function buildItemLevelColorMap(groups) {
    const palette = ["#4e79a7", "#f28e2b", "#59a14f", "#e15759", "#b07aa1", "#76b7b2", "#edc948", "#ff9da7", "#9c755f", "#bab0ac"];
    const levels = Array.from(new Set(groups.flatMap((group) => group.variants
      .map((candidate) => Number(candidate.item_level))
      .filter((level) => Number.isFinite(level) && level > 0)))).sort((a, b) => a - b);
    return new Map(levels.map((level, index) => [level, palette[index % palette.length]]));
  }

  function renderGearResultChart(candidates, baseline, scale) {
    const groups = groupGearCandidates(candidates);
    const levelColors = buildItemLevelColorMap(groups);
    const chart = node("div", "simc-benchmark-gear-chart");
    const legend = node("div", "simc-benchmark-gear-level-legend");
    legend.setAttribute("aria-label", "装等颜色图例");
    levelColors.forEach((color, level) => {
      const key = node("span", "simc-benchmark-gear-level-key");
      const swatch = node("i"); swatch.style.backgroundColor = color; swatch.setAttribute("aria-hidden", "true");
      key.append(swatch, node("span", "", `模拟装等 ${level}`)); legend.appendChild(key);
    });
    if (levelColors.size) chart.appendChild(legend);

    const body = node("div", "simc-benchmark-gear-chart-body");
    const guide = node("div", "simc-benchmark-gear-hover-guide"); guide.hidden = true; guide.setAttribute("aria-hidden", "true");
    const tooltip = node("div", "simc-benchmark-gear-tooltip"); tooltip.hidden = true; tooltip.setAttribute("role", "tooltip");
    const itemTooltip = node("div", "simc-benchmark-item-tooltip"); itemTooltip.hidden = true; itemTooltip.setAttribute("role", "tooltip");
    const itemTooltipId = `simc-benchmark-item-tooltip-${Math.random().toString(36).slice(2)}`;
    itemTooltip.id = itemTooltipId;
    body.append(guide, tooltip); chart.appendChild(itemTooltip);
    const position = (dps) => scale.range > 0 ? Math.max(0, Math.min(100, ((dps - scale.lowest) / scale.range) * 100)) : 100;
    const baselineDps = baseline ? validDps(baseline.dps) : null;

    function itemTooltipLineClass(line) {
      if (/^(?:装备：|使用：|被动：|效果：|Equip:|Use:|Passive:|Effect:)/.test(line)) return "simc-benchmark-item-tooltip-effect";
      if (/^\+\d/.test(line)) return "simc-benchmark-item-tooltip-stat";
      return "";
    }

    function showItemTooltip(event, group, identity) {
      const levels = Array.from(new Set(group.variants.map((candidate) => Number(candidate.item_level))
        .filter((level) => Number.isFinite(level) && level > 0))).join(" / ");
      const details = String(group.effect || "").replace(/\r\n?/g, "\n").split("\n")
        .map((line) => line.trim()).filter(Boolean);
      itemTooltip.replaceChildren(
        node("strong", "simc-benchmark-item-tooltip-name", group.label),
        ...(levels ? [node("span", "simc-benchmark-item-tooltip-level", `本次模拟输入装等：${levels}`)] : []),
        ...(group.source_label ? [node("span", "simc-benchmark-item-tooltip-source", `候选与模拟装等来源：${group.source_label}`)] : []),
        ...(details.length
          ? details.map((line) => node("span", itemTooltipLineClass(line), line))
          : [node("span", "simc-benchmark-item-tooltip-empty", "暂无冻结的物品 tooltip 信息")]),
        ...(details.length ? [node("span", "simc-benchmark-item-tooltip-note", "属性与特效为物品静态 tooltip 快照，仅用于识别；它们不替代本次模拟输入装等。")] : []),
      );
      const rect = identity.getBoundingClientRect();
      const maxLeft = Math.max(8, window.innerWidth - 370);
      itemTooltip.style.left = `${Math.min(maxLeft, Math.max(8, rect.left))}px`;
      itemTooltip.style.top = `${Math.max(8, rect.bottom + 8)}px`;
      itemTooltip.hidden = false;
    }

    function hideItemTooltip() { itemTooltip.hidden = true; }

    groups.forEach((group) => {
      const row = node("div", "simc-benchmark-gear-row");
      const identity = node("button", "simc-benchmark-gear-identity");
      identity.type = "button";
      identity.setAttribute("aria-describedby", itemTooltipId);
      identity.setAttribute("aria-label", `查看 ${group.label} 的装备说明`);
      const iconUrl = safeIconUrl(group.icon_url);
      if (iconUrl) {
        const icon = node("img", "simc-benchmark-candidate-icon");
        icon.src = iconUrl; icon.alt = ""; icon.loading = "lazy"; icon.addEventListener("error", () => icon.remove(), { once: true });
        identity.appendChild(icon);
      }
      identity.appendChild(node("strong", "simc-benchmark-gear-name", group.label));
      identity.addEventListener("pointerenter", (event) => showItemTooltip(event, group, identity));
      identity.addEventListener("pointerleave", hideItemTooltip);
      identity.addEventListener("focus", (event) => showItemTooltip(event, group, identity));
      identity.addEventListener("blur", hideItemTooltip);
      identity.addEventListener("click", (event) => {
        event.preventDefault();
        if (itemTooltip.hidden) showItemTooltip(event, group, identity);
        else hideItemTooltip();
      });
      const plot = node("div", "simc-benchmark-gear-plot");
      let previousDps = scale.lowest;
      group.variants.forEach((candidate) => {
        const dps = validDps(candidate.dps) ?? previousDps;
        const level = Number(candidate.item_level);
        const start = position(previousDps); const end = position(dps);
        const segment = node("button", "simc-benchmark-gear-segment", Number.isFinite(level) && level > 0 ? String(level) : "装备");
        segment.type = "button";
        segment.style.left = `${Math.min(start, end)}%`;
        segment.style.width = `${Math.max(0.45, Math.abs(end - start))}%`;
        segment.style.backgroundColor = levelColors.get(level) || "#64748b";
        segment.setAttribute("aria-label", `${group.label} ${Number.isFinite(level) && level > 0 ? `模拟装等 ${level}` : ""} ${numberFormat.format(dps)} DPS`);
        const moveTooltip = (event) => {
          const rect = body.getBoundingClientRect();
          if (Number.isFinite(event?.clientX) && Number.isFinite(event?.clientY)) {
            tooltip.style.left = `${Math.min(Math.max(8, rect.width - 190), Math.max(8, event.clientX - rect.left + 12))}px`;
            tooltip.style.top = `${Math.max(8, event.clientY - rect.top - 58)}px`;
          } else {
            tooltip.style.left = `${Math.max(8, rect.width / 2 - 90)}px`;
            tooltip.style.top = `${Math.max(8, row.offsetTop - 8)}px`;
          }
        };
        const showComparison = (event) => {
          row.classList.add("is-hovered"); guide.hidden = false; tooltip.hidden = false;
          const plotRect = plot.getBoundingClientRect(); const bodyRect = body.getBoundingClientRect();
          guide.style.left = `${plotRect.left - bodyRect.left + plotRect.width * end / 100}px`;
          const delta = baselineDps && baselineDps > 0 ? (dps - baselineDps) * 100 / baselineDps : null;
          tooltip.replaceChildren(node("strong", "", group.label), node("span", "", `${Number.isFinite(level) && level > 0 ? `模拟装等 ${level} · ` : ""}${numberFormat.format(dps)} DPS`), node("span", "", delta === null ? "无基准对比" : `相对基准 ${delta >= 0 ? "+" : ""}${delta.toFixed(2)}%`));
          moveTooltip(event);
        };
        const hideComparison = () => { row.classList.remove("is-hovered"); guide.hidden = true; tooltip.hidden = true; };
        segment.addEventListener("pointerenter", showComparison); segment.addEventListener("pointermove", moveTooltip); segment.addEventListener("pointerleave", hideComparison);
        segment.addEventListener("focus", showComparison); segment.addEventListener("blur", hideComparison);
        plot.appendChild(segment); previousDps = dps;
      });
      const best = group.variants.reduce((winner, candidate) => (validDps(candidate.dps) ?? -1) > (validDps(winner.dps) ?? -1) ? candidate : winner, group.variants[0]);
      const metrics = node("div", "simc-benchmark-candidate-metrics");
      metrics.append(node("div", "simc-benchmark-candidate-value", `${numberFormat.format(validDps(best.dps) ?? 0)} DPS`), node("div", "simc-benchmark-relative", comparisonText(best, [baseline, ...candidates].filter(Boolean), scale)));
      row.append(identity, plot, metrics); body.appendChild(row);
    });
    chart.appendChild(body); return chart;
  }

  function profileTalentSimulatorUrl(identity, buildCode, versionKey) {
    const className = String(identity?.class_name || "").trim().toLowerCase();
    const specName = String(identity?.spec_key || identity?.spec || "").trim().toLowerCase();
    const code = String(buildCode || "").trim();
    const version = String(versionKey || "").trim();
    if (!className || !specName || !code) return "";
    const classAliases = { deathknight: "DeathKnight", demonhunter: "DemonHunter" };
    const toPascalCase = (value) => value.split("_").filter(Boolean)
      .map((part) => part.charAt(0).toUpperCase() + part.slice(1)).join("");
    const params = new URLSearchParams();
    params.set('class', classAliases[className] || toPascalCase(className));
    params.set('spec', toPascalCase(specName));
    params.set('code', code);
    if (version) params.set('version', version);
    return `/portal/talents/?${params.toString()}`;
  }

  function appendProfileFactSection(body, title, entries, className = "") {
    const facts = node("dl", `simc-benchmark-profile-identity ${className}`.trim());
    entries.forEach(([label, value]) => {
      if (value === null || value === undefined || value === "") return;
      facts.append(node("dt", "", label), node("dd", "", value));
    });
    if (!facts.childElementCount) return;
    const section = node("section", "simc-benchmark-profile-section");
    section.append(node("h4", "", title), facts);
    body.appendChild(section);
  }

  function renderProfileDetails(profileDetail, simulationDetail, audit, summaryText = "展开 Profile 配置", reportUrl = "") {
    const details = node("details", "simc-benchmark-profile-details");
    const summary = node("summary", "profile-details-toggle", summaryText);
    if (!summaryText) summary.hidden = true;
    details.appendChild(summary);
    const hasProfile = profileDetail && typeof profileDetail === "object";
    const hasSimulation = simulationDetail && typeof simulationDetail === "object";
    if (!hasProfile && !hasSimulation) {
      details.appendChild(node("div", "simc-benchmark-profile-empty", "该 Profile 没有可展示的配置内容"));
      return details;
    }
    profileDetail = hasProfile ? profileDetail : {};
    simulationDetail = hasSimulation ? simulationDetail : {};
    const identity = profileDetail.identity || {};
    const basics = node("dl", "simc-benchmark-profile-identity");
    [
      ["角色", identity.name], ["职业", identity.class_name], ["专精", identity.spec],
      ["种族", identity.race], ["等级", identity.level], ["服务器", identity.realm],
      ["使用 APL", audit?.apl_label],
    ].forEach(([label, value]) => {
      if (value === null || value === undefined || value === "") return;
      basics.append(node("dt", "", label), node("dd", "", value));
    });
    const body = node("div", "simc-benchmark-profile-detail-body");
    if (reportUrl) {
      const actions = node("div", "simc-benchmark-profile-actions");
      const link = node("a", "simc-benchmark-profile-report-link", "查看 SimC 原始报告");
      link.href = reportUrl;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      actions.appendChild(link);
      body.appendChild(actions);
    }
    if (basics.childElementCount) {
      const section = node("section", "simc-benchmark-profile-section");
      section.append(node("h4", "", "基础信息"), basics); body.appendChild(section);
    }
    const fightStyle = simulationDetail?.fight_style?.label || simulationDetail?.fight_style?.value;
    const simulationNumber = (value) => (
      value === null || value === undefined || value === "" ? null : Number(value)
    );
    const targets = simulationNumber(simulationDetail?.desired_targets);
    const maxTime = simulationNumber(simulationDetail?.max_time);
    const varyCombatLength = simulationNumber(simulationDetail?.vary_combat_length);
    const iterations = simulationNumber(simulationDetail?.iterations);
    const targetError = simulationNumber(simulationDetail?.target_error);
    appendProfileFactSection(body, "战斗场景", [
      ["战斗类型", fightStyle],
      ["目标数量", Number.isFinite(targets) ? numberFormat.format(targets) : null],
      ["模拟时长", Number.isFinite(maxTime) ? `${numberFormat.format(maxTime)} 秒` : null],
      ["时长浮动", Number.isFinite(varyCombatLength) ? `${numberFormat.format(varyCombatLength * 100)}%` : null],
      ["敌人类型", simulationDetail?.enemy_type],
      ["迭代次数", Number.isFinite(iterations) ? numberFormat.format(iterations) : null],
      ["目标误差", Number.isFinite(targetError) ? `${numberFormat.format(targetError * 100)}%` : null],
    ], "simc-benchmark-simulation-facts");
    const classRaidBuffs = Array.isArray(simulationDetail?.class_raid_buffs)
      ? simulationDetail.class_raid_buffs.map((buff) => buff?.label || buff?.value).filter(Boolean) : [];
    const raidBuffs = Array.isArray(simulationDetail?.raid_buffs)
      ? simulationDetail.raid_buffs.map((buff) => buff?.label || buff?.value).filter(Boolean) : [];
    appendProfileFactSection(body, "团队 Buff", [
      ["职业自身团队增益", simulationDetail?.use_class_raid_buff
        ? `已启用${classRaidBuffs.length ? ` · ${classRaidBuffs.join("、")}` : ""}`
        : "未启用"],
      ["额外团队增益", raidBuffs.length ? raidBuffs.join("、") : "未启用额外团队增益"],
    ], "simc-benchmark-simulation-facts");
    const talentCode = profileDetail?.talents?.build_code;
    if (talentCode) {
      const section = node("section", "simc-benchmark-profile-section");
      const simulatorUrl = profileTalentSimulatorUrl(
        identity, talentCode, profileDetail?.talent_version,
      );
      const talentRow = node("div", "simc-benchmark-profile-talent-row");
      talentRow.appendChild(node("code", "simc-benchmark-profile-talent-code", talentCode));
      if (simulatorUrl) {
        const link = node("a", "simc-benchmark-profile-talent-link", "打开天赋模拟器");
        link.href = simulatorUrl;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        talentRow.appendChild(link);
      }
      section.append(node("h4", "", "天赋"), talentRow); body.appendChild(section);
    }
    const equipment = Array.isArray(profileDetail.equipment) ? profileDetail.equipment : [];
    if (equipment.length) {
      const section = node("section", "simc-benchmark-profile-section");
      const list = node("div", "simc-benchmark-profile-equipment");
      equipment.forEach((item) => {
        const row = node("div", "simc-benchmark-profile-equipment-row");
        const name = item?.display_name || item?.name_zh || item?.name || `#${item?.item_id || "—"}`;
        const description = String(item?.display_description || "").trim();
        if (description) {
          row.setAttribute("data-wow-item-tooltip", description);
          row.setAttribute("data-wow-item-tooltip-name", name);
          row.tabIndex = 0;
        }
        const meta = [item?.item_level ? `装等 ${item.item_level}` : "", item?.enchant?.display_name ? `附魔：${item.enchant.display_name}` : ""]
          .filter(Boolean).join(" · ");
        row.append(node("span", "simc-benchmark-profile-equipment-slot", item?.slot_label || item?.slot || "装备"));
        const copy = node("span", "simc-benchmark-profile-equipment-copy");
        const iconUrl = safeIconUrl(item?.icon_url);
        if (iconUrl) {
          const icon = node("img", "wow-item-icon");
          icon.src = iconUrl; icon.alt = ""; icon.loading = "lazy";
          icon.addEventListener("error", () => icon.remove(), { once: true });
          copy.appendChild(icon);
        }
        const text = node("span", "simc-benchmark-profile-equipment-text");
        text.append(node("strong", "", name));
        if (meta) text.appendChild(node("small", "", meta));
        copy.appendChild(text);
        list.appendChild(row); row.appendChild(copy);
      });
      section.append(node("h4", "", `装备 (${equipment.length})`), list); body.appendChild(section);
    }
    details.appendChild(body);
    return details;
  }

  function renderCoordinate(coordinate) {
    const allCandidates = Array.isArray(coordinate?.candidates) ? coordinate.candidates : [];
    const baseline = allCandidates.find(isBaseline);
    const candidates = sortCandidates(allCandidates.filter((candidate) => !isBaseline(candidate)));
    const caseNode = node("section", "simc-benchmark-case");
    const info = node("div", "simc-benchmark-basic-info");
    [
      ["simc-benchmark-info-spec", "专精", coordinate?.labels?.spec || coordinate?.spec_key],
      ["simc-benchmark-info-profile", "Profile", coordinate?.labels?.profile || coordinate?.profile_key],
      ["simc-benchmark-info-scenario", "场景", scenarioLabel(coordinate)],
    ].forEach(([className, label, value]) => {
      const item = node("div", className);
      item.append(node("span", "simc-benchmark-info-label", label), node("strong", "simc-benchmark-info-value", value || "—"));
      info.appendChild(item);
    });
    caseNode.append(info, renderProfileDetails(
      coordinate?.profile_detail, coordinate?.simulation_detail, coordinate?.audit,
      "展开 Profile 配置", rawReportUrlForCoordinate(coordinate),
    ));
    if (!candidates.length) { caseNode.appendChild(state("当前坐标暂无已完成候选结果", "empty")); return caseNode; }
    const values = candidates.map((candidate) => validDps(candidate?.dps)).filter((value) => value !== null);
    const lowest = values.length ? Math.min(...values) : 0;
    const highest = values.length ? Math.max(...values) : 0;
    const scale = { lowest, highest, range: highest - lowest };
    const axis = node("div", "simc-benchmark-axis-labels");
    [0, 25, 50, 75, 100].forEach((value) => axis.appendChild(node("span", "simc-benchmark-axis-label", `${value}%`)));
    const chart = renderGearResultChart(candidates, baseline, scale);
    const range = node("div", "simc-benchmark-range-note", scale.range > 0
      ? `区间对比：${numberFormat.format(lowest)} DPS = 0%，${numberFormat.format(highest)} DPS = 100%`
      : "区间内结果相同");
    caseNode.append(range, axis, chart); return caseNode;
  }

  function renderSpecComparison(shell, payload, { syncLocation = false, detailUrl = "" } = {}) {
    let coordinateOptions = Array.isArray(payload?.results?.coordinate_options)
      ? payload.results.coordinate_options : [];
    let rows = Array.isArray(payload?.results?.coordinates) ? payload.results.coordinates : [];
    if (!coordinateOptions.length) coordinateOptions = rows;
    const scenarios = new Map();
    coordinateOptions.forEach((coordinate) => {
      const key = String(coordinate?.scenario_key || "");
      if (key && !scenarios.has(key)) scenarios.set(key, scenarioLabel(coordinate));
    });
    if (!scenarios.size) {
      shell.body.replaceChildren(state("暂无可对比的职业专精结果", "not-ready"));
      return;
    }

    const params = new URLSearchParams(syncLocation ? window.location.search : "");
    const currentScenario = String(rows[0]?.scenario_key || "");
    const requestedScenario = params.get("scenario") || currentScenario;
    const selectedScenario = scenarios.has(requestedScenario)
      ? requestedScenario : scenarios.keys().next().value;
    const controls = node("div", "simc-benchmark-spec-controls");
    const label = node("label", "simc-benchmark-filter");
    label.appendChild(node("span", "simc-benchmark-filter-label", "场景"));
    const select = node("select", "simc-benchmark-filter-select");
    scenarios.forEach((text, value) => {
      const option = node("option", "", text); option.value = value; select.appendChild(option);
    });
    select.value = selectedScenario;
    label.appendChild(select);
    controls.append(label, node("p", "simc-benchmark-spec-axis-note", "纵轴：职业专精 · 横向条：DPS（相对本场景最高值）"));
    const result = node("div", "simc-benchmark-spec-results");

    const syncUrl = () => {
      if (!syncLocation) return;
      const next = new URLSearchParams(window.location.search);
      next.set("scenario", select.value);
      next.delete("spec"); next.delete("profile"); next.delete("selected");
      window.history.replaceState(null, "", `${window.location.pathname}?${next.toString()}`);
    };
    const renderRows = (coordinates) => {
      const projected = coordinates.map((coordinate) => {
        const candidates = Array.isArray(coordinate?.candidates) ? coordinate.candidates : [];
        const baseline = candidates.find(isBaseline);
        return { coordinate, dps: baseline ? validDps(baseline.dps) : null };
      }).sort((left, right) => {
        if (left.dps === null) return right.dps === null ? 0 : 1;
        if (right.dps === null) return -1;
        return right.dps - left.dps;
      });
      const highest = projected.reduce((value, row) => Math.max(value, row.dps || 0), 0);
      const chart = node("div", "simc-benchmark-spec-chart");
      projected.forEach((entry, index) => {
        const coordinate = entry.coordinate;
        const row = node("div", "simc-benchmark-spec-row");
        const toggle = node("button", "simc-benchmark-spec-row-toggle");
        toggle.type = "button";
        const identity = node("div", "simc-benchmark-spec-identity");
        identity.appendChild(node("span", "simc-benchmark-spec-rank", entry.dps === null ? "—" : String(index + 1)));
        const iconUrl = safeIconUrl(coordinate?.spec_icon_url);
        if (iconUrl) {
          const icon = node("img", "simc-benchmark-spec-icon");
          icon.src = iconUrl; icon.alt = ""; icon.loading = "lazy";
          icon.addEventListener("error", () => icon.remove(), { once: true });
          identity.appendChild(icon);
        }
        const copy = node("div", "simc-benchmark-spec-copy");
        copy.appendChild(node("strong", "simc-benchmark-spec-name", coordinate?.labels?.spec || coordinate?.spec_key || "未知专精"));
        const profile = coordinate?.labels?.profile || coordinate?.profile_key;
        if (profile) copy.appendChild(node("small", "simc-benchmark-spec-profile", profile));
        identity.appendChild(copy);
        const track = node("div", "simc-benchmark-spec-track");
        const bar = node("div", "simc-benchmark-spec-bar");
        bar.style.width = `${highest > 0 && entry.dps !== null ? Math.max(0.8, entry.dps * 100 / highest) : 0}%`;
        track.appendChild(bar);
        const metrics = node("div", "simc-benchmark-spec-metrics");
        metrics.appendChild(node("strong", "simc-benchmark-spec-dps", entry.dps === null ? "暂无结果" : `${numberFormat.format(entry.dps)} DPS`));
        metrics.appendChild(node("small", "simc-benchmark-spec-relative", entry.dps === null || highest <= 0 ? "该场景未完成" : `相对最高 ${(entry.dps * 100 / highest).toFixed(1)}%`));
        let profileDetails = renderProfileDetails(
          coordinate?.profile_detail, coordinate?.simulation_detail, coordinate?.audit,
          "", rawReportUrlForCoordinate(coordinate),
        );
        const detailId = `simc-benchmark-spec-profile-${index}`;
        profileDetails.id = detailId;
        profileDetails.classList.add("simc-benchmark-spec-profile-details");
        toggle.setAttribute("aria-expanded", "false");
        toggle.setAttribute("aria-controls", detailId);
        toggle.setAttribute("aria-label", `${coordinate?.labels?.spec || coordinate?.spec_key || "职业专精"} Profile`);
        let profileLoading = false;
        toggle.addEventListener("click", async () => {
          if (profileDetails.open) {
            profileDetails.open = false;
            toggle.setAttribute("aria-expanded", "false");
            row.classList.remove("is-expanded");
            return;
          }
          profileDetails.open = true;
          toggle.setAttribute("aria-expanded", "true");
          row.classList.add("is-expanded");
          if (coordinate?.profile_detail || !detailUrl || profileLoading) return;
          profileLoading = true;
          toggle.setAttribute("aria-busy", "true");
          profileDetails.replaceChildren(state("正在加载冻结 Profile…", "loading"));
          const query = new URLSearchParams();
          query.set("selected", "1");
          query.set("spec", coordinate?.spec_key || "");
          query.set("profile", coordinate?.profile_key || "");
          query.set("scenario", coordinate?.scenario_key || select.value);
          try {
            const nextPayload = await requestJson(`${detailUrl}?${query.toString()}`);
            const nextRows = Array.isArray(nextPayload?.results?.coordinates)
              ? nextPayload.results.coordinates : [];
            const nextCoordinate = nextRows.find((row) => (
              row?.spec_key === coordinate?.spec_key
              && row?.profile_key === coordinate?.profile_key
              && row?.scenario_key === coordinate?.scenario_key
            ));
            if (!nextCoordinate?.profile_detail) throw new Error("Frozen profile detail unavailable");
            coordinate.profile_detail = nextCoordinate.profile_detail;
            coordinate.simulation_detail = nextCoordinate.simulation_detail;
            coordinate.candidates = nextCoordinate.candidates;
            const loadedDetails = renderProfileDetails(
              nextCoordinate?.profile_detail, nextCoordinate?.simulation_detail, nextCoordinate?.audit,
              "", rawReportUrlForCoordinate(nextCoordinate),
            );
            loadedDetails.id = detailId;
            loadedDetails.classList.add("simc-benchmark-spec-profile-details");
            loadedDetails.open = true;
            profileDetails.replaceWith(loadedDetails);
            profileDetails = loadedDetails;
          } catch (error) {
            profileDetails.replaceChildren(state("冻结 Profile 加载失败，请稍后重试", "error"));
          } finally {
            profileLoading = false;
            toggle.removeAttribute("aria-busy");
          }
        });
        toggle.append(identity, track, metrics);
        row.append(toggle, profileDetails); chart.appendChild(row);
      });
      result.replaceChildren(projected.length ? chart : state("该场景暂无职业专精结果", "empty"));
    };
    renderRows(rows);
    syncUrl();

    let requestController = null;
    select.addEventListener("change", async () => {
      syncUrl();
      if (!detailUrl) return;
      if (requestController) requestController.abort();
      requestController = new AbortController();
      const current = requestController;
      const query = new URLSearchParams();
      query.set("selected", "1");
      query.set("scenario", select.value);
      result.replaceChildren(state("正在加载场景对比…", "loading"));
      try {
        const nextPayload = await requestJson(`${detailUrl}?${query.toString()}`, { signal: current.signal });
        if (current !== requestController) return;
        rows = Array.isArray(nextPayload?.results?.coordinates) ? nextPayload.results.coordinates : [];
        renderRows(rows);
      } catch (error) {
        if (error?.name !== "AbortError" && current === requestController) {
          result.replaceChildren(state("场景对比加载失败，请稍后重试", "error"));
        }
      }
    });
    shell.body.replaceChildren(controls, result);
  }

  function renderResults(shell, payload, { syncLocation = false, detailUrl = "" } = {}) {
    if (payload?.result_view === "spec_comparison") {
      renderSpecComparison(shell, payload, { syncLocation, detailUrl });
      return;
    }
    const coordinates = Array.isArray(payload?.results?.coordinates) ? payload.results.coordinates : [];
    let coordinateOptions = Array.isArray(payload?.results?.coordinate_options)
      ? payload.results.coordinate_options
      : coordinates;
    if (!coordinateOptions.length) { shell.body.replaceChildren(state("暂无已完成模拟结果", "not-ready")); return; }
    const params = new URLSearchParams(syncLocation ? window.location.search : "");
    const requested = {
      spec: params.get("spec") || "",
      profile: params.get("profile") || "",
      scenario: params.get("scenario") || "",
    };
    const dimensions = [["spec_key", "spec", "专精"], ["profile_key", "profile", "Profile"], ["scenario_key", "scenario", "场景"]];
    const filters = node("div", "simc-benchmark-filters");
    const selected = {};
    dimensions.forEach(([key, labelKey, title]) => {
      const label = node("label", "simc-benchmark-filter"); label.appendChild(node("span", "simc-benchmark-filter-label", title));
      const select = node("select", "simc-benchmark-filter-select"); select.dataset.dimension = key;
      selected[key] = select; label.appendChild(select); filters.appendChild(label);
    });

    const availableCoordinates = (key) => coordinateOptions.filter((coordinate) => {
      if (key !== "spec_key" && String(coordinate?.spec_key || "") !== selected.spec_key.value) return false;
      if (key === "scenario_key" && String(coordinate?.profile_key || "") !== selected.profile_key.value) return false;
      return true;
    });
    const syncFilterOptions = (key, preferred = "") => {
      const [, labelKey] = dimensions.find(([dimension]) => dimension === key);
      const select = selected[key]; const previous = preferred || select.value; const options = new Map();
      availableCoordinates(key).forEach((coordinate) => {
        const value = String(coordinate?.[key] || "");
        const label = key === "scenario_key"
          ? scenarioLabel(coordinate)
          : String(coordinate?.labels?.[labelKey] || value);
        if (value && !options.has(value)) options.set(value, label);
      });
      select.replaceChildren();
      options.forEach((label, value) => { const option = node("option", "", label); option.value = value; select.appendChild(option); });
      select.value = options.has(previous) ? previous : (options.keys().next().value || "");
    };
    const syncDependentFilters = (preferredProfile = "", preferredScenario = "") => {
      syncFilterOptions("profile_key", preferredProfile);
      syncFilterOptions("scenario_key", preferredScenario);
    };
    syncFilterOptions("spec_key", requested.spec);
    syncDependentFilters(requested.profile, requested.scenario);

    const selectedResult = node("div", "simc-benchmark-cases");
    const syncUrl = () => {
      if (!syncLocation) return;
      const params = new URLSearchParams(window.location.search);
      params.set("spec", selected.spec_key.value);
      params.set("profile", selected.profile_key.value);
      params.set("scenario", selected.scenario_key.value);
      params.delete("selected");
      window.history.replaceState(null, "", `${window.location.pathname}?${params.toString()}`);
    };
    const matchesSelection = (item) => dimensions.every(
      ([key]) => String(item?.[key] || "") === selected[key].value,
    );
    const renderCoordinateResult = (rows) => {
      const coordinate = rows.find(matchesSelection) || rows[0];
      selectedResult.replaceChildren(coordinate ? renderCoordinate(coordinate) : state("当前筛选条件下没有结果", "empty"));
    };
    let coordinateRequestController = null;
    const loadSelectedCoordinate = async () => {
      syncUrl();
      if (!detailUrl) {
        renderCoordinateResult(coordinates);
        return;
      }
      if (coordinateRequestController) coordinateRequestController.abort();
      coordinateRequestController = new AbortController();
      const requestController = coordinateRequestController;
      const params = new URLSearchParams();
      params.set("selected", "1");
      params.set("spec", selected.spec_key.value);
      params.set("profile", selected.profile_key.value);
      params.set("scenario", selected.scenario_key.value);
      selectedResult.replaceChildren(state("正在加载当前结果…", "loading"));
      try {
        const nextPayload = await requestJson(`${detailUrl}?${params.toString()}`, { signal: requestController.signal });
        if (requestController !== coordinateRequestController) return;
        const rows = Array.isArray(nextPayload?.results?.coordinates) ? nextPayload.results.coordinates : [];
        const nextOptions = Array.isArray(nextPayload?.results?.coordinate_options)
          ? nextPayload.results.coordinate_options
          : [];
        if (nextOptions.length) coordinateOptions = nextOptions;
        const resolved = rows[0] || null;
        syncFilterOptions("spec_key", String(resolved?.spec_key || ""));
        syncDependentFilters(
          String(resolved?.profile_key || ""),
          String(resolved?.scenario_key || ""),
        );
        syncUrl();
        renderCoordinateResult(rows);
      } catch (error) {
        if (error?.name !== "AbortError" && requestController === coordinateRequestController) {
          selectedResult.replaceChildren(state("当前结果加载失败，请稍后重试", "error"));
        }
      }
    };
    selected.spec_key.addEventListener("change", () => { syncDependentFilters(); loadSelectedCoordinate(); });
    selected.profile_key.addEventListener("change", () => { syncFilterOptions("scenario_key"); loadSelectedCoordinate(); });
    selected.scenario_key.addEventListener("change", loadSelectedCoordinate);
    renderCoordinateResult(coordinates);
    syncUrl();
    shell.body.replaceChildren(filters, selectedResult);
  }

  function applyPanelHeading(panel) {
    const name = String(panel?.name || "模拟结果");
    const description = String(panel?.description || "");
    const title = document.getElementById("simc-benchmarks-title");
    const descriptionCard = document.getElementById("simc-benchmarks-description");
    const copy = descriptionCard?.querySelector(".simc-benchmark-description-content");
    if (title) title.textContent = name;
    if (copy) {
      copy.replaceChildren(renderMarkdownDescription(description));
      descriptionCard.hidden = !description;
    }
    document.title = `${name} · WowDaily.cn`;
  }

  function panelShell(panel) {
    const article = node("article", "simc-benchmark-panel"); article.dataset.benchmarkPanelId = String(panel.id || "");
    const header = node("header", "simc-benchmark-panel-header"); const copy = node("div", "simc-benchmark-panel-copy");
    copy.append(node("h3", "simc-benchmark-panel-title", panel.name || panel.slug || ""));
    if (panel.description) copy.appendChild(renderMarkdownDescription(panel.description, "simc-benchmark-panel-description"));
    header.appendChild(copy);
    const body = node("div", "simc-benchmark-panel-body"); article.append(header, body); return { article, body };
  }

  async function loadPanel(panel, root, { setPageHeading = false } = {}) {
    const shell = panelShell(panel); root.appendChild(shell.article); shell.body.appendChild(state("正在加载模拟结果…", "loading"));
    try {
      const panelId = panel.id;
      const detailUrl = `${LIST_URL}${encodeURIComponent(String(panelId))}/`;
      const params = new URLSearchParams(setPageHeading ? window.location.search : "");
      params.set("selected", "1");
      const payload = await requestJson(`${detailUrl}?${params.toString()}`);
      if (payload.status === "ready") {
        if (setPageHeading) applyPanelHeading(payload.panel || panel);
        renderResults(shell, payload, { syncLocation: setPageHeading, detailUrl });
      } else shell.body.replaceChildren(state("暂无已完成模拟结果", "not-ready"));
    } catch (_) { shell.body.replaceChildren(state("Benchmark 结果加载失败，请稍后重试", "error")); }
  }

  function renderPanelList(root, panels) {
    const list = node("div", "simc-benchmark-list");
    list.setAttribute("role", "list");

    const listHeader = node("div", "simc-benchmark-list-header");
    listHeader.setAttribute("aria-hidden", "true");
    listHeader.append(
      node("span", "", "模拟任务"),
      node("span", "", "数据概览"),
      node("span", "", "操作"),
    );
    list.appendChild(listHeader);

    panels.forEach((panel) => {
      const panelId = Number(panel?.id);
      if (!Number.isInteger(panelId) || panelId <= 0) return;
      const panelName = panel?.name || "未命名基线任务";
      const link = node("a", "simc-benchmark-list-row");
      link.href = `/portal/simc-benchmarks/${encodeURIComponent(String(panel.id))}/`;
      link.setAttribute("role", "listitem");
      link.setAttribute("aria-label", `查看 ${panelName} 的模拟结果`);

      const copy = node("div", "simc-benchmark-list-copy");
      copy.append(
        node("span", "simc-benchmark-list-kicker", "公开模拟任务"),
        node("h2", "simc-benchmark-list-name", panelName),
      );
      if (panel?.description) {
        copy.appendChild(renderMarkdownDescription(panel.description, "simc-benchmark-list-description"));
      }

      const meta = node("div", "simc-benchmark-list-meta");
      const resultCount = Math.max(0, Number(panel?.result_count) || 0);
      const resultMetric = node("span", "simc-benchmark-list-metric");
      resultMetric.append(
        node("span", "simc-benchmark-list-metric-label", "数据量"),
        node("strong", "simc-benchmark-list-metric-value", numberFormat.format(resultCount)),
      );
      const updateMetric = node("span", "simc-benchmark-list-metric");
      updateMetric.append(
        node("span", "simc-benchmark-list-metric-label", "数据更新"),
        node("strong", "simc-benchmark-list-metric-value", formatResultUpdateTime(panel?.result_updated_at)),
      );
      meta.append(resultMetric, updateMetric);

      const action = node("span", "simc-benchmark-list-action");
      action.append(
        node("span", "", "查看结果"),
        node("span", "simc-benchmark-list-arrow", "→"),
      );
      link.append(copy, meta, action);
      list.appendChild(link);
    });
    root.replaceChildren(list);
  }

  async function loadBenchmarks() {
    const root = document.getElementById("simc-benchmark-root"); if (!root) return;
    const panelId = root.dataset.panelId; root.setAttribute("aria-busy", "true");
    try {
      root.replaceChildren();
      if (panelId) await loadPanel({ id: panelId }, root, { setPageHeading: true });
      else {
        const payload = await requestJson(LIST_URL);
        const panels = payload.status === "ready" && Array.isArray(payload.panels) ? payload.panels : [];
        if (!panels.length) root.appendChild(state("暂无公开的 Benchmark 面板", "empty"));
        else renderPanelList(root, panels);
      }
    } catch (_) { root.replaceChildren(state("Benchmark 列表加载失败，请稍后重试", "error")); }
    finally { root.setAttribute("aria-busy", "false"); }
  }

  document.addEventListener("DOMContentLoaded", loadBenchmarks);
})();
