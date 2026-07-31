(() => {
  "use strict";

  const LIST_URL = "/portal/api/simc-benchmarks/panels/";
  const numberFormat = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 });

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  }

  function state(message, kind) {
    const element = node("div", `simc-benchmark-state simc-benchmark-state--${kind || "empty"}`, message);
    element.setAttribute("role", kind === "error" ? "alert" : "status");
    return element;
  }

  async function requestJson(url) {
    const response = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
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
  function sortCandidates(candidates) { return candidates.slice().sort((a, b) => (validDps(b?.dps) ?? -1) - (validDps(a?.dps) ?? -1)); }

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

  function renderCandidate(candidate, candidates, scale) {
    const baseline = isBaseline(candidate);
    const row = node("div", `simc-benchmark-candidate${baseline ? " simc-benchmark-candidate--baseline" : ""}`);
    const grid = node("div", "simc-benchmark-candidate-grid");
    const identity = node("div", "simc-benchmark-candidate-identity");
    const iconUrl = safeIconUrl(candidate.icon_url);
    if (iconUrl) {
      const icon = node("img", "simc-benchmark-candidate-icon");
      icon.src = iconUrl; icon.alt = ""; icon.loading = "lazy"; icon.decoding = "async"; icon.referrerPolicy = "no-referrer";
      icon.addEventListener("error", () => icon.remove(), { once: true });
      identity.appendChild(icon);
    }
    const copy = node("div", "simc-benchmark-candidate-copy");
    const name = node("div", "simc-benchmark-candidate-name", candidate.label || candidate.key || "候选方案");
    if (baseline) name.appendChild(node("span", "simc-benchmark-baseline-badge", "Baseline"));
    copy.append(name, node("div", "simc-benchmark-candidate-source", candidate.source_label || "—"));
    identity.appendChild(copy);
    const dps = validDps(candidate.dps);
    const ratio = dps !== null && scale.range > 0
      ? Math.max(0, Math.min(100, ((dps - scale.lowest) / scale.range) * 100))
      : (dps !== null ? 100 : 0);
    const track = node("div", "simc-benchmark-bar-track");
    const bar = node("div", "simc-benchmark-bar");
    bar.style.width = `${ratio}%`; track.appendChild(bar);
    track.setAttribute("role", "meter"); track.setAttribute("aria-valuemin", "0"); track.setAttribute("aria-valuemax", "100"); track.setAttribute("aria-valuenow", ratio.toFixed(1));
    const metrics = node("div", "simc-benchmark-candidate-metrics");
    metrics.append(node("div", "simc-benchmark-candidate-value", dps === null ? "—" : `${numberFormat.format(dps)} DPS`), node("div", "simc-benchmark-relative", comparisonText(candidate, candidates, scale)));
    grid.append(identity, track, metrics); row.appendChild(grid); return row;
  }

  function renderCoordinate(coordinate) {
    const allCandidates = Array.isArray(coordinate?.candidates) ? coordinate.candidates : [];
    const baseline = allCandidates.find(isBaseline);
    const candidates = sortCandidates(allCandidates.filter((candidate) => !isBaseline(candidate)));
    const caseNode = node("section", "simc-benchmark-case");
    if (!candidates.length) { caseNode.appendChild(state("当前坐标暂无已完成候选结果", "empty")); return caseNode; }
    const values = candidates.map((candidate) => validDps(candidate?.dps)).filter((value) => value !== null);
    const lowest = values.length ? Math.min(...values) : 0;
    const highest = values.length ? Math.max(...values) : 0;
    const scale = { lowest, highest, range: highest - lowest };
    const info = node("div", "simc-benchmark-basic-info");
    [
      ["simc-benchmark-info-spec", "专精", coordinate?.labels?.spec || coordinate?.spec_key],
      ["simc-benchmark-info-profile", "Profile", coordinate?.labels?.profile || coordinate?.profile_key],
      ["simc-benchmark-info-scenario", "场景", coordinate?.labels?.scenario || coordinate?.scenario_key],
    ].forEach(([className, label, value]) => {
      const item = node("div", className);
      item.append(node("span", "simc-benchmark-info-label", label), node("strong", "simc-benchmark-info-value", value || "—"));
      info.appendChild(item);
    });
    const axis = node("div", "simc-benchmark-axis-labels");
    [0, 25, 50, 75, 100].forEach((value) => axis.appendChild(node("span", "simc-benchmark-axis-label", `${value}%`)));
    const chart = node("div", "simc-benchmark-chart");
    candidates.forEach((candidate) => chart.appendChild(renderCandidate(candidate || {}, [baseline, ...candidates].filter(Boolean), scale)));
    const range = node("div", "simc-benchmark-range-note", scale.range > 0
      ? `区间对比：${numberFormat.format(lowest)} DPS = 0%，${numberFormat.format(highest)} DPS = 100%`
      : "区间内结果相同");
    caseNode.append(info, range, axis, chart); return caseNode;
  }

  function renderResults(shell, payload) {
    const coordinates = Array.isArray(payload?.results?.coordinates) ? payload.results.coordinates : [];
    if (!coordinates.length) { shell.body.replaceChildren(state("暂无已完成模拟结果", "not-ready")); return; }
    const dimensions = [["spec_key", "spec", "专精"], ["profile_key", "profile", "Profile"], ["scenario_key", "scenario", "场景"]];
    const filters = node("div", "simc-benchmark-filters");
    const selected = {};
    dimensions.forEach(([key, labelKey, title]) => {
      const label = node("label", "simc-benchmark-filter"); label.appendChild(node("span", "simc-benchmark-filter-label", title));
      const select = node("select", "simc-benchmark-filter-select"); select.dataset.dimension = key;
      selected[key] = select; label.appendChild(select); filters.appendChild(label);
    });

    const availableCoordinates = (key) => coordinates.filter((coordinate) => {
      if (key !== "spec_key" && String(coordinate?.spec_key || "") !== selected.spec_key.value) return false;
      if (key === "scenario_key" && String(coordinate?.profile_key || "") !== selected.profile_key.value) return false;
      return true;
    });
    const syncFilterOptions = (key) => {
      const [, labelKey] = dimensions.find(([dimension]) => dimension === key);
      const select = selected[key]; const previous = select.value; const options = new Map();
      availableCoordinates(key).forEach((coordinate) => {
        const value = String(coordinate?.[key] || "");
        if (value && !options.has(value)) options.set(value, String(coordinate?.labels?.[labelKey] || value));
      });
      select.replaceChildren();
      options.forEach((label, value) => { const option = node("option", "", label); option.value = value; select.appendChild(option); });
      select.value = options.has(previous) ? previous : (options.keys().next().value || "");
    };
    const syncDependentFilters = () => {
      syncFilterOptions("profile_key");
      syncFilterOptions("scenario_key");
    };
    syncFilterOptions("spec_key"); syncDependentFilters();

    const selectedResult = node("div", "simc-benchmark-cases");
    const render = () => {
      const coordinate = coordinates.find((item) => dimensions.every(([key]) => String(item?.[key] || "") === selected[key].value));
      selectedResult.replaceChildren(coordinate ? renderCoordinate(coordinate) : state("当前筛选条件下没有结果", "empty"));
    };
    selected.spec_key.addEventListener("change", () => { syncDependentFilters(); render(); });
    selected.profile_key.addEventListener("change", () => { syncFilterOptions("scenario_key"); render(); });
    selected.scenario_key.addEventListener("change", render); render();
    shell.body.replaceChildren(node("div", "simc-benchmark-meta", `${coordinates.length} 个已完成模拟坐标`), filters, selectedResult);
  }

  function applyPanelHeading(panel) {
    const name = String(panel?.name || "模拟结果");
    const description = String(panel?.description || "");
    const title = document.getElementById("simc-benchmarks-title");
    const copy = document.getElementById("simc-benchmarks-description");
    if (title) title.textContent = name;
    if (copy) { copy.textContent = description; copy.hidden = !description; }
    document.title = `${name} · WowDaily.cn`;
  }

  function panelShell(panel) {
    const article = node("article", "simc-benchmark-panel"); article.dataset.benchmarkSlug = String(panel.slug || "");
    const header = node("header", "simc-benchmark-panel-header"); const copy = node("div", "simc-benchmark-panel-copy");
    copy.append(node("h3", "simc-benchmark-panel-title", panel.name || panel.slug || "Benchmark Panel"));
    if (panel.description) copy.appendChild(node("p", "simc-benchmark-panel-description", panel.description));
    header.append(copy, node("span", "simc-benchmark-status", "结果投影"));
    const body = node("div", "simc-benchmark-panel-body"); article.append(header, body); return { article, body };
  }

  async function loadPanel(panel, root, { setPageHeading = false } = {}) {
    const shell = panelShell(panel); root.appendChild(shell.article); shell.body.appendChild(state("正在加载模拟结果…", "loading"));
    try {
      const payload = await requestJson(`${LIST_URL}${encodeURIComponent(String(panel.slug || ""))}/`);
      if (payload.status === "ready") {
        if (setPageHeading) applyPanelHeading(payload.panel || panel);
        renderResults(shell, payload);
      } else shell.body.replaceChildren(state("暂无已完成模拟结果", "not-ready"));
    } catch (_) { shell.body.replaceChildren(state("Benchmark 结果加载失败，请稍后重试", "error")); }
  }

  async function loadBenchmarks() {
    const root = document.getElementById("simc-benchmark-root"); if (!root) return;
    const requested = new URLSearchParams(window.location.search).get("benchmark"); root.setAttribute("aria-busy", "true");
    try {
      root.replaceChildren();
      if (requested) await loadPanel({ slug: requested }, root, { setPageHeading: true });
      else {
        const payload = await requestJson(LIST_URL);
        const panels = payload.status === "ready" && Array.isArray(payload.panels) ? payload.panels : [];
        if (!panels.length) root.appendChild(state("暂无公开的 Benchmark 面板", "empty"));
        else await Promise.all(panels.map((panel) => loadPanel(panel || {}, root)));
      }
    } catch (_) { root.replaceChildren(state("Benchmark 列表加载失败，请稍后重试", "error")); }
    finally { root.setAttribute("aria-busy", "false"); }
  }

  document.addEventListener("DOMContentLoaded", loadBenchmarks);
})();
