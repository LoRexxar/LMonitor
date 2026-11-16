"""正文读取前校验范围输入；私有检查点只保存摘要和去重的依赖引用。"""
from . import wcl_combatant_snapshot as combatants
from .spec_aggregation_cache import load_work, save_work
from .spec_stats_snapshot import content_digest


SCHEMA = 'scope-input-v1'


def input_digest(rows, facts, class_name, spec_name):
    identities = []
    for row in rows:
        event = combatants.match_combatant(facts.get(combatants._fight_key(row), []),
                                           row, class_name, spec_name)
        if event is None:
            identities.append(None)
        elif getattr(event, 'cache_token', None) is not None:
            identities.append(event.cache_token)
        else:
            # 旧表内容可变，不能凭人物姓名跳过正文校验。
            return None
    return content_digest([[{k: v for k, v in row.items() if k != 'last_updated'}
                            for row in rows], identities])


def dependency_references(rows, facts):
    """只保存物品/装等、附魔和天赋引用，不复制说明文字或日志正文。"""
    from .spec_stats_service import _item_ids_from_records
    from .wow_item_display import _positive_int
    events = [event for values in facts.values() for event in values]
    gear_rows = [*rows, *({'gear_json': event.get('gear') or []} for event in events)]
    items = _item_ids_from_records(gear_rows)
    levels, enchants, talents = set(), set(), set()
    for row in gear_rows:
        for item in row.get('gear_json') or []:
            if not isinstance(item, dict):
                continue
            item_id = _positive_int(item.get('id') or item.get('itemID') or item.get('item_id'))
            if item_id:
                levels.add((item_id, _positive_int(item.get('itemLevel'))))
            for field in ('permanentEnchant', 'temporaryEnchant'):
                if value := combatants._positive_int(item.get(field)):
                    enchants.add(value)
    def collect(value):
        if isinstance(value, dict):
            for key in ('id', 'node_id', 'talent_id', 'spell_id', 'spellID', 'display_spell_id'):
                if identifier := combatants._positive_int(value.get(key)):
                    talents.add(identifier)
            for child in value.values():
                if isinstance(child, (dict, list)):
                    collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
    for row in rows:
        collect(row.get('talents_json'))
    for event in events:
        collect(event.get('talentTree'))
    return {'items': sorted(items), 'levels': sorted(levels),
            'enchants': sorted(enchants), 'talents': sorted(talents)}


def reference_records(refs):
    """恢复原依赖读取器的引用输入，复用其完整失效规则。"""
    return [{'gear_json': ([{'id': pk} for pk in refs['items']]
                          + [{'id': pk, 'itemLevel': level} for pk, level in refs['levels']]
                          + [{'permanentEnchant': pk} for pk in refs['enchants']]),
             'talents_json': [{'id': pk} for pk in refs['talents']]}]


class ScopeInputCheckpoint:
    def __init__(self, season_id, scope, class_name, spec_name, contract, *, empty_metadata=True):
        self.season_id, self.scope = season_id, 'input-check:' + scope
        self.class_name, self.spec_name = class_name, spec_name
        self.contract = content_digest([SCHEMA, contract, class_name, spec_name])
        self.empty_metadata = empty_metadata

    def reuse(self, rows, identities, *, context=None):
        """只返回核对过依赖的旧指纹；发布正文仍走原有完整性验证。"""
        from .dungeon_aggregation import _metadata_dependencies
        previous = load_work(self.season_id, self.scope, self.contract)
        if not isinstance(previous, dict):
            return None
        signature = input_digest(rows, identities() if callable(identities) else identities,
                                 self.class_name, self.spec_name)
        if signature is None or previous.get('input') != signature:
            return None
        try:
            refs = previous['references']
            metadata = (_metadata_dependencies(reference_records(refs), {}, self.class_name,
                        self.spec_name, context=context) if rows or not self.empty_metadata else {})
            if content_digest(metadata) != previous['metadata']:
                return None
            if not isinstance(previous['fingerprint'], str):
                return None
            return previous
        except (KeyError, TypeError, ValueError):
            return None

    def remember(self, rows, facts, metadata, fingerprint):
        signature = input_digest(rows, facts, self.class_name, self.spec_name)
        if signature is None:
            return
        save_work(self.season_id, self.scope, self.contract, {
            'input': signature, 'references': dependency_references(rows, facts),
            'metadata': content_digest(metadata), 'fingerprint': fingerprint,
        }, raw_json=True, durable=False)
