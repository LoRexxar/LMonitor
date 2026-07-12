"""Battle.net 角色预检：在保存/启动 SimC 前验证角色身份、专精和装备可用性。"""
from __future__ import annotations

from urllib.parse import quote

import requests
from django.conf import settings

from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
from botend.services.simc_player_config import SPEC_CLASS


_REGION_CONFIG = {
    'us': ('https://us.api.blizzard.com', 'profile-us', 'en_US'),
    'eu': ('https://eu.api.blizzard.com', 'profile-eu', 'en_GB'),
    'kr': ('https://kr.api.blizzard.com', 'profile-kr', 'ko_KR'),
    'tw': ('https://tw.api.blizzard.com', 'profile-tw', 'zh_TW'),
    'cn': ('https://gateway.battlenet.com.cn', 'profile-cn', 'zh_CN'),
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


def _spec_key(profile):
    spec = (profile.get('active_spec') or {}).get('name', '')
    # API returns localised names; only English namespaces can reliably map by display name.
    normalized = ''.join(ch for ch in str(spec).lower() if ch.isalnum() or ch == '_')
    aliases = {'beastmaster': 'beast_mastery', 'beastmastery': 'beast_mastery'}
    return aliases.get(normalized, normalized)


def fetch_battlenet_character_preflight(*, region, realm, character, requested_spec=''):
    """Fetch current character profile and return a safe structured SimC-readiness summary."""
    region = str(region or '').strip().lower()
    if region not in _REGION_CONFIG:
        raise ValueError('Battle.net region 必须是 us、eu、kr、tw 或 cn')
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

    class_name = str((profile.get('character_class') or {}).get('name') or '').lower()
    spec_key = _spec_key(profile)
    requested_spec = str(requested_spec or '').strip().lower()
    items = (equipment_payload.get('equipped_items') or [])
    item_levels = [row.get('level', {}).get('value') for row in items if isinstance(row, dict)]
    item_levels = [int(value) for value in item_levels if isinstance(value, (int, float))]
    warnings = []
    if not items:
        warnings.append('角色没有可用的已装备物品，不能启动 SimC。')
    if requested_spec and spec_key and requested_spec != spec_key:
        warnings.append(f'当前 Battle.net 专精为 {spec_key}，与选择的 {requested_spec} 不一致；请切换角色专精或重新选择。')
    expected_class = SPEC_CLASS.get(requested_spec, '')
    if expected_class and class_name and expected_class != class_name:
        warnings.append(f'选择的专精 {requested_spec} 不属于该角色职业 {class_name}。')
    # Armory execution asks SimC to import the active Battle.net build. The API does
    # not expose a portable export string, so the editable Profile still stores only
    # the armory identity; make that boundary explicit to callers.

    primary = {}
    for stat in ('strength', 'agility', 'intellect', 'stamina'):
        if stats.get(stat) is not None:
            primary[stat] = stats[stat]
    secondary = {key: value for key, value in stats.items() if key in ('crit', 'haste', 'mastery', 'versatility')}
    gear_strength = primary.get('strength') or primary.get('agility') or primary.get('intellect') or 0
    simc_config = {
        'player_config_mode': 'battlenet', 'battlenet_region': region,
        'battlenet_realm': ((profile.get('realm') or {}).get('name') or realm),
        'battlenet_character': (profile.get('name') or character),
        'spec': spec_key, 'talent': '', 'gear_strength': gear_strength,
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
        'equipment': {'count': len(items), 'item_level': round(sum(item_levels) / len(item_levels)) if item_levels else None},
        'stats': {'primary': primary, 'secondary': secondary},
        # Character/equipment/statistics were fetched from Battle.net; armory mode
        # obtains the active build during actual SimC execution.
        'simc_ready': not warnings,
        'warnings': warnings,
        'simc_config': simc_config,
    }
