"""在任务生成前批量核对装备的职业、专精、部位及主属性适用性。"""
from collections import defaultdict
import re

from django.db.models import F
from django.core.exceptions import ValidationError
from botend.services.wow_item_identity import resolve_item_identity, project_item_identity

from botend.constants.wow import canonical_class_spec
from botend.models import WowItemSnapshot, WowItemVariantSnapshot
from botend.services.gear_builder import (
    spec_matches, SLOT_FAMILIES, PRIMARY_ARMOR_INVENTORY_TYPES, _item_primary_options,
    embellishment_eligibility_reason,
)
from botend.services.gear_builder_catalog_source import INVENTORY_SLOTS
from botend.services.simc_player_config import canonical_simc_profile_identity
from simc_equipment_control import ALIASES, candidate_swaps, equipment_rules


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
        candidate_params = list(candidate_params)
        self.embellishment_rules = equipment_rules()['embellishments']
        self.effect_bonuses = {row['bonus_id'] for row in self.embellishment_rules.values()}
        self.embellishments = defaultdict(list)
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
        if any(self._declared_embellishments(swap) != (set(), False)
               for params in candidate_params for swap in candidate_swaps(params)):
            for row in WowItemVariantSnapshot.objects.filter(
                season__is_active=True, batch_key=F('season__gear_batch_key'),
                variant_type=WowItemVariantSnapshot.TYPE_EMBELLISHMENT,
            ).select_related('item'):
                # Only rule-declared effect bonuses identify materials. 8960 is
                # a shared marker, not eighteen separate embellishments.
                bonuses = {int(value) for value in (row.bonus_ids or []) if str(value).isdigit()}
                for bonus in bonuses.intersection(self.effect_bonuses):
                    self.embellishments[(row.season_id, row.batch_key, row.game_build, bonus)].append(row)

    def _declared_embellishments(self, swap):
        bonuses = _identity(swap)[2].intersection(self.effect_bonuses)
        options = dict(re.findall(r'(?:^|,)\s*([a-z_]+)=([^,]+)', str(swap.get('raw_value') or '')))
        token = str(options.get('embellishment') or '').strip().lower()
        unknown = bool(token and token != 'none' and token not in self.embellishment_rules)
        if token in self.embellishment_rules:
            bonuses.add(self.embellishment_rules[token]['bonus_id'])
        return bonuses, unknown

    def _embellishment_reason(self, swap, variant, slot, class_name, spec_name):
        bonuses, unknown = self._declared_embellishments(swap)
        if unknown:
            return {'code': 'embellishment_unknown', 'reason': '无法识别所声明的美化效果'}
        if not bonuses:
            return None
        if len(bonuses) > 1:
            return {'code': 'embellishment_incompatible', 'reason': '单件装备不能附加多个美化'}
        if variant is None:
            return embellishment_eligibility_reason(None, None, slot, class_name, spec_name)
        bonus = next(iter(bonuses))
        materials = self.embellishments.get((variant.season_id, variant.batch_key, variant.game_build, bonus), [])
        if not materials:
            return embellishment_eligibility_reason(variant, None, slot, class_name, spec_name)
        reasons = [embellishment_eligibility_reason(variant, row, slot, class_name, spec_name)
                   for row in materials]
        # Multiple qualities may describe one effect. Conflicting relationships
        # are not evidence that the permissive variant is the correct one.
        if any(reason != reasons[0] for reason in reasons):
            return {'code': 'embellishment_unknown', 'reason': '中央美化材料适用关系存在歧义'}
        return reasons[0]

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
            try:
                ref = resolve_item_identity(item, game_build=swap.get('game_build', ''),
                                            is_ptr=swap.get('is_ptr', False))
            except ValidationError:
                return {**detail, 'code': 'missing_metadata', 'reason': '缺少同分支精确构建装备身份'}
            item = project_item_identity(item, ref)
            variants = self.variants[item_id]
            if ref:
                variants = [row for row in variants if row.game_build == ref['game_build']]
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
            embellishment_reason = self._embellishment_reason(swap, variant, slot, *identity)
            if embellishment_reason:
                return {**detail, **embellishment_reason}
        weapons = self.weapon_layout(params, spec_key, class_name)
        if (weapons and not weapons['titan_grip']
                and any(row['slot'] == 'main_hand' and row['inventory_type'] == 17
                        for row in weapons['weapons'])
                and any(row['slot'] == 'off_hand' for row in weapons['weapons'])):
            return {'code': 'invalid_weapon_layout', 'reason': '双手主手不能同时配置副手装备'}
        return None

    def weapon_layout(self, params, spec_key, class_name=''):
        """Freeze central slot facts once; execution must never consult live items."""
        weapons = []
        for swap in candidate_swaps(params):
            slot = ALIASES.get(swap.get('slot'), swap.get('slot'))
            if slot not in ('main_hand', 'off_hand'):
                continue
            item_id = _identity(swap)[0]
            item = self.items[item_id]
            ref = resolve_item_identity(item, game_build=swap.get('game_build', ''),
                                        is_ptr=swap.get('is_ptr', False))
            item = project_item_identity(item, ref)
            weapons.append({'slot': slot, 'item_id': item_id,
                            'inventory_type': int(item.inventory_type)})
        if not weapons:
            return None
        identity = canonical_class_spec(*canonical_simc_profile_identity(spec_key, class_name))
        return {'version': 1, 'weapons': weapons, 'titan_grip': identity == ('Warrior', 'Fury')}

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
