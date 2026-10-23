"""公开装备目录与配装刷新接口的展示投影。

仅在中央序列化完成身份、专精、特效、来源和制造资格解析后调用。
内部服务及 ORM 仍需要完整中央证据，不能使用本模块替代内部序列化。
字段审计见 docs/gear-catalog-storage-fields.md。
"""
from copy import deepcopy


# Only variant metadata has browser consumers. serialize_item metadata is not
# read by the editor/assistant; compactItem deliberately uses top-level fields.
VARIANT_METADATA_FIELDS = frozenset({
    'data_branch', 'ptr_preview',       # gear-catalog.js: preferredItems
    'venomstone',                      # gear_builder.js: variantLabel
    'primary_stat_amount',             # gear_builder.js: gemSortKey
    'two_handed',                      # gear_builder.js: weapon/lock compatibility
    'item_set_id',                     # wow-gear-sources.js: variantSources
    'enchantment_id',                  # gear_builder.js: simcEquipmentLine fallback
})

# Keep the complete compact identity_reference contract, not its backing DB2
# source/evidence. Identity is not a recursive free-form fact container.
ITEM_IDENTITY_FIELDS = frozenset({
    'schema_version', 'item_id', 'game_build', 'is_ptr', 'inventory_type',
    'slot_key', 'item_class_id', 'item_subclass_id', 'allowable_class_mask',
    'name', 'name_zh', 'primary_stat_options', 'quality', 'fact_hash',
})


def _allow_fields(value, fields):
    if not isinstance(value, dict):
        return deepcopy(value)
    return {key: deepcopy(row) for key, row in value.items() if key in fields}


def _variant(value):
    if not isinstance(value, dict):
        return deepcopy(value)
    result = {}
    for key, row in value.items():
        if key == 'metadata':
            result[key] = _allow_fields(row, VARIANT_METADATA_FIELDS)
        elif key == 'item_identity':
            result[key] = _allow_fields(row, ITEM_IDENTITY_FIELDS)
        elif key == '_item':
            result[key] = _item(row)
        else:
            result[key] = deepcopy(row)
    return result


def _items(value):
    return [_item(row) for row in value] if isinstance(value, list) else deepcopy(value)


def _item(value):
    if not isinstance(value, dict):
        return deepcopy(value)
    result = {}
    for key, row in value.items():
        if key == 'metadata':
            result[key] = _allow_fields(row, ())
        elif key == 'item_identity':
            result[key] = _allow_fields(row, ITEM_IDENTITY_FIELDS)
        elif key == 'variants' and isinstance(row, list):
            result[key] = [_variant(variant) for variant in row]
        else:
            result[key] = deepcopy(row)
    return result


def compact_catalog_content(content):
    """Return a detached, idempotent public catalog without duplicate raw facts.

    Preserve the existing equipment/enhancement envelope and every non-metadata
    protocol field (including filters, display overrides and compatibility refs).
    Explicit structural paths avoid treating arbitrary tooltip/effect JSON as
    catalog items. Missing/null values stay missing/null. Discarded evidence is
    never deep-copied, avoiding a temporary second full-sized evidence graph.
    """
    if not isinstance(content, dict):
        return deepcopy(content)
    result = {}
    for key, value in content.items():
        if key == 'items':
            result[key] = _items(value)
        elif key == 'groups' and isinstance(value, dict):
            result[key] = {group: _items(rows) for group, rows in value.items()}
        elif key == 'embellishment_options' and isinstance(value, dict):
            result[key] = {ref: _item(row) for ref, row in value.items()}
        else:
            result[key] = deepcopy(value)
    return result


def compact_equipment_payload(payload, *, snapshot_slots=()):
    """仅在公开接口裁剪配装元数据，保留属性计算结果和全部装备选择。"""
    def component(value):
        if not isinstance(value, dict):
            return deepcopy(value)
        result = {}
        for key, row in value.items():
            if key == 'item':
                result[key] = _item(row)
            elif key == 'variant':
                result[key] = _variant(row)
            elif key in ('embellishment', 'enchant'):
                result[key] = component(row)
            elif key == 'gems' and isinstance(row, list):
                result[key] = [component(gem) for gem in row]
            else:
                result[key] = deepcopy(row)
        return result

    # 用户提交并通过校验的历史快照必须原样回传；裁剪中央资料生成的响应。
    return {key: {slot: deepcopy(entry) if slot in snapshot_slots else component(entry)
                  for slot, entry in value.items()}
            if key == 'equipment' and isinstance(value, dict) else deepcopy(value)
            for key, value in payload.items()}
