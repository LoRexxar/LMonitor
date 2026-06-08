async function fetchJson(url) {
  const resp = await fetch(url, { credentials: "same-origin" });
  const data = await resp.json();
  return data;
}

function getToastRoot() {
  return document.getElementById("toast-root");
}

function showToast(message, type) {
  const root = getToastRoot();
  if (!root) return;
  const el = document.createElement("div");
  const level = String(type || "info");
  const border =
    level === "success"
      ? "border-green-500"
      : level === "error"
      ? "border-red-500"
      : level === "warning"
      ? "border-yellow-500"
      : "border-blue-500";
  el.className = `bg-white shadow-lg rounded-lg px-4 py-3 border-l-4 ${border} text-slate-800 text-sm transition-opacity duration-200`;
  el.textContent = String(message || "");
  root.appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    setTimeout(() => el.remove(), 220);
  }, 2600);
}

function escapeHtml(s) {
  if (s === null || s === undefined) return "";
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function sanitizeHref(raw) {
  const s = String(raw || "").trim();
  if (!s) return "";
  if (s === "-" || s === "#" || s.toLowerCase() === "javascript:void(0);" || s.toLowerCase().startsWith("javascript:")) return "";
  if (s.startsWith("/")) return s;
  try {
    const u = new URL(s);
    if (u.protocol === "http:" || u.protocol === "https:") return s;
  } catch (e) {}
  return "";
}

// --- NGA hover preview (Portal) ---
const NGA_PREVIEW_CACHE = new Map(); // articleId -> preview text
let NGA_TOOLTIP_EL = null;
let NGA_TOOLTIP_HIDE_TIMER = null;

function ensureNgaTooltip() {
  if (NGA_TOOLTIP_EL) return NGA_TOOLTIP_EL;
  const el = document.createElement("div");
  el.id = "nga-hover-tooltip";
  el.style.position = "fixed";
  el.style.zIndex = "9999";
  el.style.maxWidth = "520px";
  el.style.display = "none";
  el.className = "rounded-xl border border-slate-200 bg-white shadow-lg p-3 text-xs text-slate-700";
  el.style.whiteSpace = "pre-wrap";
  el.style.pointerEvents = "none";
  document.body.appendChild(el);
  NGA_TOOLTIP_EL = el;
  return el;
}

function showNgaTooltipAt(x, y, html) {
  const el = ensureNgaTooltip();
  if (NGA_TOOLTIP_HIDE_TIMER) {
    clearTimeout(NGA_TOOLTIP_HIDE_TIMER);
    NGA_TOOLTIP_HIDE_TIMER = null;
  }
  const pad = 12;
  const vw = window.innerWidth || 1200;
  const vh = window.innerHeight || 800;
  el.innerHTML = html || "";
  el.style.display = "block";
  // 先给一个默认位置，再根据尺寸修正
  el.style.left = Math.min(vw - 40, x + pad) + "px";
  el.style.top = Math.min(vh - 40, y + pad) + "px";
  const rect = el.getBoundingClientRect();
  let left = x + pad;
  let top = y + pad;
  if (left + rect.width > vw - 12) left = Math.max(12, vw - rect.width - 12);
  if (top + rect.height > vh - 12) top = Math.max(12, vh - rect.height - 12);
  el.style.left = left + "px";
  el.style.top = top + "px";
}

function hideNgaTooltipSoon() {
  const el = ensureNgaTooltip();
  if (NGA_TOOLTIP_HIDE_TIMER) clearTimeout(NGA_TOOLTIP_HIDE_TIMER);
  NGA_TOOLTIP_HIDE_TIMER = setTimeout(() => {
    el.style.display = "none";
  }, 120);
}

async function fetchNgaPreviewText(articleId) {
  const id = Number(articleId || 0);
  if (!id) return "";
  if (NGA_PREVIEW_CACHE.has(id)) return NGA_PREVIEW_CACHE.get(id) || "";
  try {
    const resp = await fetch(`/portal/api/article/${id}/`);
    if (!resp.ok) return "";
    const data = await resp.json();
    const item = data?.data || {};
    const raw = String(item.content || "").trim();
    const preview = raw.length > 900 ? raw.slice(0, 900).trim() + "..." : raw;
    NGA_PREVIEW_CACHE.set(id, preview);
    return preview;
  } catch (e) {
    return "";
  }
}

function bindNgaHoverTooltips(containerEl) {
  if (!containerEl) return;
  const items = containerEl.querySelectorAll("[data-nga-article-id]");
  items.forEach((el) => {
    if (el.dataset?.ngaTooltipBound === "1") return;
    el.dataset.ngaTooltipBound = "1";
    el.addEventListener("mouseenter", async (ev) => {
      const id = el.getAttribute("data-nga-article-id");
      const preview = await fetchNgaPreviewText(id);
      const safe = preview || "暂无预览内容（可能需要配置 NGA Cookie 才能抓取主楼）";
      const html = `<div class="font-semibold text-slate-900 mb-1">主楼预览</div><div class="text-slate-700">${escapeHtml(safe)}</div>`;
      showNgaTooltipAt(ev.clientX, ev.clientY, html);
    });
    el.addEventListener("mousemove", (ev) => {
      const tip = ensureNgaTooltip();
      if (tip.style.display !== "block") return;
      // 不重写内容，仅更新位置
      showNgaTooltipAt(ev.clientX, ev.clientY, tip.innerHTML);
    });
    el.addEventListener("mouseleave", () => {
      hideNgaTooltipSoon();
    });
  });
}

function getFaviconSrc(it) {
  const p = String(it?.icon_path || "").trim();
  if (p) return p;
  return "/static/portal/favicons/default.svg";
}

function svgIcon(id, cls) {
  const safeId = String(id || "").replaceAll(/[^a-z0-9\-_]/gi, "");
  const c = cls ? escapeHtml(cls) : "";
  return `<svg class="${c}" aria-hidden="true"><use href="/static/portal/icons/icons.svg#${safeId}"></use></svg>`;
}

function getSearchQuery() {
  return String(PORTAL_STATE.query || "").trim().toLowerCase();
}

function matchItem(item, q) {
  if (!q) return true;
  const parts = [
    item?.title,
    item?.source,
    item?.author,
    item?.tag,
    item?.time,
    item?.publish_time,
    item?.published_at,
    item?.start_at,
    item?.end_at,
    item?.status,
    item?.url,
    item?.source_url,
    item?.dungeon,
    item?.dungeon_cn,
    item?.tank,
    item?.healer,
    Array.isArray(item?.dps) ? item.dps.join(" ") : item?.dps_json,
  ]
    .filter(Boolean)
    .map((v) => String(v).toLowerCase());
  return parts.some((v) => v.includes(q));
}

function filterItems(items, q) {
  if (!q) return items || [];
  return (items || []).filter((it) => matchItem(it, q));
}

function renderSimpleList(containerId, items, opts) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const query = getSearchQuery();
  const filtered = filterItems(items, query);
  if (!items || items.length === 0) {
    el.innerHTML = `<div class="text-slate-500">暂无数据</div>`;
    return;
  }
  if (!filtered.length) {
    el.innerHTML = `<div class="text-slate-500">无匹配结果</div>`;
    return;
  }
  const limit = typeof opts?.limit === "number" ? opts.limit : 12;
  const asGrid = containerId === "nga-list";
  const showReplyBadge = opts?.showReplyBadge === true || containerId === "nga-list";
  const isArticleList = containerId === "blueposts-list" || containerId === "wowhead-list";
  const html = filtered
    .slice(0, limit)
    .map((it, idx) => {
      const title = escapeHtml(it.title || "");
      const titleCn = escapeHtml(it.title_cn || "");
      const displayTitle = titleCn || title;
      const rawUrl = it.url || it.source_url || "";
      const href = sanitizeHref(rawUrl);
      const url = escapeHtml(href);
      const source = escapeHtml(it.source || "");
      const author = escapeHtml(it.author || "");
      const time = escapeHtml((it.time || it.publish_time || it.published_at || "").replaceAll("\n", " ").trim());
      const replyCount = Number(it.reply_count || 0);
      const reply = showReplyBadge && replyCount > 0
        ? (() => {
            let badgeCls = "bg-slate-100 text-slate-700 border-slate-200";
            if (replyCount >= 500) badgeCls = "bg-rose-100 text-rose-800 border-rose-200";
            else if (replyCount >= 200) badgeCls = "bg-amber-100 text-amber-800 border-amber-200";
            else badgeCls = "bg-sky-100 text-sky-800 border-sky-200";
            return `<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-semibold border ${badgeCls}">${escapeHtml(replyCount)} 回复</span>`;
          })()
        : "";

      const parts = [];
      if (source) parts.push(`<span class="inline-flex items-center gap-1">${svgIcon("icon-globe", "w-3.5 h-3.5 text-slate-400")}<span>${source}</span></span>`);
      if (author) parts.push(`<span class="inline-flex items-center gap-1">${svgIcon("icon-user", "w-3.5 h-3.5 text-slate-400")}<span>${author}</span></span>`);
      if (time) parts.push(`<span class="inline-flex items-center gap-1">${svgIcon("icon-clock", "w-3.5 h-3.5 text-slate-400")}<span>${time}</span></span>`);
      if (reply) parts.push(reply);
      const meta = parts.join("");

      const divider = asGrid ? "border-b border-slate-100" : (idx === 0 ? "" : "border-t border-slate-100");

      const articleId = it.id;
      const articleLink = isArticleList && articleId ? `/portal/article/${articleId}/` : "";
      const externalLinkIcon = href ? `<a href="${url}" target="_blank" rel="noreferrer" class="inline-flex items-center text-slate-400 hover:text-indigo-600 ml-1" title="查看原文">${svgIcon("icon-globe", "w-3.5 h-3.5")}</a>` : "";
      const titleClampCls = isArticleList ? "portal-line-clamp-1" : "portal-line-clamp-2";

      let titleHtml;
      if (articleLink) {
        if (titleCn && title) {
          titleHtml = `<a class="block hover:text-indigo-700" href="${escapeHtml(articleLink)}">
            <div class="font-medium text-slate-900 ${titleClampCls}">${titleCn}${externalLinkIcon}</div>
            <div class="text-xs text-slate-500 mt-0.5 portal-line-clamp-1">${title}</div>
          </a>`;
        } else {
          titleHtml = `<a class="block text-slate-900 hover:text-indigo-700 font-medium ${titleClampCls}" href="${escapeHtml(articleLink)}">${title}${externalLinkIcon}</a>`;
        }
      } else if (url) {
        titleHtml = `<a class="block text-slate-900 hover:text-indigo-700 font-medium ${titleClampCls}" href="${url}" target="_blank" rel="noreferrer">${title}</a>`;
      } else {
        titleHtml = `<span class="block text-slate-900 font-medium ${titleClampCls}">${title}</span>`;
      }

      const ngaHoverAttrs =
        containerId === "nga-list" && articleId
          ? ` data-nga-article-id="${escapeHtml(articleId)}"`
          : "";
      return `<div class="py-2 ${divider}"${ngaHoverAttrs}>
        ${titleHtml}
        ${meta ? `<div class="mt-1 text-xs text-slate-500 flex flex-wrap items-center gap-x-2 gap-y-1">${meta}</div>` : ""}
      </div>`;
    })
    .join("");

  el.innerHTML = asGrid ? `<div class="grid grid-cols-1 md:grid-cols-2 gap-x-6">${html}</div>` : html;
  if (containerId === "nga-list") bindNgaHoverTooltips(el);
}

