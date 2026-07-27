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
    const response = await fetch(url, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
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
      const protocol = parsed.protocol;
      if (protocol === "https:" || protocol === "http:") return parsed.href;
    } catch (error) {
      return null;
    }
    return null;
  }

  function isBaseline(candidate) {
    return candidate && (candidate.type === "baseline" || candidate.key === "baseline");
  }

  function sortCandidates(candidates) {
    return candidates.slice().sort((left, right) => {
      const leftValue = validDps(left && left.dps);
      const rightValue = validDps(right && right.dps);
      const leftDps = leftValue === null ? -1 : leftValue;
      const rightDps = rightValue === null ? -1 : rightValue;
      return rightDps - leftDps;
    });
  }

  function comparisonText(candidate, candidates, highest) {
    const dps = validDps(candidate.dps);
    if (dps === null) return "无有效结果";
    const baseline = candidates.find(isBaseline);
    const baselineDps = baseline ? validDps(baseline.dps) : null;
    const highestText = highest > 0 ? `${((dps / highest) * 100).toFixed(1)}% of highest` : "—";
    if (baselineDps !== null && baselineDps > 0) {
      const delta = ((dps - baselineDps) / baselineDps) * 100;
      const sign = delta > 0 ? "+" : "";
      return `${sign}${delta.toFixed(1)}% vs baseline · ${highestText}`;
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
      icon.src = iconUrl;
      icon.alt = "";
      icon.loading = "lazy";
      icon.decoding = "async";
      icon.referrerPolicy = "no-referrer";
      icon.addEventListener("error", () => icon.remove(), { once: true });
      identity.appendChild(icon);
    }
    const copy = node("div", "simc-benchmark-candidate-copy");
    const name = node("div", "simc-benchmark-candidate-name", candidate.label || candidate.key || "候选方案");
    if (baseline) name.appendChild(node("span", "simc-benchmark-baseline-badge", "Baseline"));
    copy.append(name, node("div", "simc-benchmark-candidate-source", candidate.source_label || "—"));
    identity.appendChild(copy);

    const track = node("div", "simc-benchmark-bar-track");
    const bar = node("div", "simc-benchmark-bar");
    const dps = validDps(candidate.dps);
    const ratio = dps !== null && highest > 0 ? Math.max(0, Math.min(100, (dps / highest) * 100)) : 0;
    bar.style.width = `${ratio}%`;
    track.appendChild(bar);
    track.setAttribute("role", "meter");
    track.setAttribute("aria-valuemin", "0");
    track.setAttribute("aria-valuemax", "100");
    track.setAttribute("aria-valuenow", ratio.toFixed(1));
    track.setAttribute("aria-label", `${candidate.label || candidate.key || "候选方案"}，相对最高 DPS ${ratio.toFixed(1)}%`);

    const metrics = node("div", "simc-benchmark-candidate-metrics");
    metrics.append(
      node("div", "simc-benchmark-candidate-value", dps === null ? "—" : `${numberFormat.format(dps)} DPS`),
      node("div", "simc-benchmark-relative", comparisonText(candidate, candidates, highest))
    );
    grid.append(identity, track, metrics);
    row.appendChild(grid);
    return row;
  }

  function renderCase(caseData) {
    const card = node("section", "simc-benchmark-case");
    const labels = caseData && caseData.labels && typeof caseData.labels === "object" ? caseData.labels : {};
    const title = node("h4", "simc-benchmark-case-title");
    [labels.spec || "未知专精", labels.scenario || "未知场景", labels.profile || "未知配置"].forEach((label) => {
      title.appendChild(node("span", "simc-benchmark-axis", label));
    });
    card.appendChild(title);

    const candidates = sortCandidates(Array.isArray(caseData && caseData.candidates) ? caseData.candidates : []);
    const highest = candidates.reduce((maximum, candidate) => {
      const dps = validDps(candidate && candidate.dps);
      return dps === null ? maximum : Math.max(maximum, dps);
    }, 0);
    if (!candidates.length) {
      card.appendChild(state("此 case 暂无候选结果", "empty"));
      return card;
    }
    const chart = node("div", "simc-benchmark-chart");
    candidates.forEach((candidate) => chart.appendChild(renderCandidate(candidate || {}, candidates, highest)));
    card.appendChild(chart);
    return card;
  }

  function panelShell(panel) {
    const article = node("article", "simc-benchmark-panel");
    article.dataset.benchmarkSlug = String(panel.slug || "");
    const header = node("header", "simc-benchmark-panel-header");
    const copy = node("div", "simc-benchmark-panel-copy");
    copy.append(node("h3", "simc-benchmark-panel-title", panel.name || panel.slug || "Benchmark Panel"));
    if (panel.description) copy.appendChild(node("p", "simc-benchmark-panel-description", panel.description));
    header.append(copy, node("span", "simc-benchmark-status", panel.status === "ready" ? "已发布" : "未就绪"));
    const body = node("div", "simc-benchmark-panel-body");
    article.append(header, body);
    return { article, body, header };
  }

  function renderReadyPanel(shell, payload) {
    const execution = payload.execution && typeof payload.execution === "object" ? payload.execution : null;
    if (!execution || !Array.isArray(execution.cases)) {
      shell.body.replaceChildren(state("已发布数据格式不可用", "error"));
      return;
    }
    const frozenPanel = payload.panel && typeof payload.panel === "object" ? payload.panel : {};
    const title = shell.header.querySelector(".simc-benchmark-panel-title");
    const description = shell.header.querySelector(".simc-benchmark-panel-description");
    if (title && frozenPanel.name) title.textContent = String(frozenPanel.name);
    if (description && frozenPanel.description !== undefined) description.textContent = String(frozenPanel.description);

    if (!execution.cases.length) {
      shell.body.replaceChildren(state("此 Panel 暂无已发布 case", "empty"));
      return;
    }
    const meta = node("div", "simc-benchmark-meta", `${execution.total_cases} cases · ${execution.total_runs} candidates · ${execution.completed_at || ""}`);
    const filters = node("div", "simc-benchmark-filters");
    const cases = node("div", "simc-benchmark-cases");
    const dimensions = [
      ["spec_key", "spec", "专精"],
      ["scenario_key", "scenario", "场景"],
      ["profile_key", "profile", "Profile"],
    ];
    const selections = {};
    const firstCoordinates = execution.cases[0].coordinates || {};
    dimensions.forEach(([coordinateKey, labelKey, filterLabel]) => {
      const label = node("label", "simc-benchmark-filter");
      label.appendChild(node("span", "simc-benchmark-filter-label", filterLabel));
      const select = node("select", "simc-benchmark-filter-select");
      select.dataset.dimension = coordinateKey;
      const options = new Map();
      execution.cases.forEach((caseData) => {
        const key = caseData && caseData.coordinates ? String(caseData.coordinates[coordinateKey] || "") : "";
        const text = caseData && caseData.labels ? String(caseData.labels[labelKey] || key) : key;
        if (key && !options.has(key)) options.set(key, text);
      });
      options.forEach((text, key) => {
        const option = node("option", "", text);
        option.value = key;
        select.appendChild(option);
      });
      selections[coordinateKey] = select;
      selections[coordinateKey].value = firstCoordinates[coordinateKey] || "";
      label.appendChild(select);
      filters.appendChild(label);
    });
    const findSelectedCase = () => execution.cases.find((caseData) => dimensions.every(([coordinateKey]) => {
      const selected = selections[coordinateKey].value;
      const actual = caseData && caseData.coordinates ? String(caseData.coordinates[coordinateKey] || "") : "";
      return selected === actual;
    }));
    const syncSelections = (caseData) => dimensions.forEach(([coordinateKey]) => {
      selections[coordinateKey].value = String(caseData.coordinates[coordinateKey] || "");
    });
    const renderSelectedCase = (event) => {
      let selectedCase = findSelectedCase();
      if (!selectedCase && event && event.target) {
        const changedKey = event.target.dataset.dimension;
        selectedCase = execution.cases.find((caseData) => caseData.coordinates
          && String(caseData.coordinates[changedKey] || "") === event.target.value);
        if (selectedCase) syncSelections(selectedCase);
      }
      if (!selectedCase) {
        cases.replaceChildren(state("当前筛选条件下没有结果", "empty"));
        return;
      }
      cases.replaceChildren(renderCase(selectedCase));
    };
    filters.addEventListener("change", renderSelectedCase);
    renderSelectedCase();
    shell.body.replaceChildren(meta, filters, cases);
  }

  async function loadPanel(panel, root) {
    const shell = panelShell(panel);
    root.appendChild(shell.article);
    if (panel.status !== "ready") {
      shell.body.appendChild(state("尚无完整、已发布的聚合结果", "not-ready"));
      return;
    }
    shell.body.appendChild(state("正在加载已发布结果…", "loading"));
    try {
      const payload = await requestJson(`${LIST_URL}${encodeURIComponent(String(panel.slug || ""))}/`);
      if (payload.status === "not_ready") {
        shell.body.replaceChildren(state("尚无完整、已发布的聚合结果", "not-ready"));
      } else if (payload.status === "ready") {
        renderReadyPanel(shell, payload);
      } else {
        throw new Error("Unknown status");
      }
    } catch (error) {
      shell.body.replaceChildren(state("Benchmark 结果加载失败，请稍后重试", "error"));
    }
  }

  async function loadBenchmarks() {
    const root = document.getElementById("simc-benchmark-root");
    if (!root) return;
    root.setAttribute("aria-busy", "true");
    try {
      const payload = await requestJson(LIST_URL);
      if (payload.status !== "ready" || !Array.isArray(payload.panels)) throw new Error("Invalid list");
      const requested = new URLSearchParams(window.location.search).get("benchmark");
      const panels = requested
        ? payload.panels.filter((panel) => panel && String(panel.slug) === requested)
        : payload.panels;
      root.replaceChildren();
      if (!panels.length) {
        root.appendChild(state(requested ? "指定的 Benchmark Panel 尚未公开" : "暂无公开 Benchmark Panel", "empty"));
        return;
      }
      await Promise.all(panels.map((panel) => loadPanel(panel || {}, root)));
    } catch (error) {
      root.replaceChildren(state("Benchmark 列表加载失败，请稍后重试", "error"));
    } finally {
      root.setAttribute("aria-busy", "false");
    }
  }

  document.addEventListener("DOMContentLoaded", loadBenchmarks);
})();
