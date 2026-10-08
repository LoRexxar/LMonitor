"""冒险手册复用配装器的职业装备资格与属性展示规则。"""
from types import SimpleNamespace

from botend.constants.wow import SPEC_IDENTITY_MAP, SPEC_CN
from botend.services.gear_builder import spec_matches
from botend.services.gear_builder_catalog_source import ARMOR_TYPE_NAMES, WEAPON_TYPE_NAMES
from botend.services.journal_service import SLOTS


CLASS_NAMES = ('', 'Warrior', 'Paladin', 'Hunter', 'Rogue', 'Priest', 'DeathKnight',
               'Shaman', 'Mage', 'Warlock', 'Monk', 'Druid', 'DemonHunter', 'Evoker')


def specialization_options(class_id):
    return [{'id': sid, 'name': SPEC_CN.get(spec, spec)}
            for sid, (name, spec) in SPEC_IDENTITY_MAP.items()
            if class_id and name == CLASS_NAMES[class_id]]


def enrich_loot_specializations(rows, branch, *, fallback_branch=None):
    """按物品 ID 批量读取当前分支拾取限制，不以构建号筛掉已有资料。"""
    from botend.models import WowItemSnapshot, WowItemVariantSnapshot
    from botend.services.wow_data_branch import branch_item
    ids = [row['item_id'] for row in rows]
    snapshots = {item.item_id: item for item in WowItemSnapshot.objects.filter(item_id__in=ids)}
    branches = {}
    if branch == 'current':
        for iid, value in WowItemVariantSnapshot.objects.filter(item__item_id__in=ids,
                season__is_active=True).values_list('item__item_id', 'data_branch').distinct():
            branches.setdefault(iid, set()).add(value)
    for row in rows:
        item = snapshots.get(row['item_id'])
        selected = branch
        if branch == 'current':
            available = branches.get(row['item_id'], set())
            selected = next(iter(available)) if len(available) == 1 else (fallback_branch or 'retail')
        display = ((item.metadata or {}).get('branch_display', {}).get(selected, {}) if item else {})
        same_source = selected == (fallback_branch or branch)
        spec_source = display if 'loot_spec_ids' in display else row if same_source else {}
        specs = spec_source.get('loot_spec_ids') or []
        eligible = (row.get('eligible_specs') if same_source else []) or []
        if item and (selected == 'retail' or display or branches.get(row['item_id']) == {selected}):
            eligible = branch_item(item, selected).eligible_specs or eligible
        if specs:
            eligible = [f'{SPEC_IDENTITY_MAP[sid][0]}:{SPEC_IDENTITY_MAP[sid][1]}'
                        for sid in specs if sid in SPEC_IDENTITY_MAP]
        row['eligible_specs'] = eligible
        row['loot_spec_ids'] = specs
        row['loot_spec_source'] = spec_source.get('loot_spec_source', '')
    return rows


def equipment_type(row):
    item_class, subclass, slot = row.get('class_id', 0), row.get('subclass_id', 0), row.get('slot', 0)
    if item_class == 2:
        return f'weapon:{subclass}', WEAPON_TYPE_NAMES.get(subclass, '其他武器')
    if item_class == 4:
        if slot in {1, 3, 5, 6, 7, 8, 9, 10, 14, 20} and subclass in ARMOR_TYPE_NAMES:
            return f'armor:{subclass}', ARMOR_TYPE_NAMES[subclass]
        return f'slot:{slot}', {2: '项链', 11: '戒指', 12: '饰品', 16: '披风'}.get(slot, SLOTS.get(slot, '其他装备'))
    return f'item:{item_class}', {0: '消耗品', 9: '配方', 12: '任务物品', 15: '杂项'}.get(item_class, '其他物品')


def class_matches(row, class_id, spec_id=0):
    if not class_id:
        return True
    class_name = CLASS_NAMES[class_id]
    # 已取得明确拾取名单时直接按名单判断，不再叠加配装器的推荐装备规则。
    if row.get('loot_spec_ids') or row.get('loot_spec_source'):
        return any(sid in row['loot_spec_ids'] and name == class_name and (not spec_id or sid == spec_id)
                   for sid, (name, _) in SPEC_IDENTITY_MAP.items())
    item = SimpleNamespace(
        allowable_class_mask=row.get('class_mask', -1), eligible_specs=row.get('eligible_specs') or [],
        item_class_id=row.get('class_id', 0), item_subclass_id=row.get('subclass_id', 0),
        inventory_type=row.get('slot', 0), metadata={
            'raidbots_stats_alloc': [{'id': value} for value in row.get('stat_types', [])],
        },
    )
    identities = [SPEC_IDENTITY_MAP[spec_id]] if spec_id in SPEC_IDENTITY_MAP else SPEC_IDENTITY_MAP.values()
    return any(spec_matches(item, name, spec) for name, spec in identities if name == class_name)
