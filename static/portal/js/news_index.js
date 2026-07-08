const NEWS_STATE = {
  q: "",
  source: "",
  excludeSource: "nga",
  page: 1,
  pageSize: 30,
  sources: [],
  meta: {},
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
  if (NEWS_STATE.q) params.set("q", NEWS_STATE.q);
  if (NEWS_STATE.source) {
    params.set("source", NEWS_STATE.source);
  } else if (NEWS_STATE.excludeSource) {
    params.set("exclude_source", NEWS_STATE.excludeSource);
  }
  return `/portal/api/news/?${params.toString()}`;
}

function syncUrl() {
  const params = new URLSearchParams();
  if (NEWS_STATE.q) params.set("q", NEWS_STATE.q);
  if (NEWS_STATE.source) {
    params.set("source", NEWS_STATE.source);
  } else if (NEWS_STATE.excludeSource) {
    params.set("exclude_source", NEWS_STATE.excludeSource);
  }
  if (NEWS_STATE.page > 1) params.set("page", String(NEWS_STATE.page));
  const next = params.toString() ? `/portal/news/?${params.toString()}` : "/portal/news/";
  window.history.replaceState({}, "", next);
}

function readUrlState() {
  const params = new URLSearchParams(window.location.search || "");
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
    el.innerHTML = `<div class="portal-news-empty">没有匹配的新闻资讯</div>`;
    return;
  }
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
      const mainUrl = articleUrl || sourceUrl || '#';
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
    const filters = [];
    if (NEWS_STATE.q) filters.push(`关键词：${NEWS_STATE.q}`);
    if (NEWS_STATE.source) {
      filters.push(`来源：${NEWS_STATE.source}`);
    } else if (NEWS_STATE.excludeSource) {
      filters.push(`已排除：${NEWS_STATE.excludeSource}`);
    }
    label.textContent = filters.length ? `${filters.join(" / ")}，共 ${total} 条` : `按发布时间倒序，共 ${total} 条`;
  }
  if (pageInfo) pageInfo.textContent = `${page} / ${totalPages || 1}`;
  if (prev) prev.disabled = !meta.has_previous;
  if (next) next.disabled = !meta.has_next;
}

async function loadNews() {
  const list = document.getElementById("news-list");
  if (list) list.innerHTML = `<div class="portal-news-empty">加载中...</div>`;
  try {
    const resp = await fetch(buildApiUrl(), { credentials: "same-origin" });
    const payload = await resp.json();
    if (!resp.ok || payload.status !== "success") throw new Error(payload.message || "load failed");
    NEWS_STATE.sources = Array.isArray(payload.sources) ? payload.sources : [];
    NEWS_STATE.meta = payload.meta || {};
    NEWS_STATE.page = Number(NEWS_STATE.meta.page || NEWS_STATE.page || 1);
    renderSources();
    renderNewsList(payload.data || []);
    renderMeta(NEWS_STATE.meta);
    syncUrl();
  } catch (err) {
    if (list) list.innerHTML = `<div class="portal-news-empty">新闻加载失败</div>`;
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

readUrlState();
bindEvents();
loadNews();
