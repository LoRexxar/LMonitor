"""在任务生成前批量核对装备的职业、专精、部位及主属性适用性。"""
from collections import defaultdict
import re

from django.db.models import F

from botend.constants.wow import canonical_class_spec
from botend.models import WowItemSnapshot, WowItemVariantSnapshot
from botend.services.gear_builder import (
    spec_matches, SLOT_FAMILIES, PRIMARY_ARMOR_INVENTORY_TYPES, _item_primary_options,
)
from botend.services.gear_builder_catalog_source import INVENTORY_SLOTS
from botend.services.simc_player_config import canonical_simc_profile_identity
from simc_equipment_control import ALIASES, candidate_swaps


def _identity(swap):
    raw = str(swap.get('raw_value') or '')
    options = dict(re.findall(r'(?:^|,)\s*([a-z_]+)=([^,]+)', raw))
    def number(value):
        return int(value) if str(value or '').isdigit() else 0
    return (number(options.get('id') or swap.get('item_id')),
            number(options.get('ilevel') or options.get('item_level') or swap.get('item_level')),
            {int(value) for value in re.findall(r'\d+', options.get('bonus_id', ''))})


class EquipmentEligibility:
    """一次批量读取供整个专精矩阵共用，不按专精重复查询或联网。"""

    def __init__(self, candidate_params):
        item_ids = {_identity(swap)[0] for params in candidate_params for swap in candidate_swaps(params)} - {0}
        self.items = {row.item_id: row for row in WowItemSnapshot.objects.filter(item_id__in=item_ids)} if item_ids else {}
        self.variants = defaultdict(list)
        if self.items:
            for row in WowItemVariantSnapshot.objects.filter(
                item__item_id__in=self.items, season__is_active=True,
                batch_key=F('season__gear_batch_key'),
                variant_type__in=(WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT,
                                  WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT),
            ).select_related('item').order_by('-season__gear_synced_at', '-season_id', '-pk'):
                self.variants[row.item.item_id].append(row)

    def reason(self, params, spec_key, class_name=''):
        # 历史候选将类型存于 Candidate 行，params 可能只有装备结构。
        # 非装备候选仍按显式类型跳过；无类型的单件/组合也必须检查绑定。
        if params.get('candidate_type') != 'gear_swap' and (
            'candidate_type' in params or not any(key in params for key in ('gear_swap', 'gear_swaps'))
        ):
            return None
        identity = canonical_class_spec(*canonical_simc_profile_identity(spec_key, class_name))
        if not identity:
            return {'code': 'unknown_spec', 'reason': '无法确认职业专精'}
        swaps = candidate_swaps(params)
        if not swaps:
            return {'code': 'missing_item', 'reason': '装备配置缺少物品身份'}
        for swap in swaps:
            item_id, level, bonuses = _identity(swap)
            slot = ALIASES.get(swap.get('slot'), swap.get('slot'))
            item = self.items.get(item_id)
            detail = {'item_id': item_id, 'slot': slot}
            if item is None:
                return {**detail, 'code': 'missing_metadata', 'reason': '装备资料不足，无法确认适用性'}
            variants = self.variants[item_id]
            # 不混用不同目录批次；主属性身份允许在同批次其他装等的变体中补齐。
            if variants:
                latest = variants[0]
                variants = [row for row in variants if row.season_id == latest.season_id and row.batch_key == latest.batch_key]
            variant = max(variants, key=lambda row: (
                int(row.item_level == level), len(set(row.bonus_ids or []).intersection(bonuses)), row.item_level,
            ), default=None)
            if not spec_matches(item, *identity, variant=variant, slot=slot):
                return {**detail, 'code': 'incompatible', 'reason': '装备的职业、专精、护甲、武器或主属性不适用'}
            inventory = int(item.inventory_type or 0)
            if item.item_class_id not in (2, 4) or inventory not in INVENTORY_SLOTS:
                return {**detail, 'code': 'missing_metadata', 'reason': '装备类型或部位资料不足'}
            if item.item_class_id == 4 and (
                inventory in PRIMARY_ARMOR_INVENTORY_TYPES and item.item_subclass_id not in (1, 2, 3, 4)
                or inventory == 14 and item.item_subclass_id != 6
            ):
                return {**detail, 'code': 'missing_metadata', 'reason': '护甲类型资料不足'}
            slots = list(INVENTORY_SLOTS[inventory])
            titan_grip = identity == ('Warrior', 'Fury') and inventory == 17 and slot == 'off_hand'
            if slot not in slots and SLOT_FAMILIES.get(slot) not in slots and not titan_grip:
                return {**detail, 'code': 'wrong_slot', 'reason': '装备不能用于该部位'}
            metadata = item.metadata or {}
            known_primary = (_item_primary_options(item, variant)
                             or 'primary_stat_options' in metadata or 'raidbots_stats_alloc' in metadata
                             or bool(variant and variant.stats_json) or inventory in (2, 11))
            if not known_primary:
                return {**detail, 'code': 'missing_metadata', 'reason': '装备主属性资料不足'}
        return None

    def filter(self, candidates, spec_key, class_name=''):
        accepted, skipped = [], []
        for candidate in candidates:
            reason = self.reason(candidate.get('candidate_params') or candidate.get('params') or {}, spec_key, class_name)
            if reason:
                skipped.append({'candidate_key': candidate.get('candidate_key') or candidate.get('key'),
                                'label': candidate.get('candidate_label') or candidate.get('label'), **reason})
            else:
                accepted.append(candidate)
        return accepted, skipped
