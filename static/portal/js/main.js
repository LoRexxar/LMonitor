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
  return String(s || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
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
  const html = filtered
    .slice(0, limit)
    .map((it, idx) => {
      const title = escapeHtml(it.title || "");
      const url = escapeHtml(it.url || it.source_url || "#");
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
      return `<div class="py-2 ${divider}">
        <a class="block text-slate-900 hover:text-indigo-700 font-medium portal-line-clamp-2" href="${url}" target="_blank" rel="noreferrer">${title}</a>
        ${meta ? `<div class="mt-1 text-xs text-slate-500 flex flex-wrap items-center gap-x-2 gap-y-1">${meta}</div>` : ""}
      </div>`;
    })
    .join("");

  el.innerHTML = asGrid ? `<div class="grid grid-cols-1 md:grid-cols-2 gap-x-6">${html}</div>` : html;
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
            const url = escapeHtml(it.url || "#");
            const icon = escapeHtml(getFaviconSrc(it));
            const fallback = "/static/portal/favicons/default.svg";
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
            const url = escapeHtml(it.url || "#");
            const desc = escapeHtml(it.desc || "");
            const icon = escapeHtml(getFaviconSrc(it));
            const fallback = "/static/portal/favicons/default.svg";
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
  wow_skill_states: { url: "/portal/api/wow-skill-diff/states/", listId: "wow-skill-diff-states" },
  wow_skill_diffs: { url: "/portal/api/wow-skill-diffs/", listId: "wow-skill-diff-list" },
  nga: { url: "/portal/api/nga-hot/", listId: "nga-list" },
  events: { url: "/portal/api/events/", listId: "events-list" },
  videos: { url: "/portal/api/videos/", listId: "videos-list", tagsId: "videos-tags" },
  mplus_rankings: { url: "/portal/api/mplus/rankings/", listId: "mplus-rankings" },
  mythicstats_dps: { url: "/portal/api/mythicstats/dps/", listId: "mythicstats-table" },
};

