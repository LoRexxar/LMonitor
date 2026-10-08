"""根据同分支 Wago 拾取规则计算专精，按物品 ID 原位补齐现有资料。"""
from collections import defaultdict
from copy import deepcopy

from django.db import transaction

from botend.constants.wow import SPEC_IDENTITY_MAP
from botend.services.journal_text import integer


# ItemSpec 的属性枚举与 ItemSparse 的属性编号不同，不能直接比较。
WEAPON_STATS = {0: 7, 1: 8, 2: 16, 3: 15, 4: 11, 5: 12, 6: 19, 7: 9,
                8: 10, 9: 28, 10: 18, 13: 14, 15: 13, 16: 20, 18: 17, 19: 21}
MOD_STATS = {3: (1,), 4: (2,), 5: (0,), 6: (3,), 13: (5,), 14: (6,),
             19: (24,), 20: (24,), 21: (24,), 32: (24,), 36: (25,), 31: (4,),
             50: (26,), 71: (0, 1, 2), 72: (1, 2), 73: (0, 1), 74: (0, 2)}
TABLE_FIELDS = {
    'ItemSpec': ('ItemType', 'PrimaryStat', 'SecondaryStat', 'SpecializationID', 'MinLevel', 'MaxLevel'),
    'ItemSpecOverride': ('ItemID', 'SpecID'),
    'ChrSpecialization': ('ClassID',),
    'GemProperties': ('Type',),
}


def eligible_names(ids):
    return [f'{SPEC_IDENTITY_MAP[sid][0]}:{SPEC_IDENTITY_MAP[sid][1]}'
            for sid in ids if sid in SPEC_IDENTITY_MAP]


class LootSpecializationRules:
    def __init__(self, tables):
        self.overrides = defaultdict(set)
        for row in tables.get('ItemSpecOverride', []):
            self.overrides[integer(row['ItemID'])].add(integer(row['SpecID']))
        self.classes = {integer(row['ID']): integer(row['ClassID'])
                        for row in tables.get('ChrSpecialization', []) if integer(row['ID']) in SPEC_IDENTITY_MAP}
        self.gems = {integer(row['ID']): integer(row['Type']) for row in tables.get('GemProperties', [])}
        self.rules = defaultdict(list)
        rows = tables.get('ItemSpec', [])
        # 手册展示当前满级拾取池，不把仅用于低等级角色的规则并入。
        max_level = max((integer(row['MaxLevel']) for row in rows), default=0)
        for row in rows:
            if integer(row['MinLevel']) <= max_level <= integer(row['MaxLevel']):
                self.rules[integer(row['ItemType'])].append(row)

    @classmethod
    def from_source(cls, source):
        return cls({name: source.table(name, 'enUS', required_fields=fields)
                    for name, fields in TABLE_FIELDS.items()})

    def resolve(self, row):
        iid = integer(row['item_id'])
        if iid in self.overrides:
            return {'loot_spec_ids': sorted(self.overrides[iid]), 'loot_spec_source': 'wago:ItemSpecOverride'}
        if not self.classes or not self.rules:
            return None
        mask = integer(row.get('class_mask'), -1)
        allowed = {sid for sid, cid in self.classes.items() if mask <= 0 or mask & (1 << (cid - 1))}
        item_class, subclass, slot = (integer(row.get(key), -1) for key in ('class_id', 'subclass_id', 'slot'))
        if min(item_class, subclass, slot) < 0:
            return None
        if item_class not in (2, 3, 4):
            # 兑换物、坐骑、玩具等不适用武器和护甲的属性规则。
            return {'loot_spec_ids': sorted(allowed), 'loot_spec_source': 'wago:AllowableClass'}
        if row.get('stat_types') is None:
            return None
        stats = {value for mod in row['stat_types'] for value in MOD_STATS.get(integer(mod), ())}
        item_type = 0
        if item_class == 3:
            gem_id = integer(row.get('gem_properties'), -1)
            if gem_id < 0 or (gem_id and gem_id not in self.gems):
                return None
            item_type = 7
            stats.update(29 + offset for offset in range(11)
                         if self.gems.get(gem_id, 0) & (1 << (offset + 6)))
        elif item_class == 2:
            item_type = 5
            if subclass in WEAPON_STATS:
                stats.add(WEAPON_STATS[subclass])
        elif subclass in (1, 2, 3, 4):
            item_type = subclass
            if subclass == 1 and slot == 16:
                item_type = 0
                stats.add(27)
        elif subclass == 6:
            item_type = 6
            stats.add(22)
        elif 6 < subclass <= 11:
            item_type = 6
            stats.add(23)
        primary_stats = stats & {0, 1, 2}
        matches = {integer(rule['SpecializationID']) for rule in self.rules[item_type]
                   if (integer(rule['PrimaryStat']) in stats or
                       (integer(rule['PrimaryStat']) == 40 and not primary_stats))
                   and (integer(rule['SecondaryStat']) == 40 or integer(rule['SecondaryStat']) in stats)} & allowed
        # 完整规则中没有专精限制的装备（例如无主属性戒指）属于通用掉落。
        return {'loot_spec_ids': sorted(matches if matches or primary_stats else allowed),
                'loot_spec_source': 'wago:ItemSpec'}


