const NEWS_STATE = {
  q: "",
  source: "",
  excludeSource: "nga",
  page: 1,
  pageSize: 30,
  sources: [],
  meta: {},
  activeTab: "news",
  requestId: 0,
};

function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function sanitizeHref(raw) {
  const s = String(raw || "").trim();
  if (!s) return "";
  if (s.startsWith("/")) return s;
  try {
    const u = new URL(s);
    if (u.protocol === "http:" || u.protocol === "https:") return s;
  } catch (e) {}
  return "";
}

function icon(name, cls) {
  return `<svg class="${cls || "w-4 h-4"}" aria-hidden="true"><use href="/static/portal/icons/icons.svg#${name}"></use></svg>`;
}

function getDisplayTitle(item) {
  return String(item.title_cn || item.title || "未命名资讯").trim();
}

function getOriginalTitle(item) {
  const title = String(item.title || "").trim();
  const titleCn = String(item.title_cn || "").trim();
  if (!title || title === titleCn) return "";
  return title;
}

function buildApiUrl() {
  const params = new URLSearchParams();
  params.set("page", String(NEWS_STATE.page));
  params.set("page_size", String(NEWS_STATE.pageSize));

  if (NEWS_STATE.activeTab === "news") {
    if (NEWS_STATE.q) params.set("q", NEWS_STATE.q);
    if (NEWS_STATE.source) {
      params.set("source", NEWS_STATE.source);
    } else if (NEWS_STATE.excludeSource) {
      params.set("exclude_source", NEWS_STATE.excludeSource);
    }
    return `/portal/api/news/?${params.toString()}`;
  } else if (NEWS_STATE.activeTab === "build") {
    return `/portal/api/wow-skill-diffs/?${params.toString()}`;
  } else if (NEWS_STATE.activeTab === "hotfix") {
    return `/portal/api/hotfix-reports/?${params.toString()}`;
  }
}

function syncUrl() {
  const params = new URLSearchParams();
  if (NEWS_STATE.activeTab !== "news") params.set("tab", NEWS_STATE.activeTab);
  if (NEWS_STATE.activeTab === "news") {
    if (NEWS_STATE.q) params.set("q", NEWS_STATE.q);
    if (NEWS_STATE.source) {
      params.set("source", NEWS_STATE.source);
    } else if (NEWS_STATE.excludeSource) {
      params.set("exclude_source", NEWS_STATE.excludeSource);
    }
  }
  if (NEWS_STATE.page > 1) params.set("page", String(NEWS_STATE.page));
  const next = params.toString() ? `/portal/news/?${params.toString()}` : "/portal/news/";
  window.history.replaceState({}, "", next);
}

function readUrlState() {
  const params = new URLSearchParams(window.location.search || "");
  NEWS_STATE.activeTab = String(params.get("tab") || "news").trim();
  if (!["news", "build", "hotfix"].includes(NEWS_STATE.activeTab)) NEWS_STATE.activeTab = "news";
  NEWS_STATE.q = String(params.get("q") || "").trim();
  NEWS_STATE.source = String(params.get("source") || "").trim();
  const excl = String(params.get("exclude_source") || "").trim();
  if (excl) NEWS_STATE.excludeSource = excl;
  else if (!NEWS_STATE.source) NEWS_STATE.excludeSource = "nga";
  else NEWS_STATE.excludeSource = "";
  const page = Number(params.get("page") || 1);
  NEWS_STATE.page = Number.isFinite(page) && page > 0 ? Math.floor(page) : 1;
  const input = document.getElementById("news-search-input");
  if (input) input.value = NEWS_STATE.q;
  updateTabVisibility();
}

function renderSources() {
  const el = document.getElementById("news-source-list");
  if (!el) return;
  const total = NEWS_STATE.sources.reduce((sum, item) => sum + Number(item.count || 0), 0);
  const rows = [
    { key: "", label: "全部来源", count: total },
    ...NEWS_STATE.sources,
  ];
  el.innerHTML = rows
    .map((item) => {
      const isActive =
        String(item.key || "") === NEWS_STATE.source ||
        (item.key === "" && !NEWS_STATE.source && NEWS_STATE.excludeSource);
      const cls = isActive ? "portal-news-source-btn is-active" : "portal-news-source-btn";
      return `<button type="button" class="${cls}" data-source="${escapeHtml(item.key || "")}">
        <span>${escapeHtml(item.label || item.key || "其他来源")}</span>
        <span>${escapeHtml(item.count || 0)}</span>
      </button>`;
    })
    .join("");
  el.querySelectorAll("[data-source]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const key = btn.getAttribute("data-source") || "";
      NEWS_STATE.page = 1;
      if (key === "") {
        NEWS_STATE.source = "";
        NEWS_STATE.excludeSource = "nga";
      } else if (key === "nga") {
        NEWS_STATE.source = "nga";
        NEWS_STATE.excludeSource = "";
      } else {
        NEWS_STATE.source = key;
        NEWS_STATE.excludeSource = "";
      }
      loadNews();
    });
  });
}

