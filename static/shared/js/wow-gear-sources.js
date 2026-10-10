/* Shared compact and full gear-source presentation. */
(() => {
  "use strict";
  const SOURCE_LABELS = {
    mythic_plus: "大秘境", great_vault: "宏伟宝库", raid: "团队副本", delve: "地下堡",
    crafted: "专业制造", profession: "专业制造", bonus_roll: "额外掉落",
  };
  const SOURCE_PLACE_FALLBACKS = {
    mythic_plus: "当前大秘境", great_vault: "宏伟宝库", raid: "当前团队副本",
    delve: "当前赛季地下堡", crafted: "专业制造", profession: "专业制造",
  };

  function variantSources(variant, bootstrap = {}) {
    const catalog = bootstrap?.tier_set_sources;
    const setId = Number(variant?.metadata?.item_set_id);
    const slot = variant?.compatible_slots?.[0];
    if (catalog?.set_ids?.includes(setId) && catalog.slots?.[slot]) return catalog.slots[slot];
    return Array.isArray(variant?.sources) ? variant.sources : [];
  }

  function sourceText(variant, bootstrap = {}) {
    const rows = variantSources(variant, bootstrap);
    if (!rows.length) return "来源待补全";
    return rows.slice(0, 2).map((row) => {
      if (typeof row === "string") return row;
      const type = row.type_zh || SOURCE_LABELS[row.type] || "其他来源";
      const instance = row.instance_zh || row.instance || "";
      const encounter = row.encounter_zh || row.boss_zh || row.encounter || row.boss || "";
      const bossNumber = raidBossNumber(row, bootstrap);
      const location = bossNumber && encounter
        ? `${instance}${bossNumber}号 ${encounter}`
        : [instance, encounter].filter(Boolean).join(" · ");
      const profession = row.profession_zh || "";
      const difficulty = row.difficulty_zh || "";
      const parts = [type, location, profession, difficulty].filter(Boolean);
      if (parts.length === 1 && SOURCE_PLACE_FALLBACKS[row.type]) parts.push(SOURCE_PLACE_FALLBACKS[row.type]);
      return [...new Set(parts)].join(" · ");
    }).join("\n");
  }

  function raidBossNumber(source, bootstrap = {}) {
    if (source?.type !== "raid" || !(Number(source.encounter_id) > 0)) return 0;
    const mapped = bootstrap?.raid_boss_numbers?.[source.instance_id]?.[source.encounter_id];
    if (Number.isInteger(mapped) && mapped > 0) return mapped;
    const order = Number(source.encounter_order);
    // 汇总目录的跨团本偏移编号不能作为团本内序号展示。
    return Number.isInteger(order) && order > 0 && order < 100 ? order : 0;
  }

  function shortSourceText(variant, bootstrap = {}) {
    if (variant?.type === "crafted_equipment") return "专业制造";
    const sources = variantSources(variant, bootstrap).map((row) => {
      if (typeof row === "string") return row;
      const instance = row.instance_zh || row.instance || "";
      if (row.type === "crafted" || row.type === "profession") return "专业制造";
      if (row.type === "mythic_plus") {
        const grouped = Number(row.instance_id) < 0 || ["大秘境", "Mythic+ Dungeons"].includes(instance);
        return (grouped ? row.encounter_zh || row.encounter : instance) || "大秘境";
      }
      if (row.type === "raid") {
        const bossNumber = raidBossNumber(row, bootstrap);
        return `${instance || "团队副本"}${bossNumber ? `${bossNumber}号` : ""}`;
      }
      return sourceText({sources: [row]}, bootstrap);
    });
    return [...new Set(sources.filter(Boolean))].slice(0, 2).join("；") || "来源待补全";
  }

  globalThis.WowGearSources = {variantSources, sourceText, raidBossNumber, shortSourceText};
})();
