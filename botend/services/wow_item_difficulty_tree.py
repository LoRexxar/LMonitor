"""Whole-tree native applicability, before any child traversal or bonus effect.

Reference: TrinityCore ItemBonusMgr.cpp CanApplyBonusTreeToItem (151-191).
read_table must be the shared core's complete-envelope, evidence-recording read
function, not an unchecked provider. A verified absent ItemBonusTree row is the
native no-flags fallback; an unavailable/incomplete source is NOT an absent row.
"""

# Exact ItemTemplate.h native enum values, not tree flag values:
# https://github.com/TrinityCore/TrinityCore/blob/master/src/server/game/Entities/Item/ItemTemplate.h
ITEM_FLAG2_CASTER_WEAPON = 0x00000200  # ItemSparse.Flags_1
ITEM_FLAG4_CC_TRINKET = 0x02000000    # ItemSparse.Flags_3


class TreeUnresolved(ValueError):
    """Missing or malformed applicability input; caller must remain unresolved."""


def _integer(row, field, *, nonnegative=True):
    value = row.get(field)
    if type(value) is not int or (nonnegative and value < 0):
        raise TreeUnresolved(f'invalid typed {field}')
    return value


def tree_applies(tree_id, item_id, context, *, read_table):
    """Return {applies, reason}; exclusion applies to the ENTIRE candidate tree.

    Only mask/item-flag checks need ItemSparse. Flags 0x4 bypasses node counting,
    but never bypasses those item checks. Other tree bits are ignored natively.
    The shared caller still records the complete node envelope before traversal;
    this helper reads it for counting through the same cached evidence reader.
    """
    for field, value in (('tree_id', tree_id), ('item_id', item_id), ('context', context)):
        if type(value) is not int or value < (0 if field == 'context' else 1):
            raise TreeUnresolved(f'invalid typed {field}')

    def result(applies, reason, **details):
        return dict(applies=applies, reason=reason, **details)

    rows = read_table('ItemBonusTree', 'ID', tree_id)
    if rows is None:
        raise TreeUnresolved('ItemBonusTree: complete source unavailable')
    if len(rows) > 1:
        raise TreeUnresolved('ItemBonusTree: ambiguous referenced row')
    flags, mask = 0, 0
    if rows:
        flags = _integer(rows[0], 'Flags')
        mask = _integer(rows[0], 'InventoryTypeSlotMask')

    if mask or flags & 0x78:
        sparse_rows = read_table('ItemSparse', 'ID', item_id)
        if sparse_rows is None or len(sparse_rows) != 1:
            raise TreeUnresolved('ItemSparse: expected one complete referenced row')
        sparse = sparse_rows[0]
        if mask:
            inventory = _integer(sparse, 'InventoryType')
            if inventory >= 32:
                raise TreeUnresolved('InventoryType outside native mask shift range')
            if not mask & (1 << inventory):
                return result(False, 'inventory_mask_mismatch')
        if flags & 0x18:
            caster = bool(_integer(sparse, 'Flags_1') & ITEM_FLAG2_CASTER_WEAPON)
            if flags & 8 and not caster:
                return result(False, 'requires_caster_weapon')
            if flags & 0x10 and caster:
                return result(False, 'excludes_caster_weapon')
        if flags & 0x60:
            cc_trinket = bool(_integer(sparse, 'Flags_3') & ITEM_FLAG4_CC_TRINKET)
            if flags & 0x20 and not cc_trinket:
                return result(False, 'requires_cc_trinket')
            if flags & 0x40 and cc_trinket:
                return result(False, 'excludes_cc_trinket')

    if flags & 4:
        return result(True, 'flags_allow_multiple')

    nodes = read_table('ItemBonusTreeNode', 'ParentItemBonusTreeID', tree_id)
    if nodes is None:
        raise TreeUnresolved('ItemBonusTreeNode: complete source unavailable')
    matching = 0
    for node in nodes:
        # The native applicability gate ignores MaxMythicPlusLevel, node Flags
        # and creation-context groups. Do not import traversal gates here.
        if _integer(node, 'MinMythicPlusLevel', nonnegative=False) > 0:
            continue
        if _integer(node, 'ItemContext') in (0, context):
            matching += 1
    return result(matching <= 1,
                  'multiple_matching_nodes' if matching > 1 else 'at_most_one_matching_node',
                  matching_nodes=matching)