const PORTAL_STATE = {
  query: "",
  dataBySection: {},
  videoTags: [],
  activeVideoTag: "",
  activeDungeon: "",
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
    "priest": "#FFFFFF",
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

function renderMplusRuns(containerId, items) {
  const el = document.getElementById(containerId);
  if (!el) return;
  if (!items || !items.length) {
    el.innerHTML = `<div class="text-slate-500">暂无数据</div>`;
    return;
  }
  const q = getSearchQuery();
  const filtered = filterItems(items, q);
  const rows = filtered.slice(0, 10).map((it) => {
    const rank = escapeHtml(it.rank);
    const dungeon = escapeHtml(it.dungeon_cn || it.dungeon || "");
    const level = escapeHtml(it.level);
    const score = it.score ? escapeHtml(it.score) : "";
    const time = it.time_seconds ? `${Math.floor(it.time_seconds / 60)}:${String(it.time_seconds % 60).padStart(2, "0")}` : "";
    const party = Array.isArray(it.party) ? it.party : [];
    const tank = party.find((p) => (p.role || "") === "tank") || {};
    const healer = party.find((p) => (p.role || "") === "healer") || {};
    const dpsList = party.filter((p) => (p.role || "") === "dps");
    const renderMember = (m) => {
      const name = escapeHtml(m?.name || "-");
      const color = classColor(m?.class_slug);
      const border = color.toLowerCase() === "#ffffff" ? "border-slate-300" : "border-transparent";
      return `<span class="inline-flex items-center gap-1.5 rounded-md bg-white/70 border ${border} px-1.5 py-0.5">
        <span class="w-2 h-2 rounded-full border border-slate-200" style="background:${color}"></span>
        <span class="text-slate-800 font-semibold">${name}</span>
      </span>`;
    };
    const tankHtml = renderMember(tank);
    const healerHtml = renderMember(healer);
    const dpsHtml = dpsList.length ? dpsList.map(renderMember).join(" / ") : (Array.isArray(it.dps) ? it.dps.map((x) => escapeHtml(x)).join(" / ") : "-");
    const link = it.run_url ? `<a class="text-indigo-700 hover:text-indigo-900 text-xs" href="${escapeHtml(it.run_url)}" target="_blank" rel="noreferrer">详情</a>` : "";
    return `<tr class="border-t border-slate-100">
      <td class="py-2 pr-2 text-slate-500">${rank}</td>
      <td class="py-2 pr-2">
        <div class="font-medium text-slate-900">${dungeon}</div>
        <div class="text-xs text-slate-500 mt-0.5">T：${tankHtml} · N：${healerHtml} · DPS：${dpsHtml}</div>
      </td>
      <td class="py-2 pr-2 text-slate-700">+${level}</td>
      <td class="py-2 pr-2 text-slate-700">${time}</td>
      <td class="py-2 pr-2 text-slate-700">${score}</td>
      <td class="py-2 pr-2 text-right">${link}</td>
    </tr>`;
  });
  el.innerHTML = `
    <table class="w-full text-sm">
      <thead class="text-xs text-slate-500">
        <tr>
          <th class="text-left py-2 pr-2 font-medium">排名</th>
          <th class="text-left py-2 pr-2 font-medium">地下城</th>
          <th class="text-left py-2 pr-2 font-medium">层数</th>
          <th class="text-left py-2 pr-2 font-medium">时间</th>
          <th class="text-left py-2 pr-2 font-medium">分数</th>
          <th class="text-right py-2 pr-2 font-medium">链接</th>
        </tr>
      </thead>
      <tbody>${rows.join("")}</tbody>
    </table>
  `;
}

const MYTHICSTATS_DEFAULT_SEASON = "";
const MYTHICSTATS_STORAGE_KEY = "portal_mythicstats_season";

function renderMythicstatsControls(dungeons, periods) {
  const el = document.getElementById("mythicstats-controls");
  if (!el) return;
  const dList = Array.isArray(dungeons) ? dungeons : [];
  const pList = Array.isArray(periods) ? periods : [];
  const payload = PORTAL_STATE.dataBySection.mythicstats_dps || {};
  const seasons = Array.isArray(payload.seasons) ? payload.seasons : [];
  const seasonOptions =
    `<option value="">当前赛季</option>` +
    seasons
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
  </div>`;
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
  "unholy-death-knight": "邪恶死亡骑士",
  "frost-death-knight": "冰霜死亡骑士",
  "blood-death-knight": "鲜血死亡骑士",
  "demonology-warlock": "恶魔学识术士",
  "affliction-warlock": "痛苦术士",
  "destruction-warlock": "毁灭术士",
  "devourer-demon-hunter": "吞噬恶魔猎手",
  "havoc-demon-hunter": "浩劫恶魔猎手",
  "vengeance-demon-hunter": "复仇恶魔猎手",
  "retribution-paladin": "惩戒圣骑士",
  "protection-paladin": "防护圣骑士",
  "holy-paladin": "神圣圣骑士",
  "arms-warrior": "武器战士",
  "fury-warrior": "狂怒战士",
  "protection-warrior": "防护战士",
  "outlaw-rogue": "狂徒潜行者",
  "subtlety-rogue": "敏锐潜行者",
  "assassination-rogue": "奇袭潜行者",
  "feral-druid": "野性德鲁伊",
  "balance-druid": "平衡德鲁伊",
  "guardian-druid": "守护德鲁伊",
  "restoration-druid": "恢复德鲁伊",
  "survival-hunter": "生存猎人",
  "beast-mastery-hunter": "兽王猎人",
  "marksmanship-hunter": "射击猎人",
  "enhancement-shaman": "增强萨满祭司",
  "elemental-shaman": "元素萨满祭司",
  "restoration-shaman": "恢复萨满祭司",
  "augmentation-evoker": "增辉唤魔师",
  "devastation-evoker": "湮灭唤魔师",
  "preservation-evoker": "恩护唤魔师",
  "windwalker-monk": "踏风武僧",
  "brewmaster-monk": "酒仙武僧",
  "mistweaver-monk": "织雾武僧",
  "shadow-priest": "暗影牧师",
  "discipline-priest": "戒律牧师",
  "holy-priest": "神圣牧师",
  "arcane-mage": "奥术法师",
  "fire-mage": "火焰法师",
  "frost-mage": "冰霜法师",
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
    const url = escapeHtml(it.spec_url || "#");

    const avgVal = Number.isFinite(Number(it.avg_value)) ? Number(it.avg_value) : 0;
    const topVal = Number.isFinite(Number(it.top_value)) ? Number(it.top_value) : 0;
    const avg = escapeHtml(it.avg || "");
    const top = escapeHtml(it.top || "");
    const avgPct = Math.max(0, Math.min(100, (avgVal / maxTop) * 100));
    const topPct = Math.max(0, Math.min(100, (topVal / maxTop) * 100));

    const bar = `<div class="relative h-3 rounded bg-slate-200/70 overflow-hidden shadow-inner">
      <div class="absolute inset-y-0 left-0" style="width:${topPct.toFixed(1)}%;background:${mythicstatsHexToRgba(color, 0.22)}"></div>
      <div class="absolute inset-y-0 left-0" style="width:${avgPct.toFixed(1)}%;background:${mythicstatsHexToRgba(color, 0.92)}"></div>
    </div>`;

    return `<div class="py-2">
      <div class="flex items-start gap-3">
        <div class="w-8 pt-0.5 text-xs font-semibold text-slate-500">${rank}</div>
        <div class="w-[500px] min-w-[500px] grid grid-cols-[1fr_44px_56px_76px_76px_52px] items-center gap-2">
          <a class="font-semibold truncate" style="color:${escapeHtml(color)}" href="${url}" target="_blank" rel="noreferrer">${name}</a>
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
  return `<div class="divide-y divide-slate-100">${rows.join("")}</div>`;
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
      const url = escapeHtml(it.url || "#");
      const cover = escapeHtml(it.cover_url || it.cover || "");
      const author = escapeHtml(it.author || "");
      const authorUrl = escapeHtml(it.author_url || "#");
      const time = escapeHtml((it.published_at || "").replaceAll("\n", " ").trim());
      const tag = escapeHtml(it.tag || "");
      const divider = idx === 0 ? "" : "border-t border-slate-100";
      const coverHtml = cover
        ? `<a class="shrink-0 w-20 h-11 rounded-lg overflow-hidden border border-slate-200 bg-slate-100" href="${url}" target="_blank" rel="noreferrer">
            <img src="${cover}" alt="" class="w-full h-full object-cover" loading="lazy" />
          </a>`
        : `<div class="shrink-0 w-20 h-11 rounded-lg overflow-hidden border border-slate-200 portal-skeleton"></div>`;
      return `<div class="py-2 ${divider}">
        <div class="flex items-start gap-3">
          ${coverHtml}
          <div class="min-w-0 flex-1">
            <a class="block text-slate-900 hover:text-indigo-700 font-medium portal-line-clamp-2" href="${url}" target="_blank" rel="noreferrer">${title}</a>
            <div class="mt-1 text-xs text-slate-500">
              ${author ? `UP：<a class="text-indigo-700 hover:text-indigo-900" href="${authorUrl}" target="_blank" rel="noreferrer">${author}</a>` : ""}
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
      const url = escapeHtml(it.url || "#");
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
  const rows = filtered
    .slice(0, 12)
    .map((it, idx) => {
      const branch = escapeHtml(it.branch || "");
      const build = escapeHtml(it.build || "-");
      const runAt = escapeHtml(it.last_run_at || "");
      const runStatus = escapeHtml(it.last_run_status || "");
      const eventAt = escapeHtml(it.last_event_at || "");
      const eventStatus = escapeHtml(it.last_event_status || "");
      const reportUrl = String(it.report_url || "").trim();
      const wagoUrl = String(it.wago_diff_url || "").trim();
      const divider = idx === 0 ? "" : "border-t border-slate-100";
      const reportBtn = reportUrl
        ? `<a class="portal-pill inline-flex items-center gap-1 px-2 py-1 text-xs" href="${escapeHtml(reportUrl)}">${svgIcon("icon-chart", "w-3.5 h-3.5")}<span>报告</span></a>`
        : "";
      const wagoBtn = wagoUrl
        ? `<a class="portal-pill inline-flex items-center gap-1 px-2 py-1 text-xs" href="${escapeHtml(wagoUrl)}" target="_blank" rel="noreferrer">${svgIcon("icon-globe", "w-3.5 h-3.5")}<span>Wago</span></a>`
        : "";
      const statusBadge =
        runStatus === "异常"
          ? `<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-semibold border bg-rose-50 text-rose-700 border-rose-200">异常</span>`
          : `<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-semibold border bg-emerald-50 text-emerald-700 border-emerald-200">正常</span>`;
      return `<div class="py-2 ${divider}">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div class="min-w-0">
            <div class="font-semibold text-slate-900">${branch} <span class="text-slate-500 font-normal">${build}</span></div>
            <div class="mt-1 text-xs text-slate-500 flex flex-wrap items-center gap-x-2 gap-y-1">
              ${statusBadge}
              ${runAt ? `<span>心跳：${runAt}</span>` : ""}
              ${eventStatus ? `<span>事件：${eventStatus}</span>` : ""}
              ${eventAt ? `<span>${eventAt}</span>` : ""}
            </div>
          </div>
          <div class="flex items-center gap-2">
            ${reportBtn}
            ${wagoBtn}
          </div>
        </div>
      </div>`;
    })
    .join("");
  el.innerHTML = `<div class="rounded-xl border border-slate-200 bg-white">${rows}</div>`;
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
    } else if (key === "mplus_rankings") {
      const payload = r.data || {};
      const items = payload.items || [];
      const dungeons = payload.dungeons || [];
      PORTAL_STATE.dataBySection[key] = items;
      renderMplusControls(dungeons);
      renderMplusRuns(ep.listId, items);
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
      renderMythicstatsControls(payload.dungeons || [], payload.periods || []);
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
          } else if (key === "videos") {
            renderVideos({ tags: PORTAL_STATE.videoTags, items: PORTAL_STATE.dataBySection.videos || [] });
          } else if (key === "events") {
            renderEvents(PORTAL_STATE.dataBySection[key]);
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
  await loadSection("wow_skill_states");
  await loadSection("wow_skill_diffs");
  await loadSection("nga");
  await loadSection("events");
  await loadSection("videos");
  await loadSection("mplus_rankings");
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
