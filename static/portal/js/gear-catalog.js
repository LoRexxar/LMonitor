/* 公共装备分片：浏览器筛选、分页与有界短期缓存。 */
(() => {
  "use strict";
  function expandTexts(payload) {
    if (!payload?.variant_text_templates) return payload;
    const templates = payload.variant_text_templates;
    function decode(ref) {
      const template = templates[ref?.template], values = ref?.values;
      if (ref?.$variant_text !== 1 || !template || !Array.isArray(values)) throw new Error("装备描述模板不完整，请刷新目录。");
      const used = new Set();
      function parameter(index) {
        if (!Number.isInteger(index) || index < 0 || index >= values.length) throw new Error("装备描述参数无效。");
        used.add(index); return values[index];
      }
      function visit([kind, value]) {
        if (kind === "c") return value;
        if (kind === "v") return parameter(value);
        if (kind === "s") return value.map(part => typeof part === "number" ? String(parameter(part)) : part).join("");
        if (kind === "l") return value.map(visit);
        if (kind === "d") return Object.fromEntries(value.map(([key, row]) => [key, visit(row)]));
        throw new Error("装备描述模板格式无效。");
      }
      const result = visit(template);
      if (used.size !== values.length) throw new Error("装备描述参数数量不匹配。");
      return result;
    }
    function item(row) {
      return {...row, variants: (row.variants || []).map(variant => {
        if (!variant.text_reference) return variant;
        const {text_reference, ...rest} = variant;
        return {...rest, ...decode(text_reference)};
      })};
    }
    const {variant_text_templates, ...result} = payload;
    if (result.items) result.items = result.items.map(item);
    if (result.groups) result.groups = Object.fromEntries(Object.entries(result.groups).map(([key, rows]) => [key, rows.map(item)]));
    if (result.embellishment_options) result.embellishment_options = Object.fromEntries(Object.entries(result.embellishment_options).map(([key, row]) => [key, item(row)]));
    return result;
  }
  function preferredItems(items) {
    const selected = new Map();
    for (const item of items || []) {
      for (const variant of item.variants || []) {
        const ptr = variant.data_branch === "ptr" || variant.metadata?.data_branch === "ptr" || variant.metadata?.ptr_preview;
        const branch = ptr ? "ptr" : variant.metadata?.data_branch || variant.data_branch || "retail";
        const priority = branch === "retail" ? 0 : branch === "ptr" ? 1 : 2;
        const key = Number(item.item_id);
        const previous = selected.get(key);
        if (!previous || priority < previous.priority) selected.set(key, {priority, item, variants: [variant]});
        else if (priority === previous.priority) previous.variants.push(variant);
      }
    }
    return Array.from(selected.values(), ({item, variants}) => ({...item, variants}));
  }

  function filter(payload, {query = "", source = "all", excludedSources = [], excludedStats = [], page = 1, pageSize = 60} = {}) {
    payload = expandTexts(payload);
    const search = String(query).trim().toLowerCase();
    const numericId = /^\d+$/.test(search) ? Number(search) : null;
    const excluded = new Set(excludedSources.flatMap(key => payload.source_groups?.[key] || []));
    const rows = [];
    // 先按 ID 选择正式服或 PTR 回退，再进行搜索、来源筛选和分页。
    for (const item of preferredItems(payload.items)) {
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

  function selectEnhancements(payload, variantId) {
    payload = expandTexts(payload);
    const keys = payload.embellishments_by_equipment?.[String(variantId || "")] || [];
    const options = payload.embellishment_options || {};
    return {...payload.groups, embellishments: keys.map(key => options[key]).filter(Boolean)};
  }

  function createLoader(fetchJson, {ttlMs = 60000, maxEntries = 6, now = Date.now, kind = "equipment"} = {}) {
    const entries = new Map();
    return async function load(url) {
      const cached = entries.get(url);
      if (cached && (cached.pending || now() - cached.at < ttlMs)) return cached.promise;
      const entry = {pending: true, at: now()};
      entry.promise = Promise.resolve().then(() => fetchJson(url)).then(payload => {
        payload = expandTexts(payload);
        const complete = kind === "enhancements"
          ? ["embellishments", "gems", "enchants"].every(key => Array.isArray(payload?.groups?.[key]))
            && payload.embellishment_options && payload.embellishments_by_equipment
          : Array.isArray(payload?.items);
        if (!payload?.snapshot || !["ready", "building"].includes(payload.snapshot.state)
            || (payload.snapshot.state === "ready" && !complete)) {
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
  window.WowGearCatalog = {filter, selectEnhancements, createLoader, expandTexts};
})();
