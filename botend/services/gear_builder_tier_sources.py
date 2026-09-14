"""按赛季套装 ID 和部位提供已核实的首领掉落来源。"""

from botend.constants.wow import localize_gear_source


# 午夜 S2 的 13 个职业套装；限制套装 ID，避免套用到其他赛季或普通装备。
MIDNIGHT_S2_SET_IDS = frozenset(range(2055, 2068))

# 首领与兑换物关联：Wago JournalEncounterItem，构建 12.1.0.69587。
# 本地冒险手册未提供兑换物对应部位，部位关系由 Wowhead 套装掉落表补齐：
# https://www.wowhead.com/cn/guide/midnight/raids/the-venomous-abyss-rewards-gear-loot
MIDNIGHT_S2_TIER_DROPS = {
    'hands': (2874, 'Entombed Sentinels', 2, (270910, 270911, 270912, 270913)),
    'shoulders': (2894, 'The Lost Explorers', 3, (270922, 270923, 270924, 270925)),
    'chest': (2882, 'Vashnik the Malignant', 4, (270926, 270927, 270928, 270929)),
    'legs': (2871, 'Sszorak', 5, (270918, 270919, 270920, 270921)),
    'head': (2887, 'The Twin Fangs', 6, (270914, 270915, 270916, 270917)),
}


def tier_set_sources(metadata, slot):
    """返回该部位实际掉落首领；无法确认的套装返回 None，不猜测来源。"""
    try:
        set_id = int((metadata or {}).get('item_set_id') or 0)
    except (TypeError, ValueError):
        return None
    if set_id not in MIDNIGHT_S2_SET_IDS or slot not in MIDNIGHT_S2_TIER_DROPS:
        return None
    drops = [MIDNIGHT_S2_TIER_DROPS[slot], (2895, "Ula'tek", 8, (270909,))]
    return [localize_gear_source({
        'type': 'raid',
        'instance_id': 1320,
        'instance': 'The Venomous Abyss',
        'encounter_id': encounter_id,
        'encounter': name,
        'encounter_order': order,
        'loot_item_ids': list(item_ids),
    }) for encounter_id, name, order, item_ids in drops]


def tier_set_source_catalog():
    """供浏览器修正已保存在本地的旧套装来源，不修改配装本身。"""
    return {
        'set_ids': sorted(MIDNIGHT_S2_SET_IDS),
        'slots': {slot: tier_set_sources({'item_set_id': 2055}, slot) for slot in MIDNIGHT_S2_TIER_DROPS},
    }