function renderSkeleton(containerId, lines = 8) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const blocks = [];
  for (let i = 0; i < lines; i++) {
    blocks.push(`<div class="portal-skeleton h-4 mb-3"></div>`);
  }
  el.innerHTML = `<div class="mt-2">${blocks.join("")}</div>`;
}

async function loadTools() {
  const topEl = document.getElementById("topbar-tools");
  const gridEl = document.getElementById("tools-nav");
  if (!topEl && !gridEl) return;
  try {
    const r = await fetchJson("/portal/api/tools/");
    const topbar = r?.data?.topbar || [];
    const items = r?.data?.items || [];
    if (topEl) {
      if (!topbar.length) {
        topEl.innerHTML = `<div class="text-xs text-slate-500">暂无入口</div>`;
      } else {
        topEl.innerHTML = topbar
          .slice(0, 24)
          .map((it) => {
            const name = escapeHtml(it.name || "");
            const href = sanitizeHref(it.url || "");
            const url = escapeHtml(href);
            const icon = escapeHtml(getFaviconSrc(it));
            const fallback = "/static/portal/favicons/default.svg";
            if (!url) {
              return `<span class="portal-pill inline-flex items-center gap-2 cursor-not-allowed opacity-70">
                <img class="w-4 h-4 rounded bg-white/80 border border-slate-200" src="${icon}" alt="" loading="lazy" onerror="this.src='${fallback}'" />
                <span>${name}</span>
              </span>`;
            }
            return `<a class="portal-pill inline-flex items-center gap-2" href="${url}" target="_blank" rel="noreferrer">
                <img class="w-4 h-4 rounded bg-white/80 border border-slate-200" src="${icon}" alt="" loading="lazy" onerror="this.src='${fallback}'" />
                <span>${name}</span>
              </a>`;
          })
          .join("");
      }
    }
    if (gridEl) {
      if (!items.length) {
        gridEl.innerHTML = `<div class="text-slate-500">暂无工具数据</div>`;
      } else {
        gridEl.innerHTML = items
          .slice(0, 120)
          .map((it) => {
            const name = escapeHtml(it.name || "");
            const href = sanitizeHref(it.url || "");
            const url = escapeHtml(href);
            const desc = escapeHtml(it.desc || "");
            const icon = escapeHtml(getFaviconSrc(it));
            const fallback = "/static/portal/favicons/default.svg";
            if (!url) {
              return `<div class="block p-3 rounded-xl border border-slate-200 bg-white opacity-70">
                <div class="flex items-center gap-2">
                  <img class="w-4 h-4 rounded bg-white/80 border border-slate-200" src="${icon}" alt="" loading="lazy" onerror="this.src='${fallback}'" />
                  <div class="font-medium">${name}</div>
                </div>
                ${desc ? `<div class="text-slate-500 text-xs mt-1">${desc}</div>` : ""}
              </div>`;
            }
            return `<a class="block p-3 rounded-xl border border-slate-200 bg-white hover:bg-slate-50 transition-colors" href="${url}" target="_blank" rel="noreferrer">
                <div class="flex items-center gap-2">
                  <img class="w-4 h-4 rounded bg-white/80 border border-slate-200" src="${icon}" alt="" loading="lazy" onerror="this.src='${fallback}'" />
                  <div class="font-medium">${name}</div>
                </div>
                ${desc ? `<div class="text-slate-500 text-xs mt-1">${desc}</div>` : ""}
              </a>`;
          })
          .join("");
      }
    }
  } catch (e) {
    if (topEl) topEl.innerHTML = `<div class="text-xs text-slate-500">加载失败</div>`;
    if (gridEl) gridEl.innerHTML = `<div class="text-slate-500">加载失败</div>`;
  }
}

const SECTION_MAP = {
  blueposts: { url: "/portal/api/blueposts/", listId: "blueposts-list" },
  exwind: { url: "/portal/api/exwind/latest/", listId: "exwind-list" },
  wowhead: { url: "/portal/api/wowhead/latest/", listId: "wowhead-list" },
  wow_skill_states: { url: "/portal/api/wow-skill-diff/states/", listId: "wow-skill-diff-states" },
  wow_skill_diffs: { url: "/portal/api/wow-skill-diffs/", listId: "wow-skill-diff-list" },
  nga: { url: "/portal/api/nga-hot/", listId: "nga-list" },
  events: { url: "/portal/api/events/", listId: "events-list" },
  videos: { url: "/portal/api/videos/", listId: "videos-list", tagsId: "videos-tags" },
  mplus_cutoffs: { url: "/portal/api/mplus/cutoff/", listId: "mplus-cutoffs" },
  mplus_rankings: { url: "/portal/api/mplus/rankings/", listId: "mplus-rankings" },
  peak_spec_rankings: { url: "/portal/api/peak/spec-rankings/", listId: "peak-spec-rankings" },
  mythicstats_dps: { url: "/portal/api/mythicstats/dps/", listId: "mythicstats-table" },
};

const PORTAL_STATE = {
  query: "",
  dataBySection: {},
  videoTags: [],
  activeVideoTag: "",
  activeDungeon: "",
  mplusCutoffsMeta: { season: "", updated_at: "" },
  activeMythicstatsDungeon: 0,
  activeMythicstatsPeriod: "",
  mythicstatsMeta: { dungeons: [], periods: [] },
  activeMythicstatsSeason: "",
  searchBound: false,
};

function classColor(slug) {
  const m = {
    "death-knight": "#C41F3B",
    "demon-hunter": "#A330C9",
    "druid": "#FF7D0A",
    "evoker": "#33937F",
    "hunter": "#ABD473",
    "mage": "#69CCF0",
    "monk": "#00FF96",
    "paladin": "#F58CBA",
    "priest": "#E5E7EB",
    "rogue": "#FFF569",
    "shaman": "#0070DE",
    "warlock": "#9482C9",
    "warrior": "#C79C6E",
  };
  return m[String(slug || "").toLowerCase()] || "#94a3b8";
}

function renderMplusControls(dungeons) {
  const el = document.getElementById("mplus-controls");
  if (!el) return;
  const list = Array.isArray(dungeons) ? dungeons : [];

  const active = PORTAL_STATE.activeDungeon || "";
  const options =
    `<option value="">全部副本</option>` +
    list.map((d) => `<option value="${escapeHtml(d.slug)}">${escapeHtml(d.name_cn || d.slug)}</option>`).join("");
  el.innerHTML = `<div class="flex items-center gap-2">
    <div class="text-xs text-slate-600">副本</div>
    <select id="mplus-dungeon-select" class="text-sm rounded-xl border border-slate-200 bg-white/80 px-3 py-1.5 focus:outline-none focus:ring-2 focus:ring-indigo-400">
      ${options}
    </select>
  </div>`;
  const sel = document.getElementById("mplus-dungeon-select");
  if (sel) {
    sel.value = active;
    sel.addEventListener("change", () => {
      PORTAL_STATE.activeDungeon = sel.value || "";
      loadSection("mplus_rankings");
    });
  }
}

