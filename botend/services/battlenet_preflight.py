"""Battle.net 角色预检：验证实时角色并生成可离线复用的完整 SimC 玩家快照。"""
from __future__ import annotations

from collections import defaultdict
from urllib.parse import quote

import requests
from django.conf import settings

from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
from botend.services.simc_player_config import SLOT_LABELS, SPEC_CLASS, normalize_battlenet_class_name
from botend.wow.talents.build_code import TalentBuildCodeDecoder
from botend.wow.talents.metadata import TalentMetadataProvider
from botend.wow.talents.service import TalentBuildCodeService


_REGION_CONFIG = {
    'us': ('https://us.api.blizzard.com', 'profile-us', 'en_US'),
    'eu': ('https://eu.api.blizzard.com', 'profile-eu', 'en_GB'),
    'kr': ('https://kr.api.blizzard.com', 'profile-kr', 'ko_KR'),
    'tw': ('https://tw.api.blizzard.com', 'profile-tw', 'zh_TW'),
}
_SIMC_SLOT_BY_BATTLENET_TYPE = {
    'HEAD': 'head', 'NECK': 'neck', 'SHOULDER': 'shoulder', 'BACK': 'back',
    'CHEST': 'chest', 'SHIRT': 'shirt', 'TABARD': 'tabard', 'WRIST': 'wrist',
    'HANDS': 'hands', 'WAIST': 'waist', 'LEGS': 'legs', 'FEET': 'feet',
    'FINGER_1': 'finger1', 'FINGER_2': 'finger2',
    'TRINKET_1': 'trinket1', 'TRINKET_2': 'trinket2',
    'MAIN_HAND': 'main_hand', 'OFF_HAND': 'off_hand',
}


def _character_slug(value):
    return quote(str(value or '').strip().lower().replace("'", '').replace(' ', '-'), safe='-')


def _token():
    cfg = getattr(settings, 'BATTLENET_CONFIG', {}) or {}
    client_id, client_secret = cfg.get('client_id'), cfg.get('client_secret')
    if not client_id or not client_secret:
        raise ValueError('Battle.net API 尚未配置，无法获取角色配置')
    response = requests.post(
        cfg.get('token_url', 'https://oauth.battle.net/token'),
        data={'grant_type': 'client_credentials'}, auth=(client_id, client_secret), timeout=20,
    )
    if response.status_code != 200:
        raise ValueError(f'Battle.net 授权失败（HTTP {response.status_code}）')
    token = (response.json() or {}).get('access_token')
    if not token:
        raise ValueError('Battle.net 授权响应缺少 access_token')
    return token


def _api_get(host, path, namespace, locale, token):
    response = requests.get(
        f'{host}{path}', params={'namespace': namespace, 'locale': locale},
        headers={'Authorization': f'Bearer {token}'}, timeout=25,
    )
    if response.status_code == 404:
        raise ValueError('未找到该 Battle.net 角色，请检查地区、服务器和角色名')
    if response.status_code != 200:
        raise ValueError(f'Battle.net 角色查询失败（HTTP {response.status_code}）')
    return response.json() or {}


def _simc_token(value):
    return ''.join(
        ch for ch in str(value or '').strip().lower().replace(' ', '_')
        if ch.isalnum() or ch == '_'
    )


def _spec_key(profile):
    normalized = _simc_token((profile.get('active_spec') or {}).get('name', ''))
    aliases = {'beastmaster': 'beast_mastery', 'beastmastery': 'beast_mastery'}
    return aliases.get(normalized, normalized)