function renderNewsList(items) {
  const el = document.getElementById("news-list");
  if (!el) return;
  if (!Array.isArray(items) || !items.length) {
    const emptyMsg = NEWS_STATE.activeTab === "news" ? "没有匹配的新闻资讯" :
                     NEWS_STATE.activeTab === "build" ? "没有匹配的 Build 报告" : "没有匹配的 Hotfix 报告";
    el.innerHTML = `<div class="portal-news-empty">${emptyMsg}</div>`;
    return;
  }

  if (NEWS_STATE.activeTab === "news") {
    el.innerHTML = items
      .map((item) => {
        const title = escapeHtml(getDisplayTitle(item));
        const originalTitle = escapeHtml(getOriginalTitle(item));
        const source = escapeHtml(item.source || "unknown");
        const category = escapeHtml(item.category || "");
        const author = escapeHtml(item.author || "");
        const time = escapeHtml(item.publish_time || "");
        const articleUrl = sanitizeHref(item.article_url || (item.id ? `/portal/article/${item.id}/` : ""));
        const sourceUrl = sanitizeHref(item.url || "");
        const mainUrl = (item.source === 'exwind' && sourceUrl) ? sourceUrl : (articleUrl || sourceUrl || '#');
        const targetAttrs = mainUrl.startsWith("/") || mainUrl === "#" ? "" : " target=\"_blank\" rel=\"noreferrer\"";
        const external = sourceUrl
          ? `<a href="${escapeHtml(sourceUrl)}" target="_blank" rel="noreferrer" class="portal-news-external" title="查看原文">${icon("icon-external", "w-3.5 h-3.5")}</a>`
          : "";
        const metaParts = [];
        if (source) metaParts.push(`<span>${icon("icon-globe", "w-3.5 h-3.5")} ${source}</span>`);
        if (category) metaParts.push(`<span>${category}</span>`);
        if (author) metaParts.push(`<span>${icon("icon-user", "w-3.5 h-3.5")} ${author}</span>`);
        if (time) metaParts.push(`<span>${icon("icon-clock", "w-3.5 h-3.5")} ${time}</span>`);
        return `<article class="portal-news-row">
          <div class="portal-news-row-main">
            <div class="portal-news-row-titleline">
              <a href="${escapeHtml(mainUrl)}"${targetAttrs} class="portal-news-row-title">${title}</a>
              ${external}
            </div>
            ${originalTitle ? `<a href="${escapeHtml(mainUrl)}"${targetAttrs} class="portal-news-row-subtitle">${originalTitle}</a>` : ""}
            <div class="portal-news-row-meta">${metaParts.join("")}</div>
          </div>
        </article>`;
      })
      .join("");
  } else {
    el.innerHTML = items
      .map((item) => {
        const title = escapeHtml(item.title || "未命名报告");
        const source = escapeHtml(item.source || "Wago");
        const time = escapeHtml(item.time || "");
        const url = sanitizeHref(item.url || "");
        const branch = escapeHtml(item.branch || "");
        const fromBuild = escapeHtml(item.from_build || "");
        const toBuild = escapeHtml(item.to_build || "");
        const fromPush = escapeHtml(item.from_push || "");
        const toPush = escapeHtml(item.to_push || "");
        const changeRange = NEWS_STATE.activeTab === "build"
          ? [fromBuild, toBuild].filter(Boolean).join(" → ")
          : [fromPush, toPush].filter(Boolean).join(" → ");
        const metaParts = [];
        if (source) metaParts.push(`<span>${icon("icon-globe", "w-3.5 h-3.5")} ${source}</span>`);
        if (branch) metaParts.push(`<span>${branch}</span>`);
        if (changeRange) metaParts.push(`<span>${changeRange}</span>`);
        if (time) metaParts.push(`<span>${icon("icon-clock", "w-3.5 h-3.5")} ${time}</span>`);
        return `<article class="portal-news-row">
          <div class="portal-news-row-main">
            <div class="portal-news-row-titleline">
              <a href="${escapeHtml(url)}" class="portal-news-row-title">${title}</a>
            </div>
            <div class="portal-news-row-meta">${metaParts.join("")}</div>
          </div>
        </article>`;
      })
      .join("");
  }
}

function renderMeta(meta) {
  const label = document.getElementById("news-result-meta");
  const pageInfo = document.getElementById("news-page-info");
  const prev = document.getElementById("news-prev-btn");
  const next = document.getElementById("news-next-btn");
  const total = Number(meta.total || 0);
  const page = Number(meta.page || 1);
  const totalPages = Number(meta.total_pages || 1);
  if (label) {
    if (NEWS_STATE.activeTab === "news") {
      const filters = [];
      if (NEWS_STATE.q) filters.push(`关键词：${NEWS_STATE.q}`);
      if (NEWS_STATE.source) {
        filters.push(`来源：${NEWS_STATE.source}`);
      } else if (NEWS_STATE.excludeSource) {
        filters.push(`已排除：${NEWS_STATE.excludeSource}`);
      }
      label.textContent = filters.length ? `${filters.join(" / ")}，共 ${total} 条` : `按发布时间倒序，共 ${total} 条`;
    } else if (NEWS_STATE.activeTab === "build") {
      label.textContent = `Build 挖掘报告，近 2 个月，共 ${total} 条`;
    } else {
      label.textContent = `Hotfix 报告，近 2 个月，共 ${total} 条`;
    }
  }
  if (pageInfo) pageInfo.textContent = `${page} / ${totalPages || 1}`;
  if (prev) prev.disabled = !meta.has_previous;
  if (next) next.disabled = !meta.has_next;
}