function renderMplusCutoffs(containerId, payload) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const rawItems = payload?.items;
  const items = Array.isArray(rawItems) ? rawItems : [];
  const query = getSearchQuery();
  const filtered = filterItems(items, query);
  if (!items.length) {
    el.innerHTML = `<div class="text-slate-500">暂无数据</div>`;
    return;
  }
  if (!filtered.length) {
    el.innerHTML = `<div class="text-slate-500">无匹配结果</div>`;
    return;
  }

  const season = escapeHtml(payload?.season || "");
  const updatedAt = escapeHtml(payload?.updated_at || "");
  const metaParts = [];
  if (season) metaParts.push(`<span>赛季：${season}</span>`);
  if (updatedAt) metaParts.push(`<span>更新：${updatedAt}</span>`);
  const meta = metaParts.length ? `<div class="text-xs text-slate-500 mb-2 flex flex-wrap gap-x-3 gap-y-1">${metaParts.join("")}</div>` : "";

  const fmt = (v) => {
    const n = Number(v);
    return Number.isFinite(n) ? n.toFixed(2) : "--";
  };

  const fmtDiff = (cur, prev) => {
    const c = Number(cur);
    const p = Number(prev);
    if (!Number.isFinite(c) || !Number.isFinite(p)) return { text: "--", cls: "text-slate-400" };
    const d = c - p;
    if (Math.abs(d) < 0.005) return { text: "0.00", cls: "text-slate-400" };
    const sign = d > 0 ? "+" : "";
    const cls = d > 0 ? "text-emerald-600" : "text-red-500";
    return { text: `${sign}${d.toFixed(2)}`, cls };
  };

  const rows = filtered
    .slice(0, 6)
    .map((it) => {
      const region = escapeHtml(it.region_name || it.region || "");
      const href = sanitizeHref(it.source_url || it.url || "");
      const url = escapeHtml(href);
      const rCell = url
        ? `<a class="font-medium text-slate-900 hover:text-indigo-700" href="${url}" target="_blank" rel="noreferrer">${region}</a>`
        : `<span class="font-medium text-slate-900">${region}</span>`;
      const diff01 = fmtDiff(it.cutoff_0_1, it.cutoff_0_1_prev);
      const diff1 = fmtDiff(it.cutoff_1, it.cutoff_1_prev);
      return `<tr class="border-t border-slate-100">
        <td class="px-3 py-2">${rCell}</td>
        <td class="px-3 py-2 text-right tabular-nums">${escapeHtml(fmt(it.cutoff_0_1))}</td>
        <td class="px-3 py-2 text-right tabular-nums text-xs ${diff01.cls}">${escapeHtml(diff01.text)}</td>
        <td class="px-3 py-2 text-right tabular-nums">${escapeHtml(fmt(it.cutoff_1))}</td>
        <td class="px-3 py-2 text-right tabular-nums text-xs ${diff1.cls}">${escapeHtml(diff1.text)}</td>
      </tr>`;
    })
    .join("");

  el.innerHTML = `${meta}<div class="rounded-xl border border-slate-200 bg-white overflow-hidden">
    <table class="w-full text-sm">
      <thead class="bg-slate-50">
        <tr>
          <th class="px-3 py-2 text-left font-semibold text-slate-700">服务器</th>
          <th class="px-3 py-2 text-right font-semibold text-slate-700">0.1%</th>
          <th class="px-3 py-2 text-right font-semibold text-slate-700">较前</th>
          <th class="px-3 py-2 text-right font-semibold text-slate-700">1%</th>
          <th class="px-3 py-2 text-right font-semibold text-slate-700">较前</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  </div>`;
}

const PEAK_CLASS_CN = {
  "death-knight": "死亡骑士",
  "demon-hunter": "恶魔猎手",
  "druid": "德鲁伊",
  "evoker": "唤魔师",
  "hunter": "猎人",
  "mage": "法师",
  "monk": "武僧",
  "paladin": "圣骑士",
  "priest": "牧师",
  "rogue": "潜行者",
  "shaman": "萨满祭司",
  "warlock": "术士",
  "warrior": "战士",
};

const PEAK_SPEC_CN = {
  "blood": "鲜血",
  "frost": "冰霜",
  "unholy": "邪恶",
  "havoc": "浩劫",
  "vengeance": "复仇",
  "devourer": "噬灭",
  "balance": "平衡",
  "feral": "野性",
  "guardian": "熊德",
  "restoration": "奶德",
  "devastation": "湮灭",
  "preservation": "恩护",
  "augmentation": "增辉",
  "beast-mastery": "兽王",
  "marksmanship": "射击",
  "survival": "生存",
  "arcane": "奥术",
  "fire": "火焰",
  "brewmaster": "酒仙",
  "mistweaver": "织雾",
  "windwalker": "踏风",
  "holy": "奶骑",
  "protection": "防护",
  "retribution": "惩戒",
  "discipline": "戒律",
  "shadow": "暗影",
  "assassination": "奇袭",
  "outlaw": "狂徒",
  "subtlety": "敏锐",
  "elemental": "元素",
  "enhancement": "增强",
  "affliction": "痛苦",
  "demonology": "恶魔学识",
  "destruction": "毁灭",
  "arms": "武器",
  "fury": "狂怒",
};

function peakCnClass(slug, fallback) {
  const k = String(slug || "").toLowerCase();
  return PEAK_CLASS_CN[k] || fallback || slug || "";
}

function peakCnSpec(slug, fallback) {
  const k = String(slug || "").toLowerCase();
  return PEAK_SPEC_CN[k] || fallback || slug || "";
}

function renderPeakSpecControls(payload) {
  const el = document.getElementById("peak-spec-controls");
  if (!el) return;
  const season = payload?.season ? escapeHtml(payload.season) : "";
  el.innerHTML = season ? `<div class="text-xs text-slate-500">赛季：${season}</div>` : "";
}

function renderPeakSpecGrid(containerId, payload) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const items = Array.isArray(payload?.items) ? payload.items : [];
  if (!items.length) {
    el.innerHTML = `<div class="text-slate-500">暂无数据</div>`;
    return;
  }

  const q = String(getSearchQuery() || "").trim().toLowerCase();
  const beforeCards = [];
  const druidCards = [];
  const afterCards = [];
  let sawDruid = false;
  for (const spec of items) {
    const classSlug = spec?.class_slug || "";
    const className = spec?.class_name || classSlug;
    const specName = spec?.spec_name || spec?.spec_slug || "";
    const specSlug = spec?.spec_slug || "";
    const top = Array.isArray(spec?.items) ? spec.items : [];

    const titleColor = classColor(classSlug);
    const softBg = mythicstatsHexToRgba(titleColor, 0.1);
    const title = `${peakCnClass(classSlug, className)} · ${peakCnSpec(specSlug, specName)}`;
    const titleLower = String(title).toLowerCase();

    let filtered = top;
    if (q) {
      const matchTitle = titleLower.includes(q);
      filtered = matchTitle ? top : top.filter((x) => String(x?.name || "").toLowerCase().includes(q));
      if (!filtered.length) continue;
    }

    const top3 = filtered.slice(0, 3);
    while (top3.length < 3) top3.push(null);
    const rows = top3.map((x) => {
      const name = escapeHtml(x?.name || "-");
      const score = x?.score != null ? escapeHtml(Number(x.score).toFixed(1)) : "-";
      const realm = String(x?.realm_name || "").trim();
      const region = String(x?.rio_region_slug || "").trim().toUpperCase();
      const server = escapeHtml([realm, region].filter(Boolean).join(" "));
      const href = sanitizeHref(x?.profile_url || "");
      const dotBorder = classSlug === "priest" ? "border-slate-400" : "border-slate-200";
      const dot = `<span class="inline-block w-2 h-2 rounded-full border ${dotBorder}" style="background:${titleColor}"></span>`;
      const linkText = href
        ? `<a class="text-slate-900 hover:underline font-semibold" href="${escapeHtml(href)}" target="_blank" rel="noreferrer">${name}</a>`
        : `<span class="text-slate-900 font-semibold">${name}</span>`;
      const link = `<span class="inline-flex items-center gap-1.5">${dot}${linkText}</span>`;
      const left = server ? `<div class="text-xs text-slate-900 truncate">${link} <span class="text-slate-300">·</span> <span class="text-slate-500 font-medium">${server}</span></div>` : `<div class="text-xs text-slate-900 truncate">${link}</div>`;
      return `<div class="flex items-center justify-between gap-2 py-0.5">
        <div class="min-w-0">
          ${left}
        </div>
        <div class="text-xs text-slate-700 font-semibold tabular-nums">${score}</div>
      </div>`;
    });

    const cardHtml = `<div class="relative overflow-hidden rounded-xl border border-slate-200 bg-white/70 px-3 py-2">
      <div class="absolute inset-0 pointer-events-none" style="background:linear-gradient(90deg, ${softBg} 0%, rgba(255,255,255,0) 62%);"></div>
      <div class="absolute left-0 top-0 bottom-0 w-1" style="background:${titleColor}"></div>
      <div class="relative">
        <div class="flex items-center gap-2 pb-2 border-b border-slate-100">
          <div class="min-w-0">
            <div class="text-xs font-semibold text-slate-900 truncate">${escapeHtml(title)}</div>
          </div>
        </div>
        <div class="pt-1">${rows.join("")}</div>
      </div>
    </div>`;

    if (classSlug === "druid") {
      sawDruid = true;
      druidCards.push(cardHtml);
    } else if (!sawDruid) {
      beforeCards.push(cardHtml);
    } else {
      afterCards.push(cardHtml);
    }
  }

  const sections = [];
  const grid3 = (arr) => `<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">${arr.join("")}</div>`;
  const grid4 = (arr) => `<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">${arr.join("")}</div>`;
  if (beforeCards.length) sections.push(grid3(beforeCards));
  if (druidCards.length) sections.push(grid4(druidCards));
  if (afterCards.length) sections.push(grid3(afterCards));

  if (!sections.length) {
    el.innerHTML = `<div class="text-slate-500">无匹配结果</div>`;
    return;
  }

  el.innerHTML = `<div class="space-y-3">${sections.join("")}</div>`;
}

