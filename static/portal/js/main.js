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
  el.innerHTML = filtered
    .slice(0, limit)
    .map((it, idx) => {
      const title = escapeHtml(it.title || "");
      const url = escapeHtml(it.url || it.source_url || "#");
      const source = escapeHtml(it.source || "");
      const author = escapeHtml(it.author || "");
      const time = escapeHtml((it.time || it.publish_time || it.published_at || "").replaceAll("\n", " ").trim());
      const reply = it.reply_count ? `${escapeHtml(it.reply_count)} 回复` : "";

      const parts = [];
      if (source) parts.push(`来源：${source}`);
      if (author) parts.push(author);
      if (time) parts.push(time);
      if (reply) parts.push(reply);
      const meta = parts.join(" · ");

      const divider = idx === 0 ? "" : "border-t border-slate-100";
      return `<div class="py-2 ${divider}">
        <a class="block text-slate-900 hover:text-indigo-700 font-medium portal-line-clamp-2" href="${url}" target="_blank" rel="noreferrer">${title}</a>
        ${meta ? `<div class="mt-1 text-xs text-slate-500">${escapeHtml(meta)}</div>` : ""}
      </div>`;
    })
    .join("");
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
            return `<a class="portal-pill" href="${url}" target="_blank" rel="noreferrer">${name}</a>`;
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
            return `<a class="block p-3 rounded-xl border border-slate-200 bg-white hover:bg-slate-50 transition-colors" href="${url}" target="_blank" rel="noreferrer">
              <div class="font-medium">${name}</div>
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
  nga: { url: "/portal/api/nga-hot/", listId: "nga-list" },
  events: { url: "/portal/api/events/", listId: "events-list" },
  videos: { url: "/portal/api/videos/", listId: "videos-list", tagsId: "videos-tags" },
  mplus_rankings: { url: "/portal/api/mplus/rankings/", listId: "mplus-rankings" },
};

const PORTAL_STATE = {
  query: "",
  dataBySection: {},
  videoTags: [],
  activeVideoTag: "",
  activeDungeon: "",
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
      return `<span class="inline-flex items-center" style="color:${color}">${name}</span>`;
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
      const author = escapeHtml(it.author || "");
      const authorUrl = escapeHtml(it.author_url || "#");
      const time = escapeHtml((it.published_at || "").replaceAll("\n", " ").trim());
      const tag = escapeHtml(it.tag || "");
      const divider = idx === 0 ? "" : "border-t border-slate-100";
      return `<div class="py-2 ${divider}">
        <a class="block text-slate-900 hover:text-indigo-700 font-medium portal-line-clamp-2" href="${url}" target="_blank" rel="noreferrer">${title}</a>
        <div class="mt-1 text-xs text-slate-500">
          ${author ? `UP：<a class="text-indigo-700 hover:text-indigo-900" href="${authorUrl}" target="_blank" rel="noreferrer">${author}</a>` : ""}
          ${author && time ? " · " : ""}
          ${time}
          ${tag ? ` · ${tag}` : ""}
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
      const status = escapeHtml(it.status || "");
      const range = end ? `${start} - ${end}` : start;
      const badge = status
        ? `<span class="ml-2 inline-flex items-center px-2 py-0.5 rounded-full text-xs bg-amber-100 text-amber-800 border border-amber-200">${status}</span>`
        : "";
      const divider = idx === 0 ? "" : "border-t border-slate-100";
      return `<div class="py-2 ${divider}">
        <div class="flex items-start justify-between gap-2">
          <a class="text-slate-900 hover:text-indigo-700 font-medium portal-line-clamp-2" href="${url}" target="_blank" rel="noreferrer">${title}</a>
          ${badge}
        </div>
        ${
          range
            ? `<div class="mt-1 text-xs text-slate-600">${range}</div>`
            : `<div class="mt-1 text-xs text-slate-500">时间待补充</div>`
        }
      </div>`;
    })
    .join("");
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
    const r = await fetchJson(url);
    if (key === "videos") {
      renderVideos(r.data || {});
    } else if (key === "mplus_rankings") {
      const payload = r.data || {};
      const items = payload.items || [];
      const dungeons = payload.dungeons || [];
      if (!PORTAL_STATE.activeDungeon && Array.isArray(dungeons) && dungeons.length) {
        PORTAL_STATE.activeDungeon = dungeons[0].slug || "";
        await loadSection("mplus_rankings");
        return;
      }
      PORTAL_STATE.dataBySection[key] = items;
      renderMplusControls(dungeons);
      renderMplusRuns(ep.listId, items);
    } else if (key === "events") {
      PORTAL_STATE.dataBySection[key] = r.data || [];
      renderEvents(r.data || []);
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
    const items = PORTAL_STATE.dataBySection[key] || [];
    total += Array.isArray(items) ? items.length : 0;
    shown += filterItems(items, q).length;
  });
  meta.textContent = `过滤结果：${shown}/${total}`;
}

function bindSearch() {
  const input = document.getElementById("portal-search");
  const clearBtn = document.getElementById("portal-search-clear");
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
          } else {
            renderSimpleList(ep.listId, PORTAL_STATE.dataBySection[key], { limit: key === "nga" ? 20 : 12 });
          }
        }
      });
      updateSearchMeta();
    });
  }
  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      PORTAL_STATE.query = "";
      if (input) input.value = "";
      Object.keys(SECTION_MAP).forEach((key) => {
        const ep = SECTION_MAP[key];
        if (ep.listId && PORTAL_STATE.dataBySection[key]) {
          if (key === "mplus_rankings") {
            renderMplusRuns(ep.listId, PORTAL_STATE.dataBySection[key]);
          } else if (key === "videos") {
            renderVideos({ tags: PORTAL_STATE.videoTags, items: PORTAL_STATE.dataBySection.videos || [] });
          } else if (key === "events") {
            renderEvents(PORTAL_STATE.dataBySection[key]);
          } else {
            renderSimpleList(ep.listId, PORTAL_STATE.dataBySection[key], { limit: key === "nga" ? 20 : 12 });
          }
        }
      });
      updateSearchMeta();
    });
  }
}

async function loadAll() {
  await loadTools();

  bindSearch();
  await loadSection("blueposts");
  await loadSection("exwind");
  await loadSection("nga");
  await loadSection("events");
  await loadSection("videos");
  await loadSection("mplus_rankings");
  updateSearchMeta();
}

document.addEventListener("DOMContentLoaded", () => {
  loadAll();
});