def _canonicalize_talent_loadout(loadout, *, class_name, spec_name):
    """Rebuild a Battle.net loadout and restore omitted granted hero roots.

    Battle.net Saved Loadouts can omit the automatically granted hero-tree root
    from both ``selected_hero_talents`` and the import string.  The remaining
    hero nodes still identify the active subtree, so restore its canonical
    parentless root as selected-but-not-purchased before freezing the code.
    """
    loadout = loadout or {}
    reference = str(loadout.get('talent_loadout_code') or '').strip()
    selected_groups = (
        ('class', loadout.get('selected_class_talents') or []),
        ('spec', loadout.get('selected_spec_talents') or []),
        ('hero', loadout.get('selected_hero_talents') or []),
    )
    if not any(rows for _, rows in selected_groups):
        return reference

    try:
        decoder_nodes = TalentMetadataProvider().get_decoder_node_list(class_name)
        decoder_by_id = {
            int(node['talent_id']): node for node in decoder_nodes
            if node.get('talent_id')
        }
        selected_nodes = []
        selected_ids = set()
        reference_states = TalentBuildCodeDecoder.decode_node_states(
            reference, decoder_nodes,
        )
        reference_by_talent_id = {}
        for canonical in decoder_nodes:
            talent_id = canonical.get('talent_id')
            if not talent_id:
                continue
            state = reference_states.get(
                f"{canonical.get('tree_type') or 'spec'}:{canonical.get('node_id') or talent_id}"
            )
            if state:
                reference_by_talent_id[int(talent_id)] = state
        for tree_type, rows in selected_groups:
            for row in rows:
                talent_id = row.get('id') if isinstance(row, dict) else None
                try:
                    talent_id = int(talent_id)
                    points = int(row.get('rank') or 0)
                except (TypeError, ValueError):
                    continue
                canonical = decoder_by_id.get(talent_id)
                if not canonical or points <= 0:
                    continue
                reference_state = reference_by_talent_id.get(talent_id) or {}
                node = {
                    'talent_id': talent_id,
                    'tree_type': canonical.get('tree_type') or tree_type,
                    'points': points,
                    'selected': True,
                    'purchased': reference_state.get(
                        'purchased', not bool(row.get('default_points')),
                    ),
                }
                if (
                    canonical.get('choice_options')
                    and canonical.get('is_choice_node') is not False
                    and reference_state.get('is_choice_node')
                ):
                    node['choice_selection'] = int(reference_state.get('choice_selection') or 0)
                if canonical.get('db2_subtree_id'):
                    node['db2_subtree_id'] = canonical['db2_subtree_id']
                selected_nodes.append(node)
                selected_ids.add(talent_id)

        active_subtree = 0
        subtree_points = defaultdict(int)
        for node in selected_nodes:
            if node.get('tree_type') == 'hero' and node.get('db2_subtree_id'):
                subtree_points[int(node['db2_subtree_id'])] += int(node.get('points') or 0)
        if subtree_points:
            active_subtree = max(subtree_points, key=subtree_points.get)
        if not active_subtree:
            active_subtree = (loadout.get('selected_hero_talent_tree') or {}).get('id')
            try:
                active_subtree = int(active_subtree)
            except (TypeError, ValueError):
                active_subtree = 0
        if active_subtree:
            roots = [
                node for node in decoder_nodes
                if (node.get('tree_type') or '') == 'hero'
                and int(node.get('db2_subtree_id') or 0) == active_subtree
                and not (node.get('parents') or [])
            ]
            if len(roots) == 1 and int(roots[0].get('talent_id') or 0) not in selected_ids:
                selected_nodes.append({
                    'talent_id': int(roots[0]['talent_id']),
                    'tree_type': 'hero',
                    'db2_subtree_id': active_subtree,
                    'points': 1,
                    'selected': True,
                    'purchased': False,
                })

        canonical = TalentBuildCodeService.encode_build_code_from_nodes(
            selected_nodes,
            class_name=class_name,
            spec_name=spec_name,
            reference_build_code=reference,
        )
        return canonical or reference
    except Exception:
        return reference


def _talent_loadouts(payload, spec_key, class_name=''):
    rows = payload.get('specializations') or []
    active_id = (payload.get('active_specialization') or {}).get('id')
    candidates = []
    for row in rows:
        specialization = row.get('specialization') or {}
        if active_id and specialization.get('id') == active_id:
            candidates.insert(0, row)
        elif _simc_token(specialization.get('name')) == spec_key:
            candidates.append(row)
    for row in candidates:
        loadouts = row.get('loadouts') or []
        active = next((loadout for loadout in loadouts if loadout.get('is_active')), None)
        active = active or (loadouts[0] if len(loadouts) == 1 else None)
        active_code = _canonicalize_talent_loadout(
            active, class_name=class_name, spec_name=spec_key,
        ) if active else ''
        alternatives = []
        seen_codes = {active_code} if active_code else set()
        for index, loadout in enumerate(loadouts, start=1):
            code = _canonicalize_talent_loadout(
                loadout, class_name=class_name, spec_name=spec_key,
            )
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            alternatives.append({
                'name': str((loadout or {}).get('name') or f'天赋方案 {index}').strip(),
                'build_code': code,
            })
        if active_code or alternatives:
            return active_code, alternatives
    return '', []


