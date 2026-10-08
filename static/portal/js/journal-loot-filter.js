/* 职业资格由后台按统一规则预计算；浏览器只筛选展示。 */
globalThis.JournalLootFilter = (row, filters) => {
  const query = String(filters.loot_q || '').trim().slice(0, 100).toLowerCase();
  return (!filters.loot_boss || row.sources.some(source => source.id === Number(filters.loot_boss))) &&
    (!filters.item_type || row.item_type === filters.item_type) &&
    (filters.slot === '' || row.slot === (Number(filters.slot) || 0)) &&
    (!filters.class || row.filter_classes.includes(Number(filters.class))) &&
    (!filters.spec || row.filter_specs.includes(Number(filters.spec))) &&
    (!query || query === String(row.item_id) || row.search_names.some(name => name.toLowerCase().includes(query)));
};
