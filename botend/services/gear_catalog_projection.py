"""Consumer-only projection for public equipment/enhancement catalog caches.

Call at the public snapshot boundary, AFTER the canonical serializers have
resolved identity, specialization, effects, source/track and recipe eligibility.
Never use this as an ORM/internal-service serializer: those consumers need the
central evidence. See docs/gear-catalog-storage-fields.md for the field audit.
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
