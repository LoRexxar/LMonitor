"""冒险手册复用配装器的职业装备资格与属性展示规则。"""
from types import SimpleNamespace

from botend.constants.wow import SPEC_IDENTITY_MAP
from botend.services.gear_builder import spec_matches
from botend.services.gear_builder_catalog_source import ARMOR_TYPE_NAMES, WEAPON_TYPE_NAMES
from botend.services.journal_service import SLOTS


CLASS_NAMES = ('', 'Warrior', 'Paladin', 'Hunter', 'Rogue', 'Priest', 'DeathKnight',
               'Shaman', 'Mage', 'Warlock', 'Monk', 'Druid', 'DemonHunter', 'Evoker')


def equipment_type(row):
    item_class, subclass, slot = row.get('class_id', 0), row.get('subclass_id', 0), row.get('slot', 0)
    if item_class == 2:
        return f'weapon:{subclass}', WEAPON_TYPE_NAMES.get(subclass, '其他武器')
    if item_class == 4:
        if slot in {1, 3, 5, 6, 7, 8, 9, 10, 14, 20} and subclass in ARMOR_TYPE_NAMES:
            return f'armor:{subclass}', ARMOR_TYPE_NAMES[subclass]
        return f'slot:{slot}', {2: '项链', 11: '戒指', 12: '饰品', 16: '披风'}.get(slot, SLOTS.get(slot, '其他装备'))
    return f'item:{item_class}', {0: '消耗品', 9: '配方', 12: '任务物品', 15: '杂项'}.get(item_class, '其他物品')


def class_matches(row, class_id):
    if not class_id:
        return True
    class_name = CLASS_NAMES[class_id]
    item = SimpleNamespace(
        allowable_class_mask=row.get('class_mask', -1), eligible_specs=[],
        item_class_id=row.get('class_id', 0), item_subclass_id=row.get('subclass_id', 0),
        inventory_type=row.get('slot', 0), metadata={
            'raidbots_stats_alloc': [{'id': value} for value in row.get('stat_types', [])],
        },
    )
    return any(spec_matches(item, name, spec) for name, spec in SPEC_IDENTITY_MAP.values() if name == class_name)
