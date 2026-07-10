"""将 SimC 玩家输入块标准化为 Dashboard 可展示的玩家配置详情。

该模块只解析已提交的文本和本地 WowItemSnapshot，不访问 Battle.net 或执行 SimC。
"""
from __future__ import annotations

import re

from botend.models import SimcMasteryCoefficient, SimcSecondaryStatRule, WowItemSnapshot


SPEC_CLASS = {
    'arms': 'warrior', 'fury': 'warrior', 'protection': 'warrior', 'protection_warrior': 'warrior',
    'havoc': 'demonhunter', 'vengeance': 'demonhunter',
    'balance': 'druid', 'feral': 'druid', 'guardian': 'druid', 'restoration': 'druid',
    'devastation': 'evoker', 'preservation': 'evoker', 'augmentation': 'evoker',
    'beast_mastery': 'hunter', 'marksmanship': 'hunter', 'survival': 'hunter',
    'arcane': 'mage', 'fire': 'mage', 'frost': 'mage',
    'brewmaster': 'monk', 'mistweaver': 'monk', 'windwalker': 'monk',
    'holy': 'priest', 'discipline': 'priest', 'shadow': 'priest',
    'retribution': 'paladin',
    'assassination': 'rogue', 'outlaw': 'rogue', 'subtlety': 'rogue',
    'elemental': 'shaman', 'enhancement': 'shaman', 'restoration_shaman': 'shaman',
    'affliction': 'warlock', 'demonology': 'warlock', 'destruction': 'warlock',
    'blood': 'deathknight', 'frost_dk': 'deathknight', 'unholy': 'deathknight',
}

SLOT_LABELS = {
    'head': '头盔', 'neck': '项链', 'shoulder': '肩甲', 'back': '披风', 'chest': '胸甲',
    'shirt': '衬衫', 'tabard': '战袍', 'wrist': '护腕', 'hands': '手套', 'waist': '腰带',
    'legs': '腿甲', 'feet': '靴子', 'finger1': '戒指1', 'finger2': '戒指2',
    'trinket1': '饰品1', 'trinket2': '饰品2', 'main_hand': '主手', 'off_hand': '副手',
}
EQUIPMENT_SLOTS = set(SLOT_LABELS)
SECONDARY = ('crit', 'haste', 'mastery', 'versatility')
PRIMARY = ('strength', 'agility', 'intellect', 'stamina')


def _number(value):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _parse_line(line):
    key, sep, raw_value = line.partition('=')
    if not sep:
        return '', '', {}
    key = key.strip().lower()
    raw_value = raw_value.strip()
    values = {}
    for part in raw_value.split(','):
        part = part.strip()
        if not part:
            continue
        field, has_value, value = part.partition('=')
        if has_value:
            values[field.strip().lower()] = value.strip()
    return key, raw_value, values


def _item_meta(item_id, snapshots):
    item_id = _number(item_id)
    snapshot = snapshots.get(item_id) if item_id else None
    return {
        'id': item_id,
        'name': (snapshot.name if snapshot else '') or '',
        'name_zh': (snapshot.name_zh if snapshot else '') or '',
        'display_name': ((snapshot.name_zh or snapshot.name) if snapshot else '') or (f'#{item_id}' if item_id else '未知物品'),
        'icon': (snapshot.icon if snapshot else '') or '',
        'quality': (snapshot.quality if snapshot else 0) or 0,
        'wowhead_url': f'https://www.wowhead.com/cn/item={item_id}' if item_id else '',
    }


def _secondary_stat_detail(rating, per_percent, coefficient=1):
    percent = None
    if rating is not None and per_percent:
        percent = round(rating / per_percent * coefficient, 2)
    return {'rating': rating, 'percent': percent}