function renderMplusRuns(containerId, items) {
  const el = document.getElementById(containerId);
  if (!el) return;
  if (!items || !items.length) {
    el.innerHTML = `<div class="text-slate-500">暂无数据</div>`;
    return;
  }
  const q = getSearchQuery();
  const filtered = filterItems(items, q);

  const fmtTime = (sec) => {
    if (!sec && sec !== 0) return "--";
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return `${m}:${String(s).padStart(2, "0")}`;
  };

  const renderMember = (m) => {
    const name = escapeHtml(m?.name || "-");
    const color = classColor(m?.class_slug);
    const border = m?.class_slug === "priest" ? "border-slate-400" : "border-transparent";
    return `<span class="inline-flex items-center gap-1 rounded-md bg-white/70 border ${border} px-1.5 py-0.5">
      <span class="w-2 h-2 rounded-full border border-slate-200" style="background:${color}"></span>
      <span class="text-slate-800 text-xs font-medium">${name}</span>
    </span>`;
  };

  const rows = filtered.map((it) => {
    const dungeon = escapeHtml(it.dungeon_cn || it.dungeon || "");
    const level = it.level || 0;
    const time = fmtTime(it.time_seconds);
    const score = it.score ? Number(it.score).toFixed(1) : "--";
    const party = Array.isArray(it.party) ? it.party : [];
    const tank = party.find((p) => (p.role || "") === "tank") || {};
    const healer = party.find((p) => (p.role || "") === "healer") || {};
    const dpsList = party.filter((p) => (p.role || "") === "dps");
    const tankHtml = renderMember(tank);
    const healerHtml = renderMember(healer);
    const dpsHtml = dpsList.length
      ? dpsList.map(renderMember).join("")
      : (Array.isArray(it.dps) ? it.dps.map((x) => `<span class="text-xs text-slate-600">${escapeHtml(x)}</span>`).join(" / ") : "-");
    const runHref = sanitizeHref(it.run_url || "");
    const link = runHref
      ? `<a class="text-indigo-600 hover:text-indigo-800 text-xs font-medium" href="${escapeHtml(runHref)}" target="_blank" rel="noreferrer">Raider.IO</a>`
      : "";

    return `<tr class="border-t border-slate-100 hover:bg-slate-50/50">
      <td class="px-3 py-2.5">
        <div class="font-medium text-slate-900">${dungeon}</div>
      </td>
      <td class="px-3 py-2.5 text-center">
        <span class="inline-flex items-center justify-center px-2 py-0.5 rounded-md bg-indigo-50 text-indigo-700 font-semibold text-sm">+${level}</span>
      </td>
      <td class="px-3 py-2.5 text-center tabular-nums text-slate-700">${time}</td>
      <td class="px-3 py-2.5 text-center tabular-nums text-slate-700">${score}</td>
      <td class="px-3 py-2.5">
        <div class="flex flex-wrap items-center gap-1">
          <span class="text-slate-400 text-xs">T</span>${tankHtml}
          <span class="text-slate-400 text-xs ml-1">N</span>${healerHtml}
          <span class="text-slate-400 text-xs ml-1">D</span>${dpsHtml}
        </div>
      </td>
      <td class="px-3 py-2.5 text-right">${link}</td>
    </tr>`;
  });

  el.innerHTML = `
    <div class="rounded-xl border border-slate-200 bg-white overflow-hidden">
      <table class="w-full text-sm">
        <thead class="bg-slate-50">
          <tr>
            <th class="px-3 py-2 text-left font-semibold text-slate-700">副本</th>
            <th class="px-3 py-2 text-center font-semibold text-slate-700">层数</th>
            <th class="px-3 py-2 text-center font-semibold text-slate-700">时间</th>
            <th class="px-3 py-2 text-center font-semibold text-slate-700">分数</th>
            <th class="px-3 py-2 text-left font-semibold text-slate-700">队伍配置</th>
            <th class="px-3 py-2 text-right font-semibold text-slate-700">链接</th>
          </tr>
        </thead>
        <tbody>${rows.join("")}</tbody>
      </table>
    </div>
  `;
}

const MYTHICSTATS_DEFAULT_SEASON = "";
const MYTHICSTATS_STORAGE_KEY = "portal_mythicstats_season";

function renderMythicstatsControls(dungeons, periods, seasonsFromApi) {
  const el = document.getElementById("mythicstats-controls");
  if (!el) return;
  let dList = Array.isArray(dungeons) ? dungeons : [];
  if (!dList.length) dList = [{ id: 0, name: "All dungeons" }];
  const pList = Array.isArray(periods) ? periods : [];
  const payload = PORTAL_STATE.dataBySection.mythicstats_dps || {};
  const rawNote = String(payload.source_note || "").trim();
  const keyMin = Number(payload.key_min);
  const keyMax = Number(payload.key_max);
  let note = "";
  if (Number.isFinite(keyMin) && Number.isFinite(keyMax) && keyMin > 0 && keyMax > 0) note = `数据口径：Mythic+ ${keyMin}-${keyMax} 层`;
  else if (Number.isFinite(keyMin) && keyMin > 0 && !Number.isFinite(keyMax)) note = `数据口径：Mythic+ ${keyMin}+ 层`;
  else if (rawNote) note = `数据口径：${rawNote}`;
  const rawSrcUrl = String(payload.source_url || "https://mythicstats.com/dps").trim();
  const srcHref = sanitizeHref(rawSrcUrl) || "https://mythicstats.com/dps";
  const noteHtml = note ? `<div class="text-xs text-slate-500 mt-2">` + escapeHtml(note) + `（<a class="text-indigo-700 hover:text-indigo-900" href="` + escapeHtml(srcHref) + `" target="_blank" rel="noreferrer">MythicStats</a>）</div>` : "";
  const tipHtml = `<div class="mt-2 text-xs font-semibold text-red-700 bg-red-50 border border-red-200 rounded-lg px-3 py-2">提示：以下榜单随美服每周三更新，每周初期日志数量较少参考价值不大，请关注日志数量。</div>`;
  const seasons = Array.isArray(seasonsFromApi) ? seasonsFromApi : (Array.isArray(payload.seasons) ? payload.seasons : []);
  const currentSeason = String(PORTAL_STATE.activeMythicstatsSeason || MYTHICSTATS_DEFAULT_SEASON);
  const allSeasons = currentSeason && !seasons.includes(currentSeason) ? [currentSeason, ...seasons] : seasons;
  const seasonOptions =
    `<option value="">当前赛季</option>` +
    allSeasons
      .map((s) => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`)
      .join("");
  const dungeonOptions = dList
    .map((d) => `<option value="${escapeHtml(d.id)}">${escapeHtml(d.name || d.id)}</option>`)
    .join("");
  const periodOptions = pList
    .map((p) => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.label || p.id)}</option>`)
    .join("");
  el.innerHTML = `<div class="flex items-center gap-2">
    <div class="text-xs text-slate-600">赛季</div>
    <select id="mythicstats-season-select" class="text-sm rounded-xl border border-slate-200 bg-white/80 px-3 py-1.5 focus:outline-none focus:ring-2 focus:ring-indigo-400">
      ${seasonOptions}
    </select>
  </div>
  <div class="flex items-center gap-2">
    <div class="text-xs text-slate-600">副本</div>
    <select id="mythicstats-dungeon-select" class="text-sm rounded-xl border border-slate-200 bg-white/80 px-3 py-1.5 focus:outline-none focus:ring-2 focus:ring-indigo-400">
      ${dungeonOptions}
    </select>
  </div>
  <div class="flex items-center gap-2">
    <div class="text-xs text-slate-600">周</div>
    <select id="mythicstats-period-select" class="text-sm rounded-xl border border-slate-200 bg-white/80 px-3 py-1.5 focus:outline-none focus:ring-2 focus:ring-indigo-400">
      ${periodOptions}
    </select>
  </div>${noteHtml}${tipHtml}`;
  const seasonSel = document.getElementById("mythicstats-season-select");
  if (seasonSel) {
    const cur = String(PORTAL_STATE.activeMythicstatsSeason || MYTHICSTATS_DEFAULT_SEASON);
    seasonSel.value = cur;
    seasonSel.addEventListener("change", () => {
      PORTAL_STATE.activeMythicstatsSeason = seasonSel.value || MYTHICSTATS_DEFAULT_SEASON;
      try {
        localStorage.setItem(MYTHICSTATS_STORAGE_KEY, PORTAL_STATE.activeMythicstatsSeason);
      } catch (e) {}
      PORTAL_STATE.activeMythicstatsPeriod = "";
      loadSection("mythicstats_dps");
    });
  }
  const dungeonSel = document.getElementById("mythicstats-dungeon-select");
  if (dungeonSel) {
    dungeonSel.value = String(PORTAL_STATE.activeMythicstatsDungeon || 0);
    dungeonSel.addEventListener("change", () => {
      const v = Number(dungeonSel.value || 0);
      PORTAL_STATE.activeMythicstatsDungeon = Number.isFinite(v) ? v : 0;
      PORTAL_STATE.activeMythicstatsPeriod = "";
      loadSection("mythicstats_dps");
    });
  }
  const periodSel = document.getElementById("mythicstats-period-select");
  if (periodSel) {
    periodSel.value = String(PORTAL_STATE.activeMythicstatsPeriod || "");
    periodSel.addEventListener("change", () => {
      PORTAL_STATE.activeMythicstatsPeriod = periodSel.value || "";
      loadSection("mythicstats_dps");
    });
  }
}

