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

  function isBaseline(candidate) { return candidate && (candidate.type === "baseline" || candidate.key === "baseline"); }
  function sortCandidates(candidates) { return candidates.slice().sort((a, b) => (validDps(b?.dps) ?? -1) - (validDps(a?.dps) ?? -1)); }

  function comparisonText(candidate, candidates, highest) {
    const dps = validDps(candidate.dps);
    if (dps === null) return "无有效结果";
    const baseline = candidates.find(isBaseline);
    const baselineDps = baseline ? validDps(baseline.dps) : null;
    const highestText = highest > 0 ? `${((dps / highest) * 100).toFixed(1)}% of highest` : "—";
    if (baselineDps !== null && baselineDps > 0) {
      const delta = ((dps - baselineDps) / baselineDps) * 100;
      return `${delta > 0 ? "+" : ""}${delta.toFixed(1)}% vs baseline · ${highestText}`;
    }
    return highestText;
  }

  function renderCandidate(candidate, candidates, highest) {
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
    const ratio = dps !== null && highest > 0 ? Math.max(0, Math.min(100, (dps / highest) * 100)) : 0;
    const track = node("div", "simc-benchmark-bar-track");
    const bar = node("div", "simc-benchmark-bar");
    bar.style.width = `${ratio}%`; track.appendChild(bar);
    track.setAttribute("role", "meter"); track.setAttribute("aria-valuemin", "0"); track.setAttribute("aria-valuemax", "100"); track.setAttribute("aria-valuenow", ratio.toFixed(1));
    const metrics = node("div", "simc-benchmark-candidate-metrics");
    metrics.append(node("div", "simc-benchmark-candidate-value", dps === null ? "—" : `${numberFormat.format(dps)} DPS`), node("div", "simc-benchmark-relative", comparisonText(candidate, candidates, highest)));
    grid.append(identity, track, metrics); row.appendChild(grid); return row;
  }

  function renderCoordinate(coordinate) {
    const candidates = sortCandidates(Array.isArray(coordinate?.candidates) ? coordinate.candidates : []);
    const caseNode = node("section", "simc-benchmark-case");
    if (!candidates.length) { caseNode.appendChild(state("当前坐标暂无已完成候选结果", "empty")); return caseNode; }
    const highest = candidates.reduce((maximum, candidate) => Math.max(maximum, validDps(candidate?.dps) ?? 0), 0);
    const chart = node("div", "simc-benchmark-chart");
    candidates.forEach((candidate) => chart.appendChild(renderCandidate(candidate || {}, candidates, highest)));
    caseNode.appendChild(chart); return caseNode;
  }

  function renderResults(shell, payload) {
    const coordinates = Array.isArray(payload?.results?.coordinates) ? payload.results.coordinates : [];
    if (!coordinates.length) { shell.body.replaceChildren(state("暂无已完成模拟结果", "not-ready")); return; }
    const dimensions = [["spec_key", "spec", "专精"], ["scenario_key", "scenario", "场景"], ["profile_key", "profile", "Profile"]];
    const filters = node("div", "simc-benchmark-filters");
    const selected = {};
    dimensions.forEach(([key, labelKey, title]) => {
      const label = node("label", "simc-benchmark-filter"); label.appendChild(node("span", "simc-benchmark-filter-label", title));
      const select = node("select", "simc-benchmark-filter-select"); select.dataset.dimension = key;
      const options = new Map();
      coordinates.forEach((coordinate) => { const value = String(coordinate?.[key] || ""); if (value && !options.has(value)) options.set(value, String(coordinate?.labels?.[labelKey] || value)); });
      options.forEach((text, value) => { const option = node("option", "", text); option.value = value; select.appendChild(option); });
      select.value = String(coordinates[0]?.[key] || ""); selected[key] = select;
      label.appendChild(select); filters.appendChild(label);
    });
    const selectedResult = node("div", "simc-benchmark-cases");
    const render = (event) => {
      let coordinate = coordinates.find((item) => dimensions.every(([key]) => String(item?.[key] || "") === selected[key].value));
      if (!coordinate && event?.target) {
        coordinate = coordinates.find((item) => String(item?.[event.target.dataset.dimension] || "") === event.target.value);
        if (coordinate) dimensions.forEach(([key]) => { selected[key].value = String(coordinate[key] || ""); });
      }
      selectedResult.replaceChildren(coordinate ? renderCoordinate(coordinate) : state("当前筛选条件下没有结果", "empty"));
    };
    filters.addEventListener("change", render); render();
    shell.body.replaceChildren(node("div", "simc-benchmark-meta", `${coordinates.length} 个已完成模拟坐标`), filters, selectedResult);
  }

  function panelShell(panel) {
    const article = node("article", "simc-benchmark-panel"); article.dataset.benchmarkSlug = String(panel.slug || "");
    const header = node("header", "simc-benchmark-panel-header"); const copy = node("div", "simc-benchmark-panel-copy");
    copy.append(node("h3", "simc-benchmark-panel-title", panel.name || panel.slug || "Benchmark Panel"));
    if (panel.description) copy.appendChild(node("p", "simc-benchmark-panel-description", panel.description));
    header.append(copy, node("span", "simc-benchmark-status", "结果投影"));
    const body = node("div", "simc-benchmark-panel-body"); article.append(header, body); return { article, body };
  }

  async function loadPanel(panel, root) {
    const shell = panelShell(panel); root.appendChild(shell.article); shell.body.appendChild(state("正在加载模拟结果…", "loading"));
    try {
      const payload = await requestJson(`${LIST_URL}${encodeURIComponent(String(panel.slug || ""))}/`);
      if (payload.status === "ready") renderResults(shell, payload);
      else shell.body.replaceChildren(state("暂无已完成模拟结果", "not-ready"));
    } catch (_) { shell.body.replaceChildren(state("Benchmark 结果加载失败，请稍后重试", "error")); }
  }

  async function loadBenchmarks() {
    const root = document.getElementById("simc-benchmark-root"); if (!root) return;
    const requested = new URLSearchParams(window.location.search).get("benchmark"); root.setAttribute("aria-busy", "true");
    try {
      root.replaceChildren();
      if (requested) await loadPanel({ slug: requested }, root);
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