def _active_talent_loadout_code(payload, spec_key, class_name=''):
    return _talent_loadouts(payload, spec_key, class_name)[0]


def _equipment_line(item):
    slot = _SIMC_SLOT_BY_BATTLENET_TYPE.get(str((item.get('slot') or {}).get('type') or '').upper())
    item_id = (item.get('item') or {}).get('id')
    if not slot or not item_id:
        return ''
    parts = [f'{slot}=,id={int(item_id)}']
    bonus_ids = [str(int(value)) for value in (item.get('bonus_list') or []) if isinstance(value, (int, float))]
    if bonus_ids:
        parts.append(f'bonus_id={"/".join(bonus_ids)}')
    enchant_ids = [
        str(int(row['enchantment_id'])) for row in (item.get('enchantments') or [])
        if isinstance(row, dict) and isinstance(row.get('enchantment_id'), (int, float))
    ]
    if enchant_ids:
        parts.append(f'enchant_id={"/".join(enchant_ids)}')
    gem_ids = [
        str(int((row.get('item') or {})['id'])) for row in (item.get('sockets') or [])
        if isinstance(row, dict) and isinstance((row.get('item') or {}).get('id'), (int, float))
    ]
    if gem_ids:
        parts.append(f'gem_id={"/".join(gem_ids)}')
    return ','.join(parts)


def _display_name(payload, fallback_id, *keys):
    if not isinstance(payload, dict):
        return f'#{fallback_id}'
    for key in keys or ('name',):
        if payload.get(key):
            return str(payload[key])
    return f'#{fallback_id}'


def _equipment_detail(item):
    slot = _SIMC_SLOT_BY_BATTLENET_TYPE.get(str((item.get('slot') or {}).get('type') or '').upper())
    item_id = (item.get('item') or {}).get('id')
    if not slot or not isinstance(item_id, (int, float)):
        return None
    enchantments = [
        row for row in (item.get('enchantments') or [])
        if isinstance(row, dict) and isinstance(row.get('enchantment_id'), (int, float))
    ]
    gems = []
    for row in item.get('sockets') or []:
        gem = row.get('item') or {} if isinstance(row, dict) else {}
        gem_id = gem.get('id')
        if isinstance(gem_id, (int, float)):
            gems.append({
                'id': int(gem_id),
                'display_name': _display_name(row, int(gem_id), 'display_string', 'name'),
            })
    enchant = enchantments[0] if enchantments else None
    enchant_id = int(enchant['enchantment_id']) if enchant else None
    return {
        'id': int(item_id),
        'display_name': _display_name(item, int(item_id)),
        'slot': slot,
        'slot_label': SLOT_LABELS[slot],
        'item_level': (item.get('level') or {}).get('value'),
        'enchant': ({
            'id': enchant_id,
            'display_name': _display_name(enchant, enchant_id, 'display_string', 'name'),
        } if enchant else None),
        'gems': gems,
        'bonus_ids': [int(value) for value in (item.get('bonus_list') or []) if isinstance(value, (int, float))],
    }


def _build_player_snapshot(profile, items, class_name, spec_key, talent):
    actor_name = str(profile.get('name') or 'BattleNetPlayer').replace('"', '')
    lines = [f'{class_name}="{actor_name}"']
    if profile.get('level'):
        lines.append(f'level={int(profile["level"])}')
    race = _simc_token((profile.get('race') or {}).get('name'))
    if race:
        lines.append(f'race={race}')
    lines.append(f'spec={spec_key}')
    if talent:
        lines.append(f'talents={talent}')
    lines.extend(line for line in (_equipment_line(item) for item in items) if line)
    return '\n'.join(lines)