const MYTHICSTATS_SPEC_CN = {
  "unholy-death-knight": "邪恶",
  "frost-death-knight": "冰霜",
  "blood-death-knight": "鲜血",
  "demonology-warlock": "恶魔",
  "affliction-warlock": "痛苦",
  "destruction-warlock": "毁灭",
  "devourer-demon-hunter": "噬灭",
  "havoc-demon-hunter": "浩劫",
  "vengeance-demon-hunter": "复仇",
  "retribution-paladin": "惩戒",
  "protection-paladin": "防骑",
  "holy-paladin": "奶骑",
  "arms-warrior": "武器",
  "fury-warrior": "狂怒",
  "protection-warrior": "防战",
  "outlaw-rogue": "狂徒",
  "subtlety-rogue": "敏锐",
  "assassination-rogue": "奇袭",
  "feral-druid": "野性",
  "balance-druid": "平衡",
  "guardian-druid": "熊德",
  "restoration-druid": "奶德",
  "survival-hunter": "生存",
  "beast-mastery-hunter": "兽王",
  "marksmanship-hunter": "射击",
  "enhancement-shaman": "增强",
  "elemental-shaman": "元素",
  "restoration-shaman": "恢复",
  "augmentation-evoker": "增辉",
  "devastation-evoker": "湮灭",
  "preservation-evoker": "恩护",
  "windwalker-monk": "踏风",
  "brewmaster-monk": "酒仙",
  "mistweaver-monk": "织雾",
  "shadow-priest": "暗影",
  "discipline-priest": "戒律",
  "holy-priest": "神牧",
  "arcane-mage": "奥术",
  "fire-mage": "火焰",
  "frost-mage": "冰霜",
};

function getMythicstatsSpecDisplay(it) {
  const slug = String(it?.spec_slug || "").trim();
  if (slug && MYTHICSTATS_SPEC_CN[slug]) return MYTHICSTATS_SPEC_CN[slug];
  const name = String(it?.spec_name || "").trim();
  return name || slug;
}

const MYTHICSTATS_CLASS_COLOR = {
  "death-knight": "#C41F3B",
  "demon-hunter": "#A330C9",
  druid: "#FF7D0A",
  evoker: "#33937F",
  hunter: "#ABD473",
  mage: "#69CCF0",
  monk: "#00FF96",
  paladin: "#F58CBA",
  priest: "#E5E7EB",
  rogue: "#FFF569",
  shaman: "#0070DE",
  warlock: "#9482C9",
  warrior: "#C79C6E",
};

