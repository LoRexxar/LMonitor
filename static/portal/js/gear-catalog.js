/* 公共装备分片：浏览器筛选、分页与有界短期缓存。 */
(() => {
  "use strict";
  function filter(payload, {query = "", source = "all", excludedSources = [], excludedStats = [], page = 1, pageSize = 60} = {}) {
    const search = String(query).trim().toLowerCase();
    const numericId = /^\d+$/.test(search) ? Number(search) : null;
    const excluded = new Set(excludedSources.flatMap(key => payload.source_groups?.[key] || []));
    const rows = [];
    for (const item of payload.items || []) {
      if (search && Number(item.item_id) !== numericId && !(item._search || []).some(name => String(name || "").toLowerCase().includes(search))) continue;
      const variants = (item.variants || []).filter(variant => {
        const facts = variant._filter || {};
        const sources = facts.sources || [];
        return (!source || source === "all" || sources.includes(source.toLowerCase()))
          && !sources.some(value => excluded.has(value))
          && !excludedStats.some(value => (facts.stats || []).includes(value));
      });
      if (variants.length) rows.push({...item, ...variants[0]._item, variants});
    }
    rows.sort((a, b) => {
      const level = Math.max(...b.variants.map(v => v.item_level || 0)) - Math.max(...a.variants.map(v => v.item_level || 0));
      return level || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0)
        || (a.variants[0]._order || 0) - (b.variants[0]._order || 0);
    });
    const size = Math.min(100, Math.max(1, Number(pageSize) || 60));
    const start = (Math.max(1, Number(page) || 1) - 1) * size;
    return {items: rows.slice(start, start + size), total: rows.length, snapshot: payload.snapshot};
  }

  function createLoader(fetchJson, {ttlMs = 60000, maxEntries = 6, now = Date.now} = {}) {
    const entries = new Map();
    return async function load(url) {
      const cached = entries.get(url);
      if (cached && (cached.pending || now() - cached.at < ttlMs)) return cached.promise;
      const entry = {pending: true, at: now()};
      entry.promise = Promise.resolve().then(() => fetchJson(url)).then(payload => {
        if (!payload?.snapshot || !["ready", "building"].includes(payload.snapshot.state)
            || (payload.snapshot.state === "ready" && !Array.isArray(payload.items))) {
          throw new Error("装备目录数据格式不完整，请稍后刷新重试。");
        }
        entry.pending = false;
        entry.at = now();
        if (payload.snapshot?.state !== "ready" && entries.get(url) === entry) entries.delete(url);
        return payload;
      }).catch(error => {
        if (entries.get(url) === entry) entries.delete(url);
        throw error;
      });
      entries.delete(url);
      entries.set(url, entry);
      while (entries.size > maxEntries) entries.delete(entries.keys().next().value);
      return entry.promise;
    };
  }
  window.WowGearCatalog = {filter, createLoader};
})();
