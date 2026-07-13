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

# Battle.net profile API returns display names with spaces (e.g. ``Death Knight``),
# while SimC/player templates use compact class slugs.  Keep this conversion at the
# API boundary so the validation path does not reject a valid death knight profile.
BATTLETNET_CLASS_SLUGS = {
    'death knight': 'deathknight',
    'demon hunter': 'demonhunter',
}


def normalize_battlenet_class_name(value):
    normalized = ' '.join(str(value or '').strip().lower().replace('_', ' ').split())
    return BATTLETNET_CLASS_SLUGS.get(normalized, normalized.replace(' ', ''))

SLOT_LABELS = {
    'head': '头盔', 'neck': '项链', 'shoulder': '肩甲', 'back': '披风', 'chest': '胸甲',
    'shirt': '衬衫', 'tabard': '战袍', 'wrist': '护腕', 'hands': '手套', 'waist': '腰带',
    'legs': '腿甲', 'feet': '靴子', 'finger1': '戒指1', 'finger2': '戒指2',
    'trinket1': '饰品1', 'trinket2': '饰品2', 'main_hand': '主手', 'off_hand': '副手',
}
EQUIPMENT_SLOTS = set(SLOT_LABELS)
EQUIPMENT_SLOT_ALIASES = {'shoulders': 'shoulder', 'wrists': 'wrist'}
SUPPORTED_ACTORS = {
    'warrior', 'paladin', 'hunter', 'rogue', 'priest', 'deathknight', 'shaman',
    'mage', 'warlock', 'monk', 'druid', 'demonhunter', 'evoker',
}
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


def authoritative_player_baseline(player_equipment):
    """Return only the equipped player block, excluding exported alternatives."""
    lines = []
    for raw_line in str(player_equipment or '').splitlines():
        if re.match(r'^\s*###\s+(?:Gear from Bags|Weekly Reward Choices)\b', raw_line, re.IGNORECASE):
            break
        lines.append(raw_line)
    return '\n'.join(lines).strip()


def validate_player_baseline(player_equipment):
    """Validate one frozen, directly executable player/equipment block.

    Attribute search accepts exported player state, not arbitrary SimC programs.  Keep
    comments, identity/talent fields and equipped slots, while rejecting imports,
    profilesets, action lists and other execution-changing directives.
    """
    baseline = authoritative_player_baseline(player_equipment)
    actors = []
    slots = {}
    scalar_keys = set()
    allowed_scalars = {
        'level', 'race', 'region', 'server', 'realm', 'role', 'position',
        'professions', 'spec', 'talents', 'talent', 'omnium_talents',
        'flask', 'food', 'potion', 'augmentation', 'temporary_enchant',
        'gear_strength', 'gear_crit', 'gear_haste', 'gear_mastery',
        'gear_versatility', 'gear_crit_rating', 'gear_haste_rating',
        'gear_mastery_rating', 'gear_versatility_rating',
    }
    actor_pattern = re.compile(
        r'^(' + '|'.join(sorted(SUPPORTED_ACTORS)) + r')$', re.IGNORECASE,
    )
    for raw_line in baseline.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        key, raw_value, values = _parse_line(line)
        if not key:
            raise ValueError('冻结玩家装备基线包含无法识别的SimC指令')
        if actor_pattern.fullmatch(key):
            if not (raw_value.startswith('"') and raw_value.endswith('"') and len(raw_value) > 2):
                raise ValueError('冻结玩家装备基线中的玩家角色格式无效')
            actors.append(key.lower())
        elif key in EQUIPMENT_SLOTS or key in EQUIPMENT_SLOT_ALIASES:
            canonical_slot = EQUIPMENT_SLOT_ALIASES.get(key, key)
            if canonical_slot in slots:
                raise ValueError(f'冻结玩家装备基线包含重复装备槽位: {canonical_slot}')
            item_id = _number(values.get('id'))
            if not item_id:
                raise ValueError(f'冻结玩家装备基线装备槽位缺少物品ID: {canonical_slot}')
            slots[canonical_slot] = item_id
        elif key in allowed_scalars:
            scalar_keys.add(key)
        else:
            raise ValueError(f'冻结玩家装备基线包含不允许执行的SimC指令: {key}')
    if len(actors) != 1:
        raise ValueError('冻结玩家装备基线必须包含一个受支持的玩家角色')
    if 'level' not in scalar_keys or 'spec' not in scalar_keys:
        raise ValueError('冻结玩家装备基线必须包含角色等级和专精')
    if 'main_hand' not in slots or len(slots) < 2:
        raise ValueError('冻结玩家装备基线必须包含主手及至少一个其他已装备物品槽位')
    return baseline


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