function mythicstatsHexToRgba(hex, alpha) {
  const h = String(hex || "").replace("#", "").trim();
  if (h.length !== 6) return `rgba(0,0,0,${alpha})`;
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

function getMythicstatsClassFromSlug(slug) {
  const s = String(slug || "").trim();
  if (!s) return "";
  if (s.endsWith("death-knight")) return "death-knight";
  if (s.endsWith("demon-hunter")) return "demon-hunter";
  const parts = s.split("-").filter(Boolean);
  return parts.length ? parts[parts.length - 1] : "";
}

function getMythicstatsColor(it) {
  const slug = String(it?.spec_slug || "").trim();
  const cls = getMythicstatsClassFromSlug(slug);
  return MYTHICSTATS_CLASS_COLOR[cls] || "#94A3B8";
}

function renderMythicstatsTierBadge(tierRaw) {
  const t = String(tierRaw || "").trim().toUpperCase();
  const styles = {
    S: "bg-emerald-100 text-emerald-800 border-emerald-200",
    A: "bg-sky-100 text-sky-800 border-sky-200",
    B: "bg-indigo-100 text-indigo-800 border-indigo-200",
    C: "bg-amber-100 text-amber-800 border-amber-200",
    D: "bg-orange-100 text-orange-800 border-orange-200",
    F: "bg-rose-100 text-rose-800 border-rose-200",
  };
  const cls = styles[t] || "bg-slate-100 text-slate-700 border-slate-200";
  const label = t || "-";
  return `<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-semibold border ${cls}">${escapeHtml(label)}</span>`;
}

function renderMythicstatsTable(role, items) {
  const q = getSearchQuery();
  const filtered = q
    ? (items || []).filter((x) => {
        const en = String(x.spec_name || "").toLowerCase();
        const cn = String(getMythicstatsSpecDisplay(x) || "").toLowerCase();
        return en.includes(q) || cn.includes(q);
      })
    : (items || []);
  if (!filtered.length) {
    return `<div class="text-slate-500">${q ? "无匹配结果" : "暂无数据"}</div>`;
  }
  const maxTop = Math.max(1, ...filtered.map((x) => (Number.isFinite(Number(x.top_value)) ? Number(x.top_value) : 0)));
  const rows = filtered.slice(0, 60).map((it) => {
    const color = getMythicstatsColor(it);
    const cls = getMythicstatsClassFromSlug(String(it?.spec_slug || "").trim());
    const isPriest = cls === "priest";
    const rank = escapeHtml(it.rank);
    const diffRaw = String(it.diff_raw || "").trim();
    const diffVal = Number(it.diff_value);
    let diffCls = "text-slate-500";
    if (Number.isFinite(diffVal) && diffVal > 0) diffCls = "text-emerald-700";
    else if (Number.isFinite(diffVal) && diffVal < 0) diffCls = "text-rose-700";

    const tier = String(it.tier || "").trim().toUpperCase();
    const tierBadge = renderMythicstatsTierBadge(tier);
    const runs = escapeHtml(it.runs || "");
    const name = escapeHtml(getMythicstatsSpecDisplay(it));
    let specUrl = String(it?.spec_url || "").trim();
    if (specUrl && specUrl.startsWith("/")) specUrl = `https://mythicstats.com${specUrl}`;
    if (specUrl && !/^https?:\/\//i.test(specUrl)) specUrl = `https://mythicstats.com/${specUrl.replace(/^\/+/, "")}`;
    const url = escapeHtml(specUrl || "https://mythicstats.com/dps");

    const avgVal = Number.isFinite(Number(it.avg_value)) ? Number(it.avg_value) : 0;
    const topVal = Number.isFinite(Number(it.top_value)) ? Number(it.top_value) : 0;
    const avg = escapeHtml(it.avg || "");
    const top = escapeHtml(it.top || "");
    const avgPct = Math.max(0, Math.min(100, (avgVal / maxTop) * 100));
    const topPct = Math.max(0, Math.min(100, (topVal / maxTop) * 100));

    const topBg = isPriest
      ? "repeating-linear-gradient(135deg, rgba(71,85,105,0.14) 0 6px, rgba(255,255,255,0.32) 6px 12px)"
      : mythicstatsHexToRgba(color, 0.22);
    const avgBg = isPriest
      ? "repeating-linear-gradient(135deg, rgba(71,85,105,0.24) 0 6px, rgba(255,255,255,0.96) 6px 12px)"
      : mythicstatsHexToRgba(color, 0.92);
    const fillShadow = "";
    const bar = `<div class="relative h-3 w-full rounded bg-slate-200/70 overflow-hidden shadow-inner border border-slate-300/70">
      <div class="absolute inset-y-0 left-0" style="width:${topPct.toFixed(1)}%;background:${topBg};${fillShadow}"></div>
      <div class="absolute inset-y-0 left-0" style="width:${avgPct.toFixed(1)}%;background:${avgBg};${fillShadow}"></div>
    </div>`;

    const specAccentBg = mythicstatsHexToRgba(color, isPriest ? 0.2 : 0.14);
    const specCell = `<div class="relative overflow-hidden rounded-md px-2 py-1" style="background:linear-gradient(90deg, ${specAccentBg} 0%, rgba(255,255,255,0) 68%);">
      <div class="absolute left-0 top-0 bottom-0 w-1" style="background:${escapeHtml(color)}"></div>
      <div class="relative">
        <a class="font-semibold truncate mythicstats-spec-link block" style="color:#0f172a" href="${url}" target="_blank" rel="noreferrer">${name}</a>
      </div>
    </div>`;

    return `<div class="py-1.5">
      <div class="flex items-center gap-3">
        <div class="w-8 text-xs font-semibold text-slate-500">${rank}</div>
        <div class="w-[372px] min-w-[372px] grid grid-cols-[72px_36px_44px_56px_56px_44px] items-center gap-1">
          ${specCell}
          <div class="text-right">${tierBadge}</div>
          <div class="text-right text-[11px] ${diffCls} font-semibold">${escapeHtml(diffRaw || "0")}</div>
          <div class="text-right text-[11px] font-semibold text-slate-700">${avg}</div>
          <div class="text-right text-[11px] font-semibold text-slate-500">${top}</div>
          <div class="text-right text-[11px] text-slate-500">${runs}</div>
        </div>
        <div class="flex-1 min-w-0">${bar}</div>
      </div>
    </div>`;
  });
  const header = `<div class="py-1 text-xs text-slate-500 font-semibold">
    <div class="flex items-center gap-3">
      <div class="w-8 text-right">#</div>
      <div class="w-[372px] min-w-[372px] grid grid-cols-[72px_36px_44px_56px_56px_44px] items-center gap-1">
        <div>专精</div>
        <div class="text-right">Tier</div>
        <div class="text-right">Diff</div>
        <div class="text-right">Avg</div>
        <div class="text-right">Top</div>
        <div class="text-right">Runs</div>
      </div>
      <div class="flex-1 min-w-0 text-right">对比</div>
    </div>
  </div>`;
  return `<div>${header}<div class="divide-y divide-slate-100">${rows.join("")}</div></div>`;
}

function renderMythicstatsTables() {
  const el = document.getElementById("mythicstats-table");
  if (!el) return;
  const payload = PORTAL_STATE.dataBySection.mythicstats_dps || {};
  const roles = payload.roles || {};
  const damage = Array.isArray(roles.damage) ? roles.damage : [];
  const tank = Array.isArray(roles.tank) ? roles.tank : [];
  const healer = Array.isArray(roles.healer) ? roles.healer : [];
  const items = []
    .concat(damage.map((x) => ({ ...x, title: x.spec_name || "", tag: "DPS", source: "mythicstats" })))
    .concat(tank.map((x) => ({ ...x, title: x.spec_name || "", tag: "坦克", source: "mythicstats" })))
    .concat(healer.map((x) => ({ ...x, title: x.spec_name || "", tag: "治疗", source: "mythicstats" })));
  PORTAL_STATE.dataBySection.mythicstats_dps_items = items;

  el.innerHTML = `
    <div class="space-y-6">
      <div>
        <div class="text-xs font-semibold text-slate-700">DPS</div>
        <div class="mt-2">${renderMythicstatsTable("damage", damage)}</div>
      </div>
      <div>
        <div class="text-xs font-semibold text-slate-700">坦克</div>
        <div class="mt-2">${renderMythicstatsTable("tank", tank)}</div>
      </div>
      <div>
        <div class="text-xs font-semibold text-slate-700">治疗</div>
        <div class="mt-2">${renderMythicstatsTable("healer", healer)}</div>
      </div>
    </div>
  `;
}

function renderVideos(payload) {
  const tagsEl = document.getElementById(SECTION_MAP.videos.tagsId);
  const listEl = document.getElementById(SECTION_MAP.videos.listId);
  if (!listEl) return;
  const tags = payload?.tags || [];
  const items = payload?.items || [];
  PORTAL_STATE.dataBySection.videos = items;
  PORTAL_STATE.videoTags = tags;

  const active = PORTAL_STATE.activeVideoTag || "";
  if (!items || !items.length) {
    if (tagsEl) tagsEl.innerHTML = "";
    listEl.innerHTML = `<div class="text-slate-500">暂无近2天视频</div>`;
    return;
  }
  if (tagsEl) {
    const allBtn = `<button data-video-tag="" class="px-3 py-1 rounded-full text-xs border ${active ? "border-slate-200 bg-white text-slate-700" : "border-slate-900 bg-slate-900 text-white"}">全部</button>`;
    const btns = tags
      .map((t) => {
        const on = active === t;
        return `<button data-video-tag="${escapeHtml(t)}" class="px-3 py-1 rounded-full text-xs border ${on ? "border-slate-900 bg-slate-900 text-white" : "border-slate-200 bg-white text-slate-700 hover:bg-slate-50"}">${escapeHtml(t)}</button>`;
      })
      .join("");
    tagsEl.innerHTML = allBtn + btns;
    tagsEl.querySelectorAll("[data-video-tag]").forEach((b) => {
      b.addEventListener("click", async () => {
        const tag = b.getAttribute("data-video-tag") || "";
        PORTAL_STATE.activeVideoTag = tag;
        await loadSection("videos");
      });
    });
  }

  const q = getSearchQuery();
  const filteredByTag = active ? items.filter((x) => (x.tag || "") === active) : items;
  const filtered = filterItems(filteredByTag, q);
  listEl.innerHTML = filtered
    .slice(0, 12)
    .map((it, idx) => {
      const title = escapeHtml(it.title || "");
      const rawUrl = it.url || "";
      const urlHref = sanitizeHref(rawUrl);
      const url = escapeHtml(urlHref);
      const coverHref = sanitizeHref(it.cover_url || it.cover || "");
      const cover = escapeHtml(coverHref);
      const author = escapeHtml(it.author || "");
      const authorHref = sanitizeHref(it.author_url || "");
      const authorUrl = escapeHtml(authorHref);
      const time = escapeHtml((it.published_at || "").replaceAll("\n", " ").trim());
      const tag = escapeHtml(it.tag || "");
      const divider = idx === 0 ? "" : "border-t border-slate-100";
      const coverBox = cover
        ? `<img src="${cover}" alt="" class="w-full h-full object-cover" loading="lazy" />`
        : `<div class="w-full h-full portal-skeleton"></div>`;
      const coverHtml = url
        ? `<a class="shrink-0 w-20 h-11 rounded-lg overflow-hidden border border-slate-200 bg-slate-100" href="${url}" target="_blank" rel="noreferrer">${coverBox}</a>`
        : `<div class="shrink-0 w-20 h-11 rounded-lg overflow-hidden border border-slate-200 bg-slate-100">${coverBox}</div>`;
      return `<div class="py-2 ${divider}">
        <div class="flex items-start gap-3">
          ${coverHtml}
          <div class="min-w-0 flex-1">
            ${
              url
                ? `<a class="block text-slate-900 hover:text-indigo-700 font-medium portal-line-clamp-2" href="${url}" target="_blank" rel="noreferrer">${title}</a>`
                : `<span class="block text-slate-900 font-medium portal-line-clamp-2">${title}</span>`
            }
            <div class="mt-1 text-xs text-slate-500">
              ${
                author && authorUrl
                  ? `UP：<a class="text-indigo-700 hover:text-indigo-900" href="${authorUrl}" target="_blank" rel="noreferrer">${author}</a>`
                  : (author ? `UP：${author}` : "")
              }
              ${author && time ? " · " : ""}
              ${time}
              ${tag ? ` · ${tag}` : ""}
            </div>
          </div>
        </div>
      </div>`;
    })
    .join("");
}

function renderEvents(items) {
  const el = document.getElementById(SECTION_MAP.events.listId);
  if (!el) return;
  const q = getSearchQuery();
  const filtered = filterItems(items || [], q);
  if (!filtered.length) {
    el.innerHTML = `<div class="text-slate-500">暂无活动数据</div>`;
    return;
  }
  el.innerHTML = filtered
    .slice(0, 12)
    .map((it, idx) => {
      const title = escapeHtml(it.title || "");
      const url = escapeHtml(it.url || "#");
      const start = escapeHtml(it.start_at || "");
      const end = escapeHtml(it.end_at || "");
      const rawStatus = String(it.status || "").trim();
      const status = escapeHtml(rawStatus);
      const range = end ? `${start} - ${end}` : start;
      let badgeCls = "bg-slate-100 text-slate-700 border-slate-200";
      if (rawStatus.includes("进行中")) badgeCls = "bg-emerald-100 text-emerald-800 border-emerald-200";
      else if (rawStatus.includes("即将")) badgeCls = "bg-sky-100 text-sky-800 border-sky-200";
      else if (rawStatus.includes("已结束")) badgeCls = "bg-slate-100 text-slate-600 border-slate-200";
      const badge = status ? `<span class="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-medium border ${badgeCls}">${status}</span>` : "";
      const divider = idx === 0 ? "" : "border-t border-slate-100";
      return `<div class="py-2 ${divider}">
        <div class="flex items-start justify-between gap-3">
          <a class="text-slate-900 hover:text-indigo-700 font-medium portal-line-clamp-2" href="${url}" target="_blank" rel="noreferrer">${title}</a>
          ${badge}
        </div>
        ${
          range
            ? `<div class="mt-1 text-xs text-slate-500 inline-flex items-center gap-1">${svgIcon("icon-clock", "w-3.5 h-3.5 text-slate-400")}<span>${range}</span></div>`
            : `<div class="mt-1 text-xs text-slate-500">时间待补充</div>`
        }
      </div>`;
    })
    .join("");
}

function renderWowSkillDiffList(containerId, items) {
  const el = document.getElementById(containerId);
  if (!el) return;
  if (!items || items.length === 0) {
    el.innerHTML = `<div class="text-slate-500">暂无数据</div>`;
    return;
  }
  const q = getSearchQuery();
  const filtered = filterItems(items, q);
  if (!filtered.length) {
    el.innerHTML = `<div class="text-slate-500">无匹配结果</div>`;
    return;
  }
  el.innerHTML = filtered
    .slice(0, 20)
    .map((it, idx) => {
      const title = escapeHtml(it.title || "");
      const url = escapeHtml(sanitizeHref(it.url) || "#");
      const time = escapeHtml((it.time || "").replaceAll("\n", " ").trim());
      const divider = idx === 0 ? "" : "border-t border-slate-100";
      return `<div class="py-2 ${divider}">
        <a class="block text-slate-900 hover:text-indigo-700 font-semibold portal-line-clamp-2" href="${url}">${title}</a>
        ${time ? `<div class="mt-1 text-xs text-slate-500 inline-flex items-center gap-1">${svgIcon("icon-clock", "w-3.5 h-3.5 text-slate-400")}<span>${time}</span></div>` : ""}
      </div>`;
    })
    .join("");
}

function renderWowSkillDiffStates(containerId, items) {
  const el = document.getElementById(containerId);
  if (!el) return;
  if (!items || items.length === 0) {
    el.innerHTML = `<div class="text-slate-500">暂无服务器监控配置</div>`;
    return;
  }
  const q = getSearchQuery();
  const filtered = filterItems(items, q);
  if (!filtered.length) {
    el.innerHTML = `<div class="text-slate-500">无匹配结果</div>`;
    return;
  }
  let hotfixItem = filtered[0];
  for (const it of filtered) {
    if (Number(it.hotfix_push_id || 0) > Number(hotfixItem.hotfix_push_id || 0)) {
      hotfixItem = it;
    }
  }
  const hotfixPushId = Number(hotfixItem.hotfix_push_id || 0) || 0;
  let hotfixRow = "";
  if (hotfixPushId > 0) {
    const hotfixRunAt = escapeHtml(hotfixItem.hotfix_last_run_at || "");
    const hotfixRunStatus = escapeHtml(hotfixItem.hotfix_last_run_status || "");
    const hotfixEventAt = escapeHtml(hotfixItem.hotfix_last_event_at || "");
    const hotfixEventStatus = escapeHtml(hotfixItem.hotfix_last_event_status || "");
    const rawHotfixEvent = String(hotfixItem.hotfix_last_event_status || "");
    const hotfixSummaryTitle = escapeHtml(hotfixItem.hotfix_summary_title || "");
    const hotfixReportUrl = sanitizeHref(hotfixItem.hotfix_report_url);
    const hotfixWagoUrl = sanitizeHref(hotfixItem.hotfix_wago_url);
    const hotfixRunBadge =
      hotfixRunStatus === "异常"
        ? `<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-semibold border bg-rose-50 text-rose-700 border-rose-200">异常</span>`
        : `<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-semibold border bg-emerald-50 text-emerald-700 border-emerald-200">正常</span>`;
    const hotfixHasUpdate = rawHotfixEvent.includes("有职业更新");
    const hotfixBadge = hotfixHasUpdate
      ? `<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-extrabold border bg-violet-100 text-violet-900 border-violet-200">Hotfix 有更新</span>`
      : (hotfixEventStatus ? `<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-semibold border bg-slate-50 text-slate-700 border-slate-200">${escapeHtml(hotfixEventStatus)}</span>` : "");
    const hotfixReportBtn = hotfixReportUrl
      ? `<a class="portal-pill inline-flex items-center gap-1 px-2.5 py-1.5 text-xs font-semibold border border-slate-200 bg-white hover:bg-slate-50" href="${escapeHtml(hotfixReportUrl)}">${svgIcon("icon-chart", "w-3.5 h-3.5")}<span>Hotfix</span></a>`
      : "";
    const hotfixWagoBtn = hotfixWagoUrl
      ? `<a class="portal-pill inline-flex items-center gap-1 px-2.5 py-1.5 text-xs font-semibold border border-slate-200 bg-white hover:bg-slate-50" href="${escapeHtml(hotfixWagoUrl)}" target="_blank" rel="noreferrer">${svgIcon("icon-globe", "w-3.5 h-3.5")}<span>Hotfix Wago</span></a>`
      : "";
    hotfixRow = `<div class="py-2.5 border-b border-slate-200/70">
      <div class="grid grid-cols-1 sm:grid-cols-[1fr_auto] gap-2 items-start">
        <div class="min-w-0">
          <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
            <div class="font-semibold text-slate-900">Hotfix</div>
            <div class="text-slate-500 font-semibold">#${hotfixPushId}</div>
            ${hotfixRunBadge}
            ${hotfixBadge}
          </div>
          <div class="mt-1 text-xs text-slate-500 flex flex-wrap items-center gap-x-3 gap-y-1">
            ${hotfixSummaryTitle ? `<span class="text-slate-700 font-semibold">${hotfixSummaryTitle}</span>` : ""}
            ${hotfixRunAt ? `<span>心跳：${hotfixRunAt}</span>` : ""}
            ${hotfixEventAt ? `<span>事件时间：${hotfixEventAt}</span>` : ""}
          </div>
        </div>
        <div class="flex items-center justify-end gap-2 pt-0.5">
          ${hotfixReportBtn}
          ${hotfixWagoBtn}
        </div>
      </div>
    </div>`;
  }
  const rows = filtered
    .slice(0, 12)
    .map((it, idx) => {
      const branch = escapeHtml(it.branch || "");
      const build = escapeHtml(it.build || "-");
      const runAt = escapeHtml(it.last_run_at || "");
      const runStatus = escapeHtml(it.last_run_status || "");
      const eventAt = escapeHtml(it.last_event_at || "");
      const eventStatus = escapeHtml(it.last_event_status || "");
      const rawEvent = String(it.last_event_status || "");
      const summaryTitle = escapeHtml(it.summary_title || "");
      const reportUrl = sanitizeHref(it.report_url);
      const wagoUrl = sanitizeHref(it.wago_diff_url);
      const divider = idx === 0 ? "" : "border-t border-slate-200/70";
      const reportBtn = reportUrl
        ? `<a class="portal-pill inline-flex items-center gap-1 px-2.5 py-1.5 text-xs font-semibold border border-slate-200 bg-white hover:bg-slate-50" href="${escapeHtml(reportUrl)}">${svgIcon("icon-chart", "w-3.5 h-3.5")}<span>报告</span></a>`
        : "";
      const wagoBtn = wagoUrl
        ? `<a class="portal-pill inline-flex items-center gap-1 px-2.5 py-1.5 text-xs font-semibold border border-slate-200 bg-white hover:bg-slate-50" href="${escapeHtml(wagoUrl)}" target="_blank" rel="noreferrer">${svgIcon("icon-globe", "w-3.5 h-3.5")}<span>Wago</span></a>`
        : "";
      const runBadge =
        runStatus === "异常"
          ? `<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-semibold border bg-rose-50 text-rose-700 border-rose-200">异常</span>`
          : `<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-semibold border bg-emerald-50 text-emerald-700 border-emerald-200">正常</span>`;
      const hasUpdate = rawEvent.includes("有职业更新");
      const eventBadge = hasUpdate
        ? `<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-extrabold border bg-amber-100 text-amber-900 border-amber-200">有职业更新</span>`
        : (eventStatus ? `<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-semibold border bg-slate-50 text-slate-700 border-slate-200">${escapeHtml(eventStatus)}</span>` : "");
      return `<div class="py-2.5 ${divider}">
        <div class="grid grid-cols-1 sm:grid-cols-[1fr_auto] gap-2 items-start">
          <div class="min-w-0">
            <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
              <div class="font-semibold text-slate-900">${branch}</div>
              <div class="text-slate-500 font-semibold">${build}</div>
              ${runBadge}
              ${eventBadge}
            </div>
            <div class="mt-1 text-xs text-slate-500 flex flex-wrap items-center gap-x-3 gap-y-1">
              ${summaryTitle ? `<span class="text-slate-700 font-semibold">${summaryTitle}</span>` : ""}
              ${runAt ? `<span>心跳：${runAt}</span>` : ""}
              ${eventAt ? `<span>事件时间：${eventAt}</span>` : ""}
            </div>
          </div>
          <div class="flex items-center justify-end gap-2 pt-0.5">
            ${reportBtn}
            ${wagoBtn}
          </div>
        </div>
      </div>`;
    })
    .join("");
  el.innerHTML = `<div class="rounded-xl border border-slate-200 bg-white overflow-hidden px-3 py-2">${hotfixRow}${rows}</div>`;
}

async function loadSection(key) {
  const ep = SECTION_MAP[key];
  if (!ep) return;

  if (ep.listId) renderSkeleton(ep.listId, key === "nga" ? 10 : 6);

  try {
    let url = ep.url;
    if (key === "mplus_rankings" && PORTAL_STATE.activeDungeon) {
      url = `${ep.url}?dungeon=${encodeURIComponent(PORTAL_STATE.activeDungeon)}`;
    }
    if (key === "mythicstats_dps") {
      const dungeon = Number(PORTAL_STATE.activeMythicstatsDungeon || 0) || 0;
      const period = String(PORTAL_STATE.activeMythicstatsPeriod || "").trim();
      const season = String(PORTAL_STATE.activeMythicstatsSeason || "").trim() || MYTHICSTATS_DEFAULT_SEASON;
      const qs = [];
      if (season) qs.push(`season=${encodeURIComponent(season)}`);
      if (dungeon) qs.push(`dungeon=${encodeURIComponent(dungeon)}`);
      if (period) qs.push(`period=${encodeURIComponent(period)}`);
      url = qs.length ? `${ep.url}?${qs.join("&")}` : ep.url;
    }
    const r = await fetchJson(url);
    if (key === "videos") {
      renderVideos(r.data || {});
    } else if (key === "mplus_cutoffs") {
      const payload = r.data || {};
      const items = Array.isArray(payload.items) ? payload.items : [];
      PORTAL_STATE.dataBySection[key] = items;
      PORTAL_STATE.mplusCutoffsMeta = {
        season: String(payload.season || ""),
        updated_at: String(payload.updated_at || ""),
      };
      renderMplusCutoffs(ep.listId, { season: PORTAL_STATE.mplusCutoffsMeta.season, updated_at: PORTAL_STATE.mplusCutoffsMeta.updated_at, items });
    } else if (key === "mplus_rankings") {
      const payload = r.data || {};
      const items = payload.items || [];
      const dungeons = payload.dungeons || [];
      PORTAL_STATE.dataBySection[key] = items;
      renderMplusControls(dungeons);
      renderMplusRuns(ep.listId, items);
    } else if (key === "peak_spec_rankings") {
      const payload = r.data || {};
      PORTAL_STATE.dataBySection[key] = payload;
      renderPeakSpecControls(payload);
      renderPeakSpecGrid(ep.listId, payload);
    } else if (key === "events") {
      PORTAL_STATE.dataBySection[key] = r.data || [];
      renderEvents(r.data || []);
    } else if (key === "mythicstats_dps") {
      const payload = r.data || {};
      PORTAL_STATE.dataBySection[key] = payload;
      PORTAL_STATE.mythicstatsMeta = { dungeons: payload.dungeons || [], periods: payload.periods || [] };
      PORTAL_STATE.activeMythicstatsSeason = String(payload.season || PORTAL_STATE.activeMythicstatsSeason || MYTHICSTATS_DEFAULT_SEASON);
      PORTAL_STATE.activeMythicstatsDungeon = Number(payload.dungeon_id || 0) || 0;
      const ap = payload.active_period ? String(payload.active_period) : "";
      const hasAp = (payload.periods || []).some((p) => String(p.id) === String(PORTAL_STATE.activeMythicstatsPeriod || ""));
      if (!PORTAL_STATE.activeMythicstatsPeriod || !hasAp) PORTAL_STATE.activeMythicstatsPeriod = ap;
      renderMythicstatsControls(payload.dungeons || [], payload.periods || [], payload.seasons || []);
      renderMythicstatsTables();
    } else if (key === "wow_skill_diffs") {
      PORTAL_STATE.dataBySection[key] = r.data || [];
      renderWowSkillDiffList(ep.listId, r.data || []);
    } else if (key === "wow_skill_states") {
      PORTAL_STATE.dataBySection[key] = r.data || [];
      renderWowSkillDiffStates(ep.listId, r.data || []);
    } else {
      PORTAL_STATE.dataBySection[key] = r.data || [];
      renderSimpleList(ep.listId, r.data || [], { limit: key === "nga" ? 20 : 12 });
    }
  } catch (e) {
    if (ep.listId) {
      const el = document.getElementById(ep.listId);
      if (el) el.innerHTML = `<div class="text-slate-500">加载失败</div>`;
    }
  }
}

function updateSearchMeta() {
  const meta = document.getElementById("portal-search-meta");
  if (!meta) return;
  const q = getSearchQuery();
  if (!q) {
    meta.textContent = "";
    return;
  }
  let total = 0;
  let shown = 0;
  Object.keys(SECTION_MAP).forEach((key) => {
    const ep = SECTION_MAP[key];
    if (!ep.listId) return;
    const items =
      key === "mythicstats_dps"
        ? (PORTAL_STATE.dataBySection.mythicstats_dps_items || [])
        : (PORTAL_STATE.dataBySection[key] || []);
    total += Array.isArray(items) ? items.length : 0;
    shown += filterItems(items, q).length;
  });
  meta.textContent = `过滤结果：${shown}/${total}`;
}

function bindSearch() {
  if (PORTAL_STATE.searchBound) return;
  const input = document.getElementById("portal-search");
  if (input) {
    input.addEventListener("input", () => {
      PORTAL_STATE.query = input.value || "";
      Object.keys(SECTION_MAP).forEach((key) => {
        const ep = SECTION_MAP[key];
        if (ep.listId && PORTAL_STATE.dataBySection[key]) {
          if (key === "mplus_rankings") {
            renderMplusRuns(ep.listId, PORTAL_STATE.dataBySection[key]);
          } else if (key === "mplus_cutoffs") {
            renderMplusCutoffs(ep.listId, {
              season: PORTAL_STATE.mplusCutoffsMeta.season,
              updated_at: PORTAL_STATE.mplusCutoffsMeta.updated_at,
              items: PORTAL_STATE.dataBySection[key] || [],
            });
          } else if (key === "videos") {
            renderVideos({ tags: PORTAL_STATE.videoTags, items: PORTAL_STATE.dataBySection.videos || [] });
          } else if (key === "events") {
            renderEvents(PORTAL_STATE.dataBySection[key]);
          } else if (key === "peak_spec_rankings") {
            renderPeakSpecControls(PORTAL_STATE.dataBySection[key]);
            renderPeakSpecGrid(ep.listId, PORTAL_STATE.dataBySection[key]);
          } else if (key === "mythicstats_dps") {
            renderMythicstatsTables();
          } else {
            renderSimpleList(ep.listId, PORTAL_STATE.dataBySection[key], { limit: key === "nga" ? 20 : 12 });
          }
        }
      });
      updateSearchMeta();
    });
    PORTAL_STATE.searchBound = true;
  }
}

async function loadAll() {
  await loadTools();

  bindSearch();
  await loadSection("blueposts");
  await loadSection("exwind");
  await loadSection("wowhead");
  await loadSection("wow_skill_states");
  await loadSection("wow_skill_diffs");
  await loadSection("nga");
  await loadSection("events");
  await loadSection("videos");
  await loadSection("mplus_cutoffs");
  await loadSection("mplus_rankings");
  await loadSection("peak_spec_rankings");
  await loadSection("mythicstats_dps");
  updateSearchMeta();
}

function removeLogoWhiteBg(img) {
  if (!img || img.dataset.portalBgRemoved === "1") return;
  const w = img.naturalWidth || 0;
  const h = img.naturalHeight || 0;
  if (!w || !h) return;

  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.drawImage(img, 0, 0, w, h);

  const data = ctx.getImageData(0, 0, w, h);
  const px = data.data;
  for (let i = 0; i < px.length; i += 4) {
    const r = px[i];
    const g = px[i + 1];
    const b = px[i + 2];
    const a = px[i + 3];
    if (a === 0) continue;
    const max = Math.max(r, g, b);
    const min = Math.min(r, g, b);
    const lum = (r * 0.2126 + g * 0.7152 + b * 0.0722);
    const nearWhite = lum >= 246 && max - min <= 16;
    if (nearWhite) px[i + 3] = 0;
  }
  ctx.putImageData(data, 0, 0);

  img.src = canvas.toDataURL("image/png");
  img.dataset.portalBgRemoved = "1";
}

function bindLogoBackgroundRemoval() {
  const imgs = document.querySelectorAll(".portal-logo-img, .portal-hero-logo-img");
  imgs.forEach((img) => {
    if (!(img instanceof HTMLImageElement)) return;
    const doIt = () => removeLogoWhiteBg(img);
    if (img.complete) doIt();
    else img.addEventListener("load", doIt, { once: true });
  });
}

document.addEventListener("DOMContentLoaded", () => {
  try {
    const s = localStorage.getItem(MYTHICSTATS_STORAGE_KEY) || "";
    PORTAL_STATE.activeMythicstatsSeason = s === "season-mn-1" ? "" : s;
  } catch (e) {
    PORTAL_STATE.activeMythicstatsSeason = "";
  }
  bindLogoBackgroundRemoval();
  loadAll();
});