def parse_manual_player_config(player_equipment, spec):
    """Parse a SimC player block. Unknown lines remain in raw_fields for traceability."""
    requested_spec = str(spec or '').strip().lower()
    parsed = {
        'identity': {'name': '', 'class_name': SPEC_CLASS.get(requested_spec, ''), 'spec': requested_spec, 'race': '', 'level': None},
        'talents': {'build_code': ''},
        'equipment': [],
        'stats': {'primary': {}, 'secondary': {}},
        'raw_fields': {},
        'missing_fields': [],
    }
    item_ids = set()
    equipment_rows = []
    for raw_line in str(player_equipment or '').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        key, raw_value, values = _parse_line(line)
        if not key:
            continue
        class_match = re.match(r'^([a-z_]+)$', key)
        if class_match and raw_value.startswith('"') and raw_value.endswith('"'):
            parsed['identity']['class_name'] = key
            parsed['identity']['name'] = raw_value[1:-1]
        elif key in ('spec', 'race', 'level'):
            parsed['identity'][key] = _number(raw_value) if key == 'level' else raw_value
        elif key in ('talents', 'talent'):
            parsed['talents']['build_code'] = raw_value
        elif key in EQUIPMENT_SLOTS:
            item_id = values.get('id')
            if item_id:
                item_ids.add(_number(item_id))
            if values.get('enchant_id'):
                item_ids.add(_number(values['enchant_id']))
            for gem_id in re.split(r'[/;:]', values.get('gems', '')):
                if gem_id:
                    item_ids.add(_number(gem_id))
            equipment_rows.append((key, values, raw_value))
        elif key in PRIMARY or key in {f'{name}_rating' for name in SECONDARY}:
            parsed['raw_fields'][key] = raw_value
        else:
            parsed['raw_fields'][key] = raw_value

    snapshots = {int(row.item_id): row for row in WowItemSnapshot.objects.filter(item_id__in=[item_id for item_id in item_ids if item_id])}
    for slot, values, raw_value in equipment_rows:
        item = _item_meta(values.get('id'), snapshots)
        enchant = _item_meta(values.get('enchant_id'), snapshots) if values.get('enchant_id') else None
        gems = [_item_meta(gem_id, snapshots) for gem_id in re.split(r'[/;:]', values.get('gems', '')) if gem_id]
        parsed['equipment'].append({
            **item,
            'slot': slot,
            'slot_label': SLOT_LABELS[slot],
            'item_level': _number(values.get('ilevel') or values.get('item_level')),
            'enchant': enchant,
            'gems': gems,
            'bonus_ids': [value for value in re.split(r'[/;:]', values.get('bonus_id', '') or values.get('bonus_ids', '')) if value],
            'raw_value': raw_value,
        })

    for stat in PRIMARY:
        value = _number(parsed['raw_fields'].get(stat))
        if value is not None:
            parsed['stats']['primary'][stat] = value
    return parsed


def build_player_config_detail(mode, spec, player_equipment='', battlenet_region='', battlenet_realm='', battlenet_character=''):
    """Return a serializable player detail object without any external request."""
    mode = 'manual_equipment' if mode == 'equipment' else mode
    if mode == 'battlenet':
        return {
            'source': {'type': 'battlenet', 'label': 'Battle.net 角色标识（未拉取）'},
            'identity': {'name': battlenet_character, 'class_name': SPEC_CLASS.get(spec, ''), 'spec': spec, 'race': '', 'level': None,
                         'region': battlenet_region, 'realm': battlenet_realm},
            'talents': {'build_code': ''}, 'equipment': [], 'stats': {'primary': {}, 'secondary': {}}, 'raw_fields': {},
            'missing_fields': ['未保存角色装备快照；预览不会访问 Battle.net。请导入完整角色配置后查看装备、天赋和属性。'],
        }

    detail = parse_manual_player_config(player_equipment, spec)
    detail['source'] = {'type': 'manual_equipment', 'label': '手动 SimC 玩家配置'}
    detail['identity']['region'] = ''
    detail['identity']['realm'] = ''
    class_name = detail['identity']['class_name'] or SPEC_CLASS.get(spec, '')
    rule = SimcSecondaryStatRule.objects.filter(class_name=class_name).first()
    mastery = SimcMasteryCoefficient.objects.filter(spec=detail['identity']['spec'] or spec).first()
    conversion = {
        'crit': getattr(rule, 'crit_per_percent', None),
        'haste': getattr(rule, 'haste_per_percent', None),
        'mastery': getattr(rule, 'mastery_per_percent', None),
        'versatility': getattr(rule, 'versatility_per_percent', None),
    }
    mastery_coefficient = getattr(mastery, 'mastery_coefficient', 1) or 1
    for stat in SECONDARY:
        rating = _number(detail['raw_fields'].get(f'{stat}_rating'))
        detail['stats']['secondary'][stat] = _secondary_stat_detail(
            rating, conversion.get(stat), mastery_coefficient if stat == 'mastery' else 1
        )
    if not detail['identity']['name']:
        detail['missing_fields'].append('玩家块未提供角色名（例如 warrior="角色名"）。')
    if not detail['talents']['build_code']:
        detail['missing_fields'].append('玩家块未提供 talents= 天赋构筑码。')
    if not detail['equipment']:
        detail['missing_fields'].append('玩家块未解析到装备槽位。')
    return detail