CRAFTED_STAT_LABELS = {'32': '精通', '36': '全能', '40': '暴击', '49': '急速'}


def _comment_item_hint(line):
    match = re.match(r'^#\s*(.+?)\s*\((\d+)\)\s*$', line)
    return (match.group(1), _number(match.group(2))) if match else ('', None)


def _parse_professions(raw_value):
    result = {}
    for entry in raw_value.split('/'):
        name, _, level = entry.partition('=')
        if name and _number(level) is not None:
            result[name.strip()] = _number(level)
    return result


def parse_manual_player_config(player_equipment, spec):
    """Parse an exported SimC player block without fetching external data.

    The exporter puts the authoritative equipped items before ``### Gear from Bags``;
    commented bag/reward alternatives are intentionally excluded.
    """
    requested_spec = str(spec or '').strip().lower()
    parsed = {
        'identity': {'name': '', 'class_name': SPEC_CLASS.get(requested_spec, ''), 'spec': requested_spec,
                     'race': '', 'level': None, 'region': '', 'realm': '', 'role': '', 'professions': {}},
        'talents': {'build_code': '', 'saved_loadouts': []}, 'omnium_talents': [],
        'equipment': [], 'stats': {'primary': {}, 'secondary': {}}, 'raw_fields': {}, 'missing_fields': [],
    }
    item_ids, equipment_rows = set(), []
    item_hint, in_bag_section, saved_loadout = ('', None), False, None
    for raw_line in str(player_equipment or '').splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith('### Gear from Bags') or line.startswith('### Weekly Reward Choices'):
            in_bag_section = True
            continue
        if line.startswith('#'):
            label = line[1:].strip()
            if label.startswith('Saved Loadout:'):
                saved_loadout = {'name': label.partition(':')[2].strip(), 'build_code': ''}
                parsed['talents']['saved_loadouts'].append(saved_loadout)
                continue
            if label.startswith('talents=') and saved_loadout is not None:
                saved_loadout['build_code'] = label.partition('=')[2].strip()
                continue
            item_hint = _comment_item_hint(line)
            continue
        if in_bag_section:
            continue
        key, raw_value, values = _parse_line(line)
        if not key:
            continue
        if re.match(r'^([a-z_]+)$', key) and raw_value.startswith('"') and raw_value.endswith('"'):
            parsed['identity']['class_name'], parsed['identity']['name'] = key, raw_value[1:-1]
        elif key in ('spec', 'race', 'region', 'role'):
            parsed['identity'][key] = raw_value
        elif key in ('server', 'realm'):
            parsed['identity']['realm'] = raw_value
        elif key == 'level':
            parsed['identity']['level'] = _number(raw_value)
        elif key == 'professions':
            parsed['identity']['professions'] = _parse_professions(raw_value)
        elif key in ('talents', 'talent'):
            if saved_loadout is not None:
                saved_loadout['build_code'] = raw_value
            else:
                parsed['talents']['build_code'] = raw_value
        elif key == 'omnium_talents':
            parsed['omnium_talents'] = [
                {'id': _number(entry.partition(':')[0]), 'rank': _number(entry.partition(':')[2])}
                for entry in raw_value.split('/') if _number(entry.partition(':')[0])
            ]
        elif key in EQUIPMENT_SLOTS or key in EQUIPMENT_SLOT_ALIASES:
            canonical_slot = EQUIPMENT_SLOT_ALIASES.get(key, key)
            for item_id in [values.get('id'), values.get('enchant_id'), values.get('gem_id')]:
                if _number(item_id): item_ids.add(_number(item_id))
            for gem_id in re.split(r'[/;:]', values.get('gems', '')):
                if _number(gem_id): item_ids.add(_number(gem_id))
            equipment_rows.append((canonical_slot, values, raw_value, item_hint))
        else:
            parsed['raw_fields'][key] = raw_value
        item_hint = ('', None)

    snapshots = {int(row.item_id): row for row in WowItemSnapshot.objects.filter(item_id__in=item_ids)}
    for slot, values, raw_value, hint in equipment_rows:
        item = _item_meta(values.get('id'), snapshots)
        if hint[0] and item['display_name'].startswith('#'):
            item['display_name'], item['export_name'] = hint[0], hint[0]
        enchant = _item_meta(values.get('enchant_id'), snapshots) if values.get('enchant_id') else None
        gem_ids = ([values['gem_id']] if values.get('gem_id') else []) + [x for x in re.split(r'[/;:]', values.get('gems', '')) if x]
        crafted = [CRAFTED_STAT_LABELS.get(value, value) for value in re.split(r'[/;:]', values.get('crafted_stats', '')) if value]
        parsed['equipment'].append({
            **item, 'slot': slot, 'slot_label': SLOT_LABELS[slot], 'item_level': hint[1] or _number(values.get('ilevel') or values.get('item_level')),
            'enchant': enchant, 'gems': [_item_meta(gem_id, snapshots) for gem_id in gem_ids],
            'bonus_ids': [value for value in re.split(r'[/;:]', values.get('bonus_id', '') or values.get('bonus_ids', '')) if value],
            'content_tuning': values.get('content_tuning', ''), 'crafted_stats': crafted,
            'crafting_quality': _number(values.get('crafting_quality')), 'raw_value': raw_value,
        })
    for stat in PRIMARY:
        value = _number(parsed['raw_fields'].get(stat))
        if value is not None: parsed['stats']['primary'][stat] = value
    return parsed


