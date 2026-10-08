/* 浏览器与接口在同一份已发布目录和技能上筛选。 */
globalThis.JournalCatalogMatches = (row, filters) => {
  const query = String(filters.q || '').trim().slice(0, 100).toLowerCase();
  return (!['dungeon', 'raid', 'world', 'affix'].includes(filters.kind) || row.kind === filters.kind) &&
    (!Number(filters.tier) || row.tier_ids.includes(Number(filters.tier))) &&
    (!query || row.search_names.some(name => name.toLowerCase().includes(query)));
};

globalThis.JournalSkillSelection = (data, requestedRole) => {
  const role = ['tank', 'healer', 'dps'].includes(requestedRole) ? requestedRole : '';
  const rows = data.sections.filter(row => !role || !row.roles.length || row.roles.includes(role))
    .map(row => ({...row, children: []}));
  const shown = new Map(rows.map(row => [row.id, row]));
  const tree = [];
  rows.forEach(row => {
    if (shown.has(row.parent)) shown.get(row.parent).children.push(row);
    else tree.push(row);
  });
  return {...data.boss, skills: tree, roles: data.roles.filter(row => !role || row.roles.includes(role)),
    skill_total: rows.length, has_dynamic: rows.some(row => row.has_dynamic)};
};