def catalog_candidate(item):
    raw = (item.get('metadata') or {}).get('raidbots_stats_alloc')
    return {'item_id': item['item_id'], 'class_id': item.get('item_class_id', -1),
            'subclass_id': item.get('item_subclass_id', -1), 'slot': item.get('inventory_type', -1),
            'class_mask': item.get('allowable_class_mask', -1),
            'stat_types': [integer(stat['id']) for stat in raw if 'id' in stat] if raw is not None else None}


def journal_branch(release, instance_id):
    return 'ptr' if str(instance_id) in (release.manifest or {}).get('ptr_overlays', {}) else 'retail'


def current_candidates(branch, catalog=()):
    from botend.journal_models import JournalState, JournalEncounter
    from botend.models import WowItemSnapshot
    from botend.services.wow_data_branch import branch_item
    rows = {}
    for item in WowItemSnapshot.objects.filter(gear_variants__data_branch=branch).distinct():
        selected = branch_item(item, branch)
        rows[item.item_id] = catalog_candidate({key: getattr(selected, key) for key in (
            'item_id', 'item_class_id', 'item_subclass_id', 'inventory_type', 'allowable_class_mask', 'metadata')})
        if branch != 'retail':
            # 基表 metadata 可能来自正式服；没有同分支手册或新目录时重新补采。
            rows[item.item_id]['stat_types'] = None
    state = JournalState.objects.select_related('active_release').filter(pk='wow-zhCN').first()
    if state and state.active_release:
        for boss in JournalEncounter.objects.filter(instance__release=state.active_release).select_related('instance'):
            if journal_branch(state.active_release, boss.instance.journal_id) == branch:
                for row in boss.payload.get('loot', []):
                    rows[row['item_id']] = row
    for item in catalog:
        candidate = catalog_candidate(item)
        # 旧目录没有保存属性来源时，优先使用手册已保存的 DB2 属性。
        if candidate['stat_types'] is not None or item['item_id'] not in rows:
            rows[item['item_id']] = candidate
    return rows


def prepare_specializations(branch, source, catalog=()):
    """抓取失败或基础字段缺失时补采真实行，不把未知当作不限专精。"""
    rules = LootSpecializationRules.from_source(source)
    candidates = current_candidates(branch, catalog)
    resolved, missing = {}, []
    for iid, row in candidates.items():
        value = rules.resolve(row)
        if value is None:
            # 仅补抓没有足够属性的 ID；完整目录不会逐物品发起请求。
            from botend.services.journal_source import http_session
            tables = {}
            for table in ('Item', 'ItemSparse'):
                source.progress(f'补采拾取规则基础数据：{iid} {table}')
                with http_session() as session:
                    response = session.get(f'https://wago.tools/api/db2-find/{table}', params={
                        'build': source.build, 'locale': 'enUS', 'filter[ID]': f'exact:{iid}'}, timeout=(10, 45))
                    response.raise_for_status()
                    tables[table] = next((r for r in response.json()['data'] if integer(r['ID']) == iid), {})
            basic, sparse = tables['Item'], tables['ItemSparse']
            if basic and sparse:
                value = rules.resolve({'item_id': iid, 'class_id': basic['ClassID'],
                    'subclass_id': basic['SubclassID'], 'slot': sparse['InventoryType'],
                    'class_mask': sparse['AllowableClass'],
                    'gem_properties': sparse.get('GemProperties', 0),
                    'stat_types': [integer(sparse.get(f'StatModifier_bonusStat_{i}'), -1) for i in range(10)]})
        if value is None:
            missing.append(iid)
        else:
            resolved[iid] = {**value, 'loot_spec_build': source.build}
    if missing:
        raise ValueError(f'拾取专精仍缺少基础数据，未发布：{missing}')
    return resolved


@transaction.atomic
def publish_specializations(branch, resolved):
    """只更新专精字段；中央装备与现有手册引用都按 ID、分支保持一致。"""
    from botend.journal_models import JournalState, JournalEncounter
    from botend.models import WowItemSnapshot
    changed = {'items': 0, 'encounters': 0}
    for item in WowItemSnapshot.objects.select_for_update().filter(item_id__in=resolved):
        facts = resolved[item.item_id]
        before = deepcopy(item.metadata)
        metadata = deepcopy(item.metadata or {})
        display = metadata.setdefault('branch_display', {}).setdefault(branch, {})
        display.update(facts, eligible_specs=eligible_names(facts['loot_spec_ids']))
        fields = []
        if before != metadata:
            item.metadata = metadata
            fields.append('metadata')
        if branch == 'retail' and item.eligible_specs != display['eligible_specs']:
            item.eligible_specs = display['eligible_specs']
            fields.append('eligible_specs')
        if fields:
            item.save(update_fields=[*fields, 'updated_at'])
            changed['items'] += 1
    state = JournalState.objects.select_related('active_release').filter(pk='wow-zhCN').first()
    if state and state.active_release:
        for boss in JournalEncounter.objects.select_for_update().filter(
                instance__release=state.active_release).select_related('instance'):
            if journal_branch(state.active_release, boss.instance.journal_id) != branch:
                continue
            before = deepcopy(boss.payload)
            for row in boss.payload.get('loot', []):
                if row['item_id'] in resolved:
                    row.update(resolved[row['item_id']])
            if boss.payload != before:
                boss.save(update_fields=['payload'])
                changed['encounters'] += 1
    return changed