def parse_manual_simc_candidates(player_equipment):
    """Extract exporter alternatives without changing the equipped-player parser."""
    result = {'base_talent': '', 'gear_candidates': [], 'talent_candidates': []}
    section = ''
    hint_name, hint_level = '', None
    saved_loadout = ''
    for raw_line in str(player_equipment or '').splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith('### Gear from Bags'):
            section, saved_loadout = 'bags', ''
            continue
        if line.startswith('### Weekly Reward Choices'):
            section, saved_loadout = 'weekly_reward', ''
            continue
        if line.startswith('#'):
            label = line[1:].strip()
            if label.startswith('Saved Loadout:'):
                saved_loadout = label.partition(':')[2].strip()
                continue
            if label.startswith('talents=') and saved_loadout:
                talent = label.partition('=')[2].strip()
                if talent:
                    result['talent_candidates'].append({'name': saved_loadout, 'talent': talent, 'source': 'saved_loadout'})
                continue
            hint_name, hint_level = _comment_item_hint(line)
            continue
        key, raw_value, values = _parse_line(line)
        if key in ('talents', 'talent') and not section and not result['base_talent']:
            result['base_talent'] = raw_value
        elif section and (key in EQUIPMENT_SLOTS or key in EQUIPMENT_SLOT_ALIASES) and _number(values.get('id')):
            canonical_slot = EQUIPMENT_SLOT_ALIASES.get(key, key)
            result['gear_candidates'].append({
                'slot': canonical_slot, 'item_id': _number(values.get('id')), 'source': section,
                'raw_value': raw_value, 'name': hint_name,
                'item_level': hint_level or _number(values.get('ilevel') or values.get('item_level')),
            })
        hint_name, hint_level = '', None
    return result


def build_player_config_detail(mode, spec, player_equipment='', battlenet_region='', battlenet_realm='', battlenet_character='',
                               talent='', gear_strength=None, gear_crit=None, gear_haste=None, gear_mastery=None, gear_versatility=None):
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

    if mode == 'attribute_only':
        detail = parse_manual_player_config(player_equipment, spec)
        class_name = detail['identity']['class_name'] or SPEC_CLASS.get(spec, '')
        rule = SimcSecondaryStatRule.objects.filter(class_name=class_name).first()
        mastery = SimcMasteryCoefficient.objects.filter(spec=detail['identity']['spec'] or spec).first()
        conversion = {
            'crit': getattr(rule, 'crit_per_percent', None),
            'haste': getattr(rule, 'haste_per_percent', None),
            'mastery': getattr(rule, 'mastery_per_percent', None),
            'versatility': getattr(rule, 'versatility_per_percent', None),
        }
        ratings = {
            'crit': _number(gear_crit), 'haste': _number(gear_haste),
            'mastery': _number(gear_mastery), 'versatility': _number(gear_versatility),
        }
        mastery_coefficient = getattr(mastery, 'mastery_coefficient', 1) or 1
        detail['source'] = {'type': 'attribute_only', 'label': '冻结玩家基线与绿字覆盖'}
        detail['identity']['class_name'] = class_name
        detail['identity']['spec'] = detail['identity']['spec'] or spec
        detail['talents']['build_code'] = talent or detail['talents']['build_code']
        detail['stats'] = {
            'primary': {'strength': _number(gear_strength)}, 'secondary': {
                stat: _secondary_stat_detail(rating, conversion.get(stat), mastery_coefficient if stat == 'mastery' else 1)
                for stat, rating in ratings.items()
            },
        }
        detail['missing_fields'] = []
        if not player_equipment.strip():
            detail['missing_fields'].append('历史配置未保存冻结玩家装备基线；不能发起新的属性模拟。')
        elif not detail['equipment']:
            detail['missing_fields'].append('冻结玩家基线未解析到装备槽位。')
        return detail

    detail = parse_manual_player_config(player_equipment, spec)
    detail['source'] = {'type': 'manual_equipment', 'label': '手动 SimC 玩家配置'}
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
