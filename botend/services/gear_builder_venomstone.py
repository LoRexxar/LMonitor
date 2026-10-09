"""至暗之夜第二赛季晋升毒液石的掉落装备升级规则。"""
from copy import deepcopy
import re


SOURCE_BUILD = '12.1.5.70077'
# ItemBonus 12848/12856 -> ItemScalingConfig 321/452；普通轨道仍为六阶。
TRACKS = {
    'hero': {'base_level': 321, 'item_level': 328, 'bonus_id': 12848},
    'myth': {'base_level': 334, 'item_level': 340, 'bonus_id': 12856},
}
# 武器（含盾牌和副手）、饰品、项链；不包括戒指和防具。
INVENTORY_TYPES = {2, 12, 13, 14, 15, 17, 21, 22, 23, 25, 26, 28}
TRACK_BONUSES = set(range(12841, 12857)) | {13848}


def tooltip_branch(metadata, requested='auto'):
    """正式服装备沿用正式服中文；只有预览装备自动选择 PTR 数据。"""
    if requested != 'auto':
        return requested
    return 'ptr-2' if (metadata or {}).get('ptr_preview') else 'live'


def upgraded_variant(inventory_type, base):
    """只从合法的英雄/神话六阶生成八阶，不沿用旧装等属性或特效。"""
    rule = TRACKS.get(base.get('upgrade_track'))
    sources = {str(row.get('type') or '').casefold() for row in base.get('sources', []) if isinstance(row, dict)}
    if (inventory_type not in INVENTORY_TYPES or not rule
            or (sources == {'delve'} and base.get('upgrade_track') == 'myth'
                and not (base.get('metadata') or {}).get('delve_myth'))
            or base.get('type') != 'drop_equipment'
            or base.get('track_rank') != 6 or base.get('track_max_rank') != 6
            or base.get('item_level') != rule['base_level']):
        return None
    result = deepcopy(base)
    result.update(
        key=f'{base["key"]}-venomstone', item_level=rule['item_level'], track_rank=8,
        stats={}, effects=[],
        bonus_ids=[int(value) for value in base.get('bonus_ids', [])
                   if int(value) not in TRACK_BONUSES] + [rule['bonus_id']],
    )
    metadata = result.setdefault('metadata', {})
    for key in ('primary_stat_values', 'primary_stat_amount', 'stats_status', 'effects_status',
                'simc_revision', 'game_build'):
        metadata.pop(key, None)
    metadata['venomstone'] = {'item_id': 280562, 'count': 10, 'build': SOURCE_BUILD}
    return result


def apply_tooltip(variant, details, *, requires_effect=False):
    """远端必须返回目标装等的属性和所需特效，否则拒绝生成可选装备。"""
    if details.get('item_level') != variant['item_level']:
        raise ValueError('毒液石 Tooltip 未返回目标装等')
    if not (details.get('stats') or details.get('primary_options') or details.get('effects')):
        raise ValueError('毒液石 Tooltip 缺少属性和特效')
    if requires_effect and not details.get('effects'):
        raise ValueError('毒液石 Tooltip 缺少装备特效')
    variant['stats'] = deepcopy(details.get('stats') or {})
    variant['effects'] = deepcopy(details.get('effects') or [])
    metadata = variant.setdefault('metadata', {})
    for key in ('primary_stat_amount', 'stats_status', 'effects_status', 'simc_revision', 'game_build'):
        metadata.pop(key, None)
    metadata['primary_stat_values'] = deepcopy(details.get('primary_options') or {})
    variant['metadata']['tooltip_item_level'] = details['item_level']
    if details.get('source'):
        variant['metadata']['tooltip_source'] = deepcopy(details['source'])


def preserve_localized_effects(variant, base_effects):
    """仅用完全匹配的双语模板绑定新数值，禁止复用六阶的中文数值。"""
    from botend.services.kithix_localization import localize_effect
    from botend.services.simc_benchmark_tooltip_generator import render_spell_description

    def normalize(text):
        text = re.sub(r'\s*\([\d.]+ (?:Min|Sec) Cooldown\)$', '', text)
        text = re.sub(r'^(?:Equip|Use):\s*', '', text)
        text = re.sub(r'\(1 \* (\d+)\)', r'\1', text)
        return re.sub(r'\s+', ' ', text.replace(' sec', '秒').replace('条件说明：', '')).strip()

    translated = []
    for effect in variant['effects']:
        if effect.get('description_zh'):
            translated.append(effect)
            continue
        result = None
        for old in base_effects:
            if not isinstance(old, dict) or not old.get('template') or not old.get('template_zh'):
                continue
            original, _ = render_spell_description(old['template'], base_spell_id=old['spell_id'], spell_queries={})
            chinese, _ = render_spell_description(old['template_zh'], base_spell_id=old['spell_id'], spell_queries={})
            candidate = {'spell_id': old['spell_id'], 'trigger_type': old.get('trigger_type'),
                         'template': normalize(original), 'description': normalize(effect.get('description', ''))}
            try:
                result = localize_effect(candidate, normalize(chinese), old.get('localization_build', SOURCE_BUILD))
                break
            except ValueError:
                continue
        if result is None and any(isinstance(old, dict) and old.get('description_zh') for old in base_effects):
            raise ValueError('毒液石特效缺少与新数值匹配的中文模板')
        translated.append(result or effect)
    variant['effects'] = translated