async function loadNews() {
  const requestId = ++NEWS_STATE.requestId;
  const requestedTab = NEWS_STATE.activeTab;
  const requestUrl = buildApiUrl();
  const list = document.getElementById("news-list");
  if (list) list.innerHTML = `<div class="portal-news-empty">加载中...</div>`;
  try {
    const resp = await fetch(requestUrl, { credentials: "same-origin" });
    const payload = await resp.json();
    if (requestId !== NEWS_STATE.requestId || requestedTab !== NEWS_STATE.activeTab) return;
    if (!resp.ok || payload.status !== "success") throw new Error(payload.message || "load failed");

    if (NEWS_STATE.activeTab === "news") {
      NEWS_STATE.sources = Array.isArray(payload.sources) ? payload.sources : [];
      NEWS_STATE.meta = payload.meta || {};
      NEWS_STATE.page = Number(NEWS_STATE.meta.page || NEWS_STATE.page || 1);
      renderSources();
    } else {
      NEWS_STATE.sources = [];
      NEWS_STATE.meta = payload.meta || {};
      NEWS_STATE.page = Number(NEWS_STATE.meta.page || NEWS_STATE.page || 1);
    }

    renderNewsList(payload.data || []);
    renderMeta(NEWS_STATE.meta);
    syncUrl();
  } catch (err) {
    if (requestId !== NEWS_STATE.requestId || requestedTab !== NEWS_STATE.activeTab) return;
    const errMsg = NEWS_STATE.activeTab === "news" ? "新闻加载失败" : "报告加载失败";
    if (list) list.innerHTML = `<div class="portal-news-empty">${errMsg}</div>`;
  }
}

function bindEvents() {
  const form = document.getElementById("news-search-form");
  const input = document.getElementById("news-search-input");
  const clear = document.getElementById("news-clear-btn");
  const prev = document.getElementById("news-prev-btn");
  const next = document.getElementById("news-next-btn");
  if (form) {
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      NEWS_STATE.q = String(input?.value || "").trim();
      NEWS_STATE.page = 1;
      loadNews();
    });
  }
  if (clear) {
    clear.addEventListener("click", () => {
      NEWS_STATE.q = "";
      NEWS_STATE.source = "";
      NEWS_STATE.excludeSource = "nga";
      NEWS_STATE.page = 1;
      if (input) input.value = "";
      loadNews();
    });
  }
  if (prev) {
    prev.addEventListener("click", () => {
      if (!NEWS_STATE.meta.has_previous) return;
      NEWS_STATE.page = Math.max(1, NEWS_STATE.page - 1);
      loadNews();
    });
  }
  if (next) {
    next.addEventListener("click", () => {
      if (!NEWS_STATE.meta.has_next) return;
      NEWS_STATE.page = NEWS_STATE.page + 1;
      loadNews();
    });
  }
}

function updateTabVisibility() {
  const sidebar = document.querySelector(".portal-news-sources");
  const searchForm = document.getElementById("news-search-form");
  const resultTitle = document.querySelector(".portal-news-result-title");
  const clearButton = document.getElementById("news-clear-btn");
  const layout = document.querySelector(".portal-news-layout");

  if (NEWS_STATE.activeTab === "news") {
    if (sidebar) sidebar.hidden = false;
    if (searchForm) searchForm.hidden = false;
    if (clearButton) clearButton.hidden = false;
    if (layout) layout.classList.remove("is-report-view");
    if (resultTitle) resultTitle.textContent = "全部资讯";
  } else {
    if (sidebar) sidebar.hidden = true;
    if (searchForm) searchForm.hidden = true;
    if (clearButton) clearButton.hidden = true;
    if (layout) layout.classList.add("is-report-view");
    if (resultTitle) {
      resultTitle.textContent = NEWS_STATE.activeTab === "build" ? "Build 挖掘报告" : "Hotfix 报告";
    }
  }

  document.querySelectorAll("[data-news-tab]").forEach((btn) => {
    const tab = btn.getAttribute("data-news-tab");
    const isActive = tab === NEWS_STATE.activeTab;
    btn.classList.toggle("is-active", isActive);
    btn.setAttribute("aria-selected", isActive ? "true" : "false");
  });
}

function bindTabEvents() {
  document.querySelectorAll("[data-news-tab]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const tab = btn.getAttribute("data-news-tab");
      if (tab === NEWS_STATE.activeTab) return;
      NEWS_STATE.activeTab = tab;
      NEWS_STATE.page = 1;
      updateTabVisibility();
      loadNews();
    });
  });
}

readUrlState();
bindEvents();
bindTabEvents();
loadNews();