def fetch_battlenet_character_preflight(*, region, realm, character, requested_spec=''):
    """Fetch live data once and return a complete immutable execution snapshot."""
    region = str(region or '').strip().lower()
    if region == 'cn':
        raise ValueError('国服角色无法通过 Battle.net 加载，请改用 SimC Addon 导入')
    if region not in _REGION_CONFIG:
        raise ValueError('Battle.net region 必须是 us、eu、kr 或 tw')
    realm, character = str(realm or '').strip(), str(character or '').strip()
    if not realm or not character:
        raise ValueError('请填写 Battle.net 服务器和角色名')

    host, namespace, locale = _REGION_CONFIG[region]
    token = _token()
    base_path = f'/profile/wow/character/{_character_slug(realm)}/{_character_slug(character)}'
    profile = _api_get(host, base_path, namespace, locale, token)
    equipment_payload = _api_get(host, f'{base_path}/equipment', namespace, locale, token)
    stats_payload = _api_get(host, f'{base_path}/statistics', namespace, locale, token)
    stats = SpecDetailBase(None, None).parse_battlenet_stats(stats_payload) or {}

    class_name = normalize_battlenet_class_name((profile.get('character_class') or {}).get('name'))
    spec_key = _spec_key(profile)
    requested_spec = str(requested_spec or '').strip().lower()
    items = equipment_payload.get('equipped_items') or []
    specializations_payload = {}
    # Real equipment rows carry item identities. Keeping this condition also preserves
    # compatibility with older diagnostic fixtures that only provide item level.
    if any((row.get('item') or {}).get('id') for row in items if isinstance(row, dict)):
        specializations_payload = _api_get(host, f'{base_path}/specializations', namespace, locale, token)
    talent, saved_loadouts = _talent_loadouts(specializations_payload, spec_key, class_name)
    player_snapshot = _build_player_snapshot(profile, items, class_name, spec_key, talent)

    item_levels = [row.get('level', {}).get('value') for row in items if isinstance(row, dict)]
    item_levels = [int(value) for value in item_levels if isinstance(value, (int, float))]
    equipment_details = [detail for detail in (_equipment_detail(row) for row in items if isinstance(row, dict)) if detail]
    equipment_summary = {
        'count': len(items),
        'item_level': round(sum(item_levels) / len(item_levels)) if item_levels else None,
    }
    warnings = []
    if not items:
        warnings.append('角色没有可用的已装备物品，不能启动 SimC。')
    if requested_spec and not spec_key:
        warnings.append('无法识别该角色当前 Battle.net 专精，不能确认与目标专精一致。')
    elif requested_spec and requested_spec != spec_key:
        warnings.append(f'当前 Battle.net 专精为 {spec_key}，与选择的 {requested_spec} 不一致；请切换角色专精或重新选择。')
    expected_class = SPEC_CLASS.get(requested_spec, '')
    if expected_class and not class_name:
        warnings.append('无法识别该角色职业，不能确认与目标专精一致。')
    elif expected_class and expected_class != class_name:
        warnings.append(f'选择的专精 {requested_spec} 不属于该角色职业 {class_name}。')

    primary = {stat: stats[stat] for stat in ('strength', 'agility', 'intellect', 'stamina') if stats.get(stat) is not None}
    secondary = {key: value for key, value in stats.items() if key in ('crit', 'haste', 'mastery', 'versatility')}
    gear_strength = primary.get('strength') or primary.get('agility') or primary.get('intellect') or 0
    simc_config = {
        'player_config_mode': 'battlenet', 'battlenet_region': region,
        'battlenet_realm': ((profile.get('realm') or {}).get('name') or realm),
        'battlenet_character': (profile.get('name') or character),
        'spec': spec_key, 'talent': talent, 'player_equipment': player_snapshot,
        'gear_strength': gear_strength,
        **{f'gear_{name}': (secondary.get(name) or {}).get('rating') or 0
           for name in ('crit', 'haste', 'mastery', 'versatility')},
    }
    return {
        'identity': {
            'name': (profile.get('name') or character),
            'realm': ((profile.get('realm') or {}).get('name') or realm),
            'region': region, 'class_name': class_name, 'level': profile.get('level'),
        },
        'spec': {'key': spec_key, 'name': (profile.get('active_spec') or {}).get('name', '')},
        'talents': {'build_code': talent, 'saved_loadouts': saved_loadouts},
        'comparison_candidates': {
            'default_talent': ({
                'name': '默认天赋', 'talent': talent, 'source': 'battlenet_active',
            } if talent else None),
            'talents': [
                {'name': row['name'], 'talent': row['build_code'], 'source': 'battlenet_loadout'}
                for row in saved_loadouts
            ],
            'gear': [],
        },
        'equipment': equipment_details,
        'equipment_summary': equipment_summary,
        'stats': {'primary': primary, 'secondary': secondary},
        'simc_ready': not warnings,
        'warnings': warnings,
        'simc_config': simc_config,
    }
