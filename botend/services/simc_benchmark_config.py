"""Validation, transactional persistence and execution planning for SimC benchmarks.

The public snapshots in this module deliberately contain only resource ids and display
metadata. Executable resource bodies remain behind the existing task/version service.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from copy import deepcopy
from typing import NoReturn

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import IntegrityError, transaction
from django.db.models import Prefetch, Q
from django.utils.text import slugify

from botend.constants.wow import SPEC_CN
from botend.models import (
    SimcApl, SimcBackendBinary, SimcBenchmarkCandidate, SimcBenchmarkPanel,
    SimcBenchmarkProfile, SimcBenchmarkScenario, SimcBenchmarkSpec,
    SimcContentTemplate, SimcProfile, SimcTalentString, WowItemSnapshot,
)
from botend.services.simc_player_config import (
    EQUIPMENT_SLOTS, EQUIPMENT_SLOT_ALIASES, canonical_simc_profile_identity,
    canonical_simc_spec_identity,
    is_supported_simc_spec_identity, normalize_battlenet_class_name,
    normalize_gear_candidate_value,
    SUPPORTED_SIMC_SPEC_IDENTITIES,
)
from botend.services.simc_task_service import (
    SIMULATION_PARAMS_WHITELIST, TaskCreationError,
)
from botend.services.simc_composer import (
    SIMC_EXTRA_OPTIONS, SIMC_RAID_BUFF_VALUES, validate_simulation_options,
)
from botend.services.simc_candidate_options import normalize_controlled_simc_options

MAX_SPECS = len(SUPPORTED_SIMC_SPEC_IDENTITIES)
MAX_PROFILES_PER_SPEC = 5
MAX_SCENARIOS = 8


def benchmark_profile_key(profile_id, talent_string_id=None):
    """Return the durable coordinate key for one Profile/talent variant."""
    if talent_string_id is None:
        return str(profile_id)
    return f'{profile_id}:talent:{talent_string_id}'

# SimulationCraft commit 32ceb18d81557965afa5e240dc32b8659549c53d,
# engine/util/util.cpp::parse_fight_style(). Keep values byte-for-byte identical.
SIMC_FIGHT_STYLES = (
    ('Patchwerk', 'Patchwerk（木桩）'),
    ('CastingPatchwerk', 'CastingPatchwerk（施法木桩）'),
    ('HecticAddCleave', 'HecticAddCleave（高频小怪顺劈）'),
    ('DungeonSlice', 'DungeonSlice（地下城切片）'),
    ('DungeonRoute', 'DungeonRoute（地下城路线）'),
    ('CleaveAdd', 'CleaveAdd（周期小怪顺劈）'),
    ('LightMovement', 'LightMovement（轻度移动）'),
    ('HeavyMovement', 'HeavyMovement（重度移动）'),
    ('beastlord', 'beastlord（兽王达玛克）'),
    ('HelterSkelter', 'HelterSkelter（混乱战斗）'),
    ('Ultraxion', 'Ultraxion（奥卓克希昂）'),
)
SIMC_FIGHT_STYLE_VALUES = frozenset(value for value, _label in SIMC_FIGHT_STYLES)

SIMC_RAID_BUFFS = (
    ('arcane_intellect', '奥术智慧'),
    ('battle_shout', '战斗怒吼'),
    ('mark_of_the_wild', '野性印记'),
    ('power_word_fortitude', '真言术：韧'),
    ('skyfury', '天怒'),
    ('chaos_brand', '混乱烙印'),
    ('mystic_touch', '秘法之触'),
    ('hunters_mark', '猎人印记'),
    ('mortal_wounds', '致死重伤'),
    ('bleeding', '流血'),
    ('bloodlust', '嗜血 / 英勇'),
)
assert tuple(value for value, _label in SIMC_RAID_BUFFS) == SIMC_RAID_BUFF_VALUES
MAX_GEAR_RAW_VALUE_CHARS = 2048
MAX_CANDIDATE_PARAMS_BYTES = 16 * 1024
MAX_PANEL_CONFIG_BYTES = 2 * 1024 * 1024

_PANEL_FIELDS = {
    'name', 'slug', 'description', 'benchmark_type', 'comparison_option',
    'comparison_config',
    'is_active', 'is_public', 'schedule_enabled', 'interval_seconds', 'next_run_at',
    'queue_priority',
}
BENCHMARK_QUEUE_PRIORITIES = frozenset((10, 20, 30))
_EXTRA_OPTION_BY_VALUE = {option['value']: option for option in SIMC_EXTRA_OPTIONS}
_ITEM_OPTION_KEYS = {
    'id', 'ilevel', 'item_level', 'bonus_id', 'bonus_ids', 'gem_id', 'gems',
    'enchant_id', 'crafted_stats', 'crafting_quality', 'drop_level',
    'content_tuning', 'suffix', 'upgrade',
}
_SAFE_KEY = re.compile(r'^[a-z0-9][a-z0-9_-]{0,99}$')


def _profile_class_name(profile):
    """Accept both canonical class names and legacy ``class_spec`` values."""
    profile_class, _profile_spec = canonical_simc_profile_identity(profile.spec, profile.class_name)
    return profile_class or normalize_battlenet_class_name(profile.class_name)


def benchmark_resource_access_q(kind, user_id=None):
    """Return Benchmark executable-state policy without user ownership scope."""
    if kind == 'backend':
        return Q(is_active=True)
    if kind == 'template':
        return Q(is_active=True, is_selectable=True)
    if kind == 'apl':
        # Personal APLs are executable once active. ``is_selectable`` is the
        # publication gate only for system APLs; Benchmark ignores ownership.
        return Q(is_active=True) & (Q(is_system=False) | Q(is_selectable=True))
    if kind == 'profile':
        return Q(is_active=True)
    if kind == 'talent':
        return Q(is_active=True, is_selectable=True)
    raise ValueError(f'unknown benchmark resource kind: {kind}')


def benchmark_resource_querysets(user_id=None):
    """Query all resources whose content state allows Benchmark execution."""
    models_by_name = {
        'backends': (SimcBackendBinary, 'backend'),
        'templates': (SimcContentTemplate, 'template'),
        'apls': (SimcApl, 'apl'),
        'profiles': (SimcProfile, 'profile'),
        'talent_strings': (SimcTalentString, 'talent'),
    }
    return {
        name: model.objects.filter(benchmark_resource_access_q(kind, user_id)).order_by('name', 'id')
        for name, (model, kind) in models_by_name.items()
    }


def resolve_default_benchmark_resources(spec_keys, user_id):
    """Resolve exactly one composer-compatible default resource set per spec."""
    querysets = benchmark_resource_querysets(user_id)

    def exactly_one(queryset, description):
        rows = list(queryset[:2])
        if len(rows) != 1:
            state = 'missing' if not rows else 'duplicate'
            _error(f'{description}: {state} default resource', 'resources')
        return rows[0]

    backend = exactly_one(
        querysets['backends'].filter(identifier='production'), 'production Backend',
    )
    resolved = {}
    for spec_key in spec_keys:
        if not is_supported_simc_spec_identity(spec_key):
            _error(f'{spec_key}: unsupported specialization', 'resources')
        expected_class, expected_spec = canonical_simc_spec_identity(spec_key)
        apl = exactly_one(querysets['apls'].filter(
            spec=spec_key, source=SimcApl.SOURCE_SIMC_UPSTREAM,
            is_system=True, owner_user_id__isnull=True,
        ), f'{spec_key} APL')
        template = exactly_one(
            querysets['templates'].filter(Q(spec=spec_key) | Q(spec__in=('default', 'all', '*'))),
            f'{spec_key} Template',
        )
        profile = exactly_one(querysets['profiles'].filter(
            user_id__isnull=True, source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            system_key=f'simc_upstream:{spec_key}', spec=spec_key,
        ), f'{spec_key} system Profile')
        talent = exactly_one(querysets['talent_strings'].filter(
            owner_user_id__isnull=True, is_system=True,
            system_key=f'simc_upstream:{spec_key}', spec=spec_key,
        ), f'{spec_key} system TalentString')
        if not _same_spec(apl.spec, expected_class, expected_spec):
            _error(f'{spec_key}: APL specialization mismatch', 'resources')
        if not _same_spec(template.spec, expected_class, expected_spec, allow_generic=True):
            _error(f'{spec_key}: Template specialization mismatch', 'resources')
        profile_class = _profile_class_name(profile)
        if profile_class and profile_class != expected_class:
            _error(f'{spec_key}: Profile class mismatch', 'resources')
        if not _same_profile_spec(profile, expected_class, expected_spec):
            _error(f'{spec_key}: Profile specialization mismatch', 'resources')
        resolved[spec_key] = {
            'apl': apl, 'template': template, 'backend': backend, 'profile': profile,
            'talent': talent,
        }
    return resolved


def _error(message, field=None) -> NoReturn:
    raise ValidationError({field: message} if field else message)


def _require_list(value, field):
    if not isinstance(value, list):
        _error(f'{field} 必须是列表', field)
    return value


def _require_dict(value, field):
    if not isinstance(value, dict):
        _error(f'{field} 必须是对象', field)
    return value


def _strict_bool(value, field, default=True):
    if value is None:
        return default
    if type(value) is not bool:
        _error(f'{field} 必须是布尔值', field)
    return value


def _order(value, field, default):
    if value is None:
        return default
    if type(value) is not int or value < 0:
        _error(f'{field} 必须是非负整数', field)
    return value


def _text(value, field, *, required=True, max_length=None):
    if not isinstance(value, str):
        _error(f'{field} 必须是字符串', field)
    result = value.strip()
    if required and not result:
        _error(f'{field} 不能为空', field)
    if max_length is not None and len(result) > max_length:
        _error(f'{field} 最长 {max_length} 个字符', field)
    return result


def _id(value, field):
    if type(value) is not int or value <= 0:
        _error(f'{field} 必须是正整数', field)
    return value


def _key(value, field):
    value = _text(value, field)
    if not _SAFE_KEY.fullmatch(value):
        _error(f'{field} 格式无效', field)
    return value


def _generated_scenario_key(name, occupied):
    """Derive a safe unique coordinate key; existing explicit keys stay unchanged."""
    base = (slugify(name) or 'scenario')[:100].rstrip('-') or 'scenario'
    candidate = base
    suffix = 2
    while candidate in occupied:
        marker = f'-{suffix}'
        candidate = f'{base[:100 - len(marker)].rstrip("-")}{marker}'
        suffix += 1
    return candidate


def _generated_panel_slug(name):
    """Create a stable internal identifier without making users maintain it."""
    base = slugify(name) or 'benchmark'
    return f'{base[:167].rstrip("-")}-{uuid.uuid4().hex}'


def _candidate_key(value, field):
    # ``baseline`` is an executor-owned synthetic candidate. Check the stripped,
    # case-insensitive form before syntax validation so no spelling can shadow it.
    value = _text(value, field)
    if value.casefold() == 'baseline':
        _error('candidate key baseline 由系统保留', field)
    if not _SAFE_KEY.fullmatch(value):
        _error(f'{field} 格式无效', field)
    return value


def _same_spec(actual, expected_class, expected_spec, *, allow_generic=False):
    actual_class, actual_spec = canonical_simc_spec_identity(actual)
    generic = str(actual or '').strip().lower() in ('', 'default', 'all', '*')
    return (allow_generic and generic) or (
        actual_spec == expected_spec
        and (not actual_class or not expected_class or actual_class == expected_class)
    )


def _same_profile_spec(profile, expected_class, expected_spec):
    actual_class, actual_spec = canonical_simc_profile_identity(profile.spec, profile.class_name)
    return actual_spec == expected_spec and actual_class == expected_class


def _resource(model, resource_id, kind, user_id):
    try:
        resource = model.objects.get(
            benchmark_resource_access_q(kind, user_id),
            pk=_id(resource_id, f'{kind}_id'),
        )
    except model.DoesNotExist:
        _error(f'{kind} 资源不存在', f'{kind}_id')
    return resource


def _normalize_simulation_params(value):
    value = _require_dict(value, 'simulation_params')
    unknown = sorted(set(value) - SIMULATION_PARAMS_WHITELIST)
    if unknown:
        _error(f'simulation_params 包含未知字段: {", ".join(unknown)}', 'simulation_params')
    # Composer owns the structured lists/dict. Keep this layer strict for every
    # other persisted value, then defer value policy to its public validator.
    for key, item in value.items():
        if key in {'raid_buffs', 'extra_options', 'profile_overrides'}:
            continue
        if item is not None and not isinstance(item, (str, int, float, bool)):
            _error(f'simulation_params.{key} 类型无效', 'simulation_params')
        if isinstance(item, float) and not math.isfinite(item):
            _error(f'simulation_params.{key} 必须是有限数值', 'simulation_params')
    options_error = validate_simulation_options(value)
    if options_error:
        _error(options_error, 'simulation_params')
    normalized = deepcopy(value)
    if 'raid_buffs' in normalized:
        selected = set(normalized['raid_buffs'])
        normalized['raid_buffs'] = [name for name in SIMC_RAID_BUFF_VALUES if name in selected]
    fight_style = value.get('fight_style')
    if fight_style is not None and fight_style not in SIMC_FIGHT_STYLE_VALUES:
        _error('simulation_params.fight_style 不是当前 SimC 支持的战斗类型', 'simulation_params')
    return normalized


def _normalize_comparison_config(value, benchmark_type):
    """Validate comparison overrides with the canonical Composer option schema."""
    if benchmark_type != SimcBenchmarkPanel.BENCHMARK_TYPE_OPTION_GAIN:
        return {}
    value = _require_dict(value or {}, 'comparison_config')
    unknown = sorted(set(value) - {'label', 'simulation_params'})
    if unknown:
        _error(f'comparison_config 包含未知字段: {", ".join(unknown)}', 'comparison_config')
    if not value:
        # Compatibility for option_gain panels created before structured comparisons.
        return {}
    label = _text(value.get('label'), 'comparison_config.label', max_length=120)
    simulation_params = _normalize_simulation_params(value.get('simulation_params'))
    if not simulation_params:
        _error('comparison_config.simulation_params 不能为空', 'comparison_config')
    return {'label': label, 'simulation_params': simulation_params}


def _normalize_item_options(raw_value):
    """Reject execution directives smuggled into the permissive legacy item parser."""
    for index, part in enumerate(raw_value.lstrip(',').split(',')):
        part = part.strip()
        if not part:
            continue
        key, separator, _value = part.partition('=')
        # A human-readable item name may precede id= and has no equals sign.
        if not separator:
            if index != 0:
                _error('装备候选包含不安全内容', 'params')
            if '/' in part or '\\' in part or '..' in part:
                _error('装备候选不允许文件路径', 'params')
            continue
        if key.strip().lower() not in _ITEM_OPTION_KEYS:
            _error(f'装备候选包含不允许的选项: {key.strip()}', 'params')


def _normalize_candidate_params(candidate_type, params):
    if candidate_type != 'gear_swap':
        _error('candidate_type 只支持 gear_swap；baseline 由系统注入', 'candidate_type')

    if isinstance(params, str):
        if '\n' in params or '\r' in params:
            _error('装备候选不允许换行', 'params')
        match = re.fullmatch(r'\s*([a-z][a-z0-9_]*)\s*=(.+)\s*', params, re.IGNORECASE)
        if not match:
            _error('gear_swap 字符串必须是单条 slot=id=... 装备行', 'params')
        slot, raw_value = match.groups()
    elif isinstance(params, dict):
        # Canonical snapshots may be resubmitted by the Dashboard, but their
        # executor-control fields must be asserted rather than silently repaired.
        if 'gear_swap' in params:
            if set(params) - {
                'candidate_type', 'is_base', 'gear_swap', 'simc_options',
                'benchmark_profile',
            }:
                _error('canonical gear params 包含未知字段', 'params')
            if params.get('candidate_type') != 'gear_swap':
                _error('canonical candidate_type 必须是 gear_swap', 'params')
            if params.get('is_base') is not False:
                _error('canonical is_base 必须严格为 false', 'params')
            if not isinstance(params.get('gear_swap'), dict):
                _error('gear_swap 必须是对象', 'params')
            swap = params['gear_swap']
            if set(swap) - {'slot', 'raw_value', 'item_id', 'source', 'bonus_id'}:
                _error('gear_swap 包含未知字段', 'params')
            slot, raw_value = swap.get('slot'), swap.get('raw_value')
        else:
            unknown = set(params) - {
                'slot', 'raw_value', 'simc_options', 'benchmark_profile',
            }
            if unknown:
                _error(f'gear_swap 包含未知字段: {", ".join(sorted(unknown))}', 'params')
            slot, raw_value = params.get('slot'), params.get('raw_value')
    else:
        _error('gear_swap params 必须是装备行或对象', 'params')

    if not isinstance(raw_value, str) or len(raw_value) > MAX_GEAR_RAW_VALUE_CHARS:
        _error(f'gear raw_value 最长 {MAX_GEAR_RAW_VALUE_CHARS} 个字符', 'params')
    canonical_slot = EQUIPMENT_SLOT_ALIASES.get(str(slot or '').strip().lower(), str(slot or '').strip().lower())
    if canonical_slot not in EQUIPMENT_SLOTS:
        _error('gear_swap slot 必须是装备槽', 'params')
    try:
        normalized = normalize_gear_candidate_value(canonical_slot, raw_value)
    except ValueError as exc:
        _error(str(exc), 'params')
    _normalize_item_options(normalized)
    item_match = re.search(r'(?:^|,)\s*id=(\d+)(?:,|$)', normalized, re.IGNORECASE)
    if item_match is None:  # Defensive invariant behind normalize_gear_candidate_value.
        _error('装备候选缺少物品 ID', 'params')
    result = {
        'candidate_type': 'gear_swap', 'is_base': False,
        'gear_swap': {
            'slot': canonical_slot, 'raw_value': normalized,
            'item_id': int(item_match.group(1)), 'source': 'manual',
        },
    }
    options = params.get('simc_options') if isinstance(params, dict) else None
    if options is not None:
        try:
            result['simc_options'] = normalize_controlled_simc_options(options)
        except ValueError as exc:
            _error(str(exc), 'params')
    benchmark_profile = params.get('benchmark_profile') if isinstance(params, dict) else None
    if benchmark_profile is not None:
        if canonical_slot != 'trinket1':
            _error('trinket benchmark_profile 只允许用于 trinket1 候选', 'params')
        if not isinstance(benchmark_profile, dict) or set(benchmark_profile) != {
            'kind', 'item_level',
        }:
            _error('benchmark_profile 必须是完整的受控对象', 'params')
        if benchmark_profile.get('kind') != 'trinket_standard_reference':
            _error('不支持的 benchmark_profile 类型', 'params')
        item_level = benchmark_profile.get('item_level')
        if type(item_level) is not int or not 1 <= item_level <= 1000:
            _error('benchmark_profile item_level 无效', 'params')
        result['benchmark_profile'] = deepcopy(benchmark_profile)
    size = len(json.dumps(result, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False).encode('utf-8'))
    if size > MAX_CANDIDATE_PARAMS_BYTES:
        _error(f'candidate params 超过 {MAX_CANDIDATE_PARAMS_BYTES} 字节', 'params')
    return result


def _benchmark_item_identity(params):
    swap = params['gear_swap']
    item_id = swap['item_id']
    options = []
    for fragment in swap['raw_value'].split(','):
        if '=' not in fragment:
            continue
        key, value = fragment.split('=', 1)
        options.append((key.strip().lower(), value.strip()))
    ids = [value for key, value in options if key == 'id']
    levels = [value for key, value in options if key in {'ilevel', 'item_level'}]
    if len(ids) != 1:
        _error('装备候选只能包含一个 id', 'params')
    if len(levels) != 1:
        if not levels:
            _error('装备候选必须填写有效装等', 'params')
        _error('装备候选只能包含一个装等', 'params')
    if not levels[0].isdigit() or int(levels[0]) <= 0:
        _error('装备候选必须填写有效装等', 'params')
    if int(ids[0]) != item_id:
        _error('装备候选 id 解析不一致', 'params')
    return item_id, int(levels[0]), options


def _derived_candidate_key(params, item_id, item_level, options):
    base = f'item-{item_id}-ilvl-{item_level}'
    simple_options = {key for key, _value in options} == {'id', 'ilevel'}
    swap = params['gear_swap']
    if swap['slot'] == 'trinket1' and simple_options and not params.get('simc_options'):
        return base
    fingerprint = hashlib.sha256(json.dumps(
        params, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
    ).encode('utf-8')).hexdigest()[:10]
    return f'{base}-{fingerprint}'


def _default_profile(spec_key):
    matches = list(SimcProfile.objects.filter(
        user_id__isnull=True, source=SimcProfile.SOURCE_SIMC_UPSTREAM,
        spec=spec_key, is_active=True, system_key__isnull=False,
    ).order_by('id')[:2])
    if len(matches) != 1:
        if len(matches) > 1:
            _error(f'专精 {spec_key} 存在重复的系统上游默认 Profile', 'profiles')
        _error(f'专精 {spec_key} 未配置 active 系统上游默认 Profile', 'profiles')
    return matches[0]


def _default_talent_string(spec_key):
    matches = list(SimcTalentString.objects.filter(
        owner_user_id__isnull=True, is_system=True,
        spec=spec_key, is_active=True, is_selectable=True,
        system_key=f'simc_upstream:{spec_key}',
    ).order_by('id')[:2])
    if len(matches) != 1:
        if len(matches) > 1:
            _error(f'专精 {spec_key} 存在重复的系统上游默认天赋字符串', 'profiles')
        _error(f'专精 {spec_key} 未配置 active 系统上游默认天赋字符串', 'profiles')
    return matches[0]


def _benchmark_tooltip_completeness(value):
    lines = [line.strip() for line in str(value or '').splitlines() if line.strip()]
    semantic_lines = sum(line.startswith(('+', 'Use:', 'Equip:', 'Passive:', 'Effect:', '使用：', '装备：', '被动：', '效果：')) for line in lines)
    return (semantic_lines, len(lines), len(str(value or '')))


def _best_benchmark_tooltip(description_zh, description):
    return max(
        (str(description_zh or '').strip(), str(description or '').strip()),
        key=_benchmark_tooltip_completeness,
    )


def _benchmark_item_display_metadata(item_id):
    """Resolve display-only item data before the candidate is frozen into an Execution."""
    item = WowItemSnapshot.objects.filter(item_id=item_id).only(
        'name_zh', 'name', 'description_zh', 'description', 'icon',
    ).first()
    if item is None:
        return '', '', ''
    label = str(item.name_zh or item.name or '').strip()
    effect = _best_benchmark_tooltip(item.description_zh, item.description)
    icon_name = str(item.icon or '').strip().split('?', 1)[0].rsplit('/', 1)[-1]
    while icon_name.rsplit('.', 1)[-1].lower() in {'jpg', 'jpeg', 'png', 'gif', 'webp'}:
        icon_name = icon_name.rsplit('.', 1)[0]
    icon_url = f'/static/wow_icons/small/{icon_name}.jpg' if icon_name else ''
    return label, effect, icon_url


def normalize_panel_payload(payload, user_id, panel=None):
    """Validate an untrusted JSON-shaped payload and return a canonical safe snapshot."""
    payload = _require_dict(payload, 'payload')
    if type(user_id) is not int or user_id <= 0:
        _error('user_id 必须是正整数', 'user_id')
    if panel is not None and panel.created_by_id != user_id:
        _error('只有 Panel owner 可以修改配置', 'panel')
    unknown_panel = set(payload) - _PANEL_FIELDS - {'specs', 'scenarios', 'candidates'}
    if unknown_panel:
        _error(f'Panel 包含未知字段: {", ".join(sorted(unknown_panel))}', 'payload')

    specs = _require_list(payload.get('specs'), 'specs')
    scenarios = _require_list(payload.get('scenarios'), 'scenarios')
    candidates = _require_list(payload.get('candidates'), 'candidates')
    if len(specs) > MAX_SPECS: _error(f'specs 最多 {MAX_SPECS} 项', 'specs')
    if len(scenarios) > MAX_SCENARIOS: _error(f'scenarios 最多 {MAX_SCENARIOS} 项', 'scenarios')

    name = _text(payload.get('name'), 'name', max_length=200)
    requested_slug = payload.get('slug')
    if panel is not None:
        panel_slug = panel.slug
    elif requested_slug is None:
        panel_slug = _generated_panel_slug(name)
    else:
        panel_slug = _key(requested_slug, 'slug')
    benchmark_type = _text(
        payload.get('benchmark_type', SimcBenchmarkPanel.BENCHMARK_TYPE_STANDARD),
        'benchmark_type', max_length=24,
    )
    if benchmark_type not in {
        SimcBenchmarkPanel.BENCHMARK_TYPE_STANDARD,
        SimcBenchmarkPanel.BENCHMARK_TYPE_OPTION_GAIN,
    }:
        _error('benchmark_type 必须是 standard 或 option_gain', 'benchmark_type')
    comparison_option = _text(
        payload.get('comparison_option', ''), 'comparison_option',
        required=False, max_length=50,
    )
    comparison_config = _normalize_comparison_config(
        payload.get('comparison_config', {}), benchmark_type,
    )
    if benchmark_type == SimcBenchmarkPanel.BENCHMARK_TYPE_OPTION_GAIN:
        if not comparison_config:
            comparison_option = comparison_option or 'power_infusion'
        if comparison_option and comparison_option not in _EXTRA_OPTION_BY_VALUE:
            _error('comparison_option 不是支持的额外选项', 'comparison_option')
        # 对比模式的候选由执行计划权威合成，不持久化普通装备候选。
        candidates = []
    else:
        comparison_option = ''

    normalized = {
        'name': name,
        'slug': panel_slug,
        'description': _text(payload.get('description', ''), 'description', required=False,
                             max_length=10000),
        'benchmark_type': benchmark_type,
        'comparison_option': comparison_option,
        'comparison_config': comparison_config,
        'is_active': _strict_bool(payload.get('is_active'), 'is_active', True),
        'is_public': _strict_bool(payload.get('is_public'), 'is_public', False),
        'schedule_enabled': _strict_bool(payload.get('schedule_enabled'), 'schedule_enabled', False),
        'interval_seconds': payload.get('interval_seconds', 86400),
        'next_run_at': payload.get('next_run_at'),
        'queue_priority': payload.get('queue_priority', 20),
        'specs': [], 'scenarios': [], 'candidates': [],
    }
    if type(normalized['interval_seconds']) is not int or normalized['interval_seconds'] <= 0:
        _error('interval_seconds 必须是正整数', 'interval_seconds')
    if normalized['queue_priority'] not in BENCHMARK_QUEUE_PRIORITIES:
        _error('queue_priority 必须是 10（低）、20（普通）或 30（高）', 'queue_priority')

    seen_specs = set()
    for index, raw in enumerate(specs):
        raw = _require_dict(raw, f'specs[{index}]')
        unknown = set(raw) - {
            'class_name', 'spec_key', 'label', 'apl_id', 'template_id', 'backend_id',
            'additional_simc_input', 'profiles', 'is_enabled', 'display_order',
        }
        if unknown: _error(f'spec 包含未知字段: {", ".join(sorted(unknown))}', 'specs')
        spec_key = _key(raw.get('spec_key'), 'spec_key')
        if spec_key in seen_specs: _error(f'重复 spec_key: {spec_key}', 'specs')
        seen_specs.add(spec_key)
        class_name = _key(raw.get('class_name'), 'class_name')
        expected_class, expected_spec = canonical_simc_spec_identity(spec_key)
        if not is_supported_simc_spec_identity(spec_key):
            _error('spec_key 不是受支持的合法专精', 'spec_key')
        if expected_class != class_name:
            _error('class_name/spec_key 不一致', 'spec_key')
        apl = _resource(SimcApl, raw.get('apl_id'), 'apl', user_id)
        template = _resource(SimcContentTemplate, raw.get('template_id'), 'template', user_id)
        backend = _resource(SimcBackendBinary, raw.get('backend_id'), 'backend', user_id)
        if not _same_spec(apl.spec, expected_class, expected_spec): _error('APL 专精不一致', 'apl_id')
        if not _same_spec(template.spec, expected_class, expected_spec, allow_generic=True):
            _error('Template 专精不一致', 'template_id')

        spec_enabled = _strict_bool(raw.get('is_enabled'), 'spec.is_enabled', True)
        profile_payload = raw.get('profiles', [])
        profile_payload = _require_list(profile_payload, 'profiles')
        if not profile_payload and spec_enabled:
            default = _default_profile(spec_key)
            default_talent = _default_talent_string(spec_key)
            profile_payload = [{
                'profile_id': default.pk,
                'talent_string_id': default_talent.pk,
                'label': default.name,
            }]
        if len(profile_payload) > MAX_PROFILES_PER_SPEC:
            _error(f'每个 spec 最多 {MAX_PROFILES_PER_SPEC} 个 profiles', 'profiles')
        normalized_profiles, seen_talent_variants = [], set()
        selected_profile_id = None
        for profile_index, profile_raw in enumerate(profile_payload):
            if type(profile_raw) is int:
                profile_raw = {'profile_id': profile_raw}
            profile_raw = _require_dict(profile_raw, f'profiles[{profile_index}]')
            unknown_profile = set(profile_raw) - {
                'profile_id', 'label', 'talent_string_id', 'apl_id', 'is_enabled', 'display_order',
            }
            if unknown_profile: _error('profile 包含未知字段', 'profiles')
            profile = _resource(SimcProfile, profile_raw.get('profile_id'), 'profile', user_id)
            if selected_profile_id is None:
                selected_profile_id = profile.pk
            elif profile.pk != selected_profile_id:
                _error('每个专精只能选择一个 Profile', 'profiles')
            profile_class = _profile_class_name(profile)
            if profile_class and profile_class != expected_class:
                _error('Profile 职业不一致', 'profiles')
            if not _same_profile_spec(profile, expected_class, expected_spec): _error('Profile 专精不一致', 'profiles')
            if not profile_raw.get('talent_string_id'):
                _error('新建 Benchmark 配置必须为每个 Profile 选择独立天赋字符串', 'talent_string_id')
            talent = _resource(
                SimcTalentString, profile_raw['talent_string_id'], 'talent', user_id,
            )
            if str(talent.spec).strip().lower() not in {expected_spec, spec_key}:
                _error('天赋字符串专精不一致', 'talent_string_id')
            selected_apl = None
            if profile_raw.get('apl_id') is not None:
                selected_apl = _resource(SimcApl, profile_raw['apl_id'], 'apl', user_id)
                if not _same_spec(selected_apl.spec, expected_class, expected_spec):
                    _error('Profile APL 专精不一致', 'apl_id')
            elif talent.default_apl_id:
                # The talent resource owns its default. Validate it again while
                # saving Panel config so inactive/mismatched defaults never leak
                # into a newly created execution.
                selected_apl = _resource(SimcApl, talent.default_apl_id, 'apl', user_id)
                if not _same_spec(selected_apl.spec, expected_class, expected_spec):
                    _error('天赋默认 APL 专精不一致', 'talent_string_id')
            talent_variant = talent.pk
            if talent_variant in seen_talent_variants:
                _error('profiles 包含重复天赋配置', 'profiles')
            seen_talent_variants.add(talent_variant)
            normalized_profiles.append({
                'profile_id': profile.pk,
                'label': _text(profile_raw.get('label', profile.name), 'profile.label',
                               max_length=200),
                'talent_string_id': talent_variant,
                'apl_id': selected_apl.pk if profile_raw.get('apl_id') is not None else None,
                'is_enabled': _strict_bool(profile_raw.get('is_enabled'), 'profile.is_enabled', True),
                'display_order': _order(profile_raw.get('display_order'), 'profile.display_order', profile_index),
            })
        if spec_enabled and not any(profile['is_enabled'] for profile in normalized_profiles):
            _error(f'启用专精 {spec_key} 至少需要一个启用 Profile', 'profiles')
        normalized['specs'].append({
            'class_name': class_name, 'spec_key': spec_key,
            'label': SPEC_CN.get(
                ''.join(part.capitalize() for part in expected_spec.split('_')), expected_spec,
            ),
            'apl_id': apl.pk, 'template_id': template.pk, 'backend_id': backend.pk,
            'additional_simc_input': _text(
                raw.get('additional_simc_input', ''), 'spec.additional_simc_input',
                required=False, max_length=20000,
            ),
            'profiles': normalized_profiles,
            'is_enabled': spec_enabled,
            'display_order': _order(raw.get('display_order'), 'spec.display_order', index),
        })

    seen_scenarios = set()
    for index, raw in enumerate(scenarios):
        raw = _require_dict(raw, f'scenarios[{index}]')
        unknown = set(raw) - {'key', 'name', 'simulation_params', 'is_enabled', 'display_order'}
        if unknown: _error('scenario 包含未知字段', 'scenarios')
        name = _text(raw.get('name'), 'scenario.name', max_length=200)
        raw_key = raw.get('key')
        key = _key(raw_key, 'scenario.key') if raw_key else _generated_scenario_key(
            name, seen_scenarios,
        )
        if key in seen_scenarios: _error(f'重复 scenario key: {key}', 'scenarios')
        seen_scenarios.add(key)
        normalized['scenarios'].append({
            'key': key, 'name': name,
            'simulation_params': _normalize_simulation_params(raw.get('simulation_params', {})),
            'is_enabled': _strict_bool(raw.get('is_enabled'), 'scenario.is_enabled', True),
            'display_order': _order(raw.get('display_order'), 'scenario.display_order', index),
        })

    seen_candidates = set()
    for index, raw in enumerate(candidates):
        raw = _require_dict(raw, f'candidates[{index}]')
        unknown = set(raw) - {
            'key', 'label', 'candidate_type', 'params', 'spec_keys', 'icon_url',
            'source_label', 'is_enabled', 'display_order',
        }
        if unknown: _error('candidate 包含未知字段', 'candidates')
        candidate_type = _text(raw.get('candidate_type'), 'candidate_type')
        params = _normalize_candidate_params(candidate_type, raw.get('params', {}))
        item_id, item_level, item_options = _benchmark_item_identity(params)
        key = _candidate_key(
            raw.get('key') or _derived_candidate_key(
                params, item_id, item_level, item_options,
            ),
            'candidate.key',
        )
        if key in seen_candidates: _error(f'重复 candidate key: {key}', 'candidates')
        seen_candidates.add(key)
        spec_keys = _require_list(raw.get('spec_keys', []), 'spec_keys')
        if any(not isinstance(item, str) for item in spec_keys):
            _error('spec_keys 只能包含字符串', 'spec_keys')
        spec_keys = [_key(item, 'spec_keys') for item in spec_keys]
        if len(set(spec_keys)) != len(spec_keys): _error('spec_keys 包含重复值', 'spec_keys')
        metadata_label, metadata_effect, metadata_icon_url = _benchmark_item_display_metadata(item_id)
        requested_label = _text(raw.get('label', ''), 'candidate.label', required=False, max_length=200)
        icon_url = metadata_icon_url or _text(raw.get('icon_url', ''), 'icon_url', required=False,
                                                max_length=500)
        if icon_url and not icon_url.startswith('/static/'):
            try:
                URLValidator()(icon_url)
            except ValidationError:
                _error('icon_url 必须是有效 URL', 'icon_url')
        label_without_level = requested_label
        level_suffix = f' · {item_level}' if item_level else ''
        if level_suffix and label_without_level.endswith(level_suffix):
            label_without_level = label_without_level[:-len(level_suffix)]
        if metadata_label:
            variant_suffix = ''
            if label_without_level.startswith(f'{metadata_label} · '):
                variant_suffix = label_without_level[len(metadata_label) + 3:]
            elif ' · ' in label_without_level:
                variant_suffix = label_without_level.rpartition(' · ')[2]
            candidate_label = (
                f'{metadata_label} · {variant_suffix}' if variant_suffix else metadata_label
            )
        else:
            candidate_label = label_without_level or f'物品 {item_id}'
        if item_level:
            candidate_label = f'{candidate_label} · {item_level}'
        normalized['candidates'].append({
            'key': key,
            'label': candidate_label,
            'candidate_type': candidate_type,
            'params': params,
            'spec_keys': spec_keys,
            'icon_url': icon_url,
            'effect': metadata_effect,
            'source_label': _text(raw.get('source_label', f'物品 #{item_id}'), 'source_label',
                                  required=False, max_length=200),
            'is_enabled': _strict_bool(raw.get('is_enabled'), 'candidate.is_enabled', True),
            'display_order': _order(raw.get('display_order'), 'candidate.display_order', index),
        })

    inherited_profiles = {}
    for spec in normalized['specs']:
        if not spec['is_enabled']:
            continue
        spec_key = spec['spec_key']
        applicable = [
            candidate for candidate in normalized['candidates']
            if candidate['is_enabled'] and (
                not candidate['spec_keys'] or spec_key in candidate['spec_keys']
            )
        ]
        marked = [
            candidate['params']['benchmark_profile']
            for candidate in applicable
            if 'benchmark_profile' in candidate['params']
        ]
        if marked and any(profile != marked[0] for profile in marked[1:]):
            _error(f'专精 {spec_key} 的候选 Benchmark Profile 不一致')
        if not marked:
            continue
        for candidate in applicable:
            if 'benchmark_profile' in candidate['params']:
                continue
            swap = candidate['params'].get('gear_swap', {})
            if swap.get('slot') != 'trinket1':
                _error(f'专精 {spec_key} 的候选混用了不同 Benchmark Profile 语义')
            existing = inherited_profiles.get(candidate['key'])
            if existing is not None and existing != marked[0]:
                _error(f'候选 {candidate["key"]} 的 Benchmark Profile 推导不一致')
            inherited_profiles[candidate['key']] = marked[0]
    for candidate in normalized['candidates']:
        inherited = inherited_profiles.get(candidate['key'])
        if inherited is not None:
            candidate['params']['benchmark_profile'] = deepcopy(inherited)

    total_size = len(json.dumps(normalized, sort_keys=True, separators=(',', ':'),
                                ensure_ascii=False, default=str).encode('utf-8'))
    if total_size > MAX_PANEL_CONFIG_BYTES:
        _error(f'Panel config snapshot 超过 {MAX_PANEL_CONFIG_BYTES} 字节', 'payload')
    return normalized


def _save_clean(instance):
    instance.full_clean()
    instance.save()
    return instance


@transaction.atomic
def replace_panel_config(payload, user_id, panel=None):
    """Create/update a Panel and atomically replace every nested configuration row."""
    if panel is not None:
        try:
            panel = SimcBenchmarkPanel.objects.select_for_update().get(pk=panel.pk)
        except SimcBenchmarkPanel.DoesNotExist:
            _error('Panel 不存在', 'panel')
        from botend.services.simc_benchmark_purge import panel_has_active_purge
        if panel_has_active_purge(panel.pk):
            _error('Panel 正在执行彻底删除，不能修改配置', 'panel')
        # Match execution-plan lock order before destructive replacement. The Panel
        # lock serializes normal writers; explicit child locks also make the contract
        # safe against maintenance code that addresses child rows directly.
        list(SimcBenchmarkSpec.objects.select_for_update().filter(
            panel=panel,
        ).order_by('id').values_list('id', flat=True))
        list(SimcBenchmarkProfile.objects.select_for_update().filter(
            panel_spec__panel=panel,
        ).order_by('id').values_list('id', flat=True))
        list(SimcBenchmarkScenario.objects.select_for_update().filter(
            panel=panel,
        ).order_by('id').values_list('id', flat=True))
        list(SimcBenchmarkCandidate.objects.select_for_update().filter(
            panel=panel,
        ).order_by('id').values_list('id', flat=True))
    snapshot = normalize_panel_payload(payload, user_id, panel=panel)
    if panel is None:
        panel = SimcBenchmarkPanel(created_by_id=user_id)
    for field in _PANEL_FIELDS:
        if field in snapshot:
            setattr(panel, field, snapshot[field])
    try:
        # Catch outside an inner savepoint: inspecting duplicates in a broken
        # transaction would otherwise raise TransactionManagementError.
        with transaction.atomic():
            _save_clean(panel)

            # Specs cascade to profile selections. Other panel children are replaced too.
            panel.specs.all().delete()
            panel.scenarios.all().delete()
            panel.candidates.all().delete()
            for spec_data in snapshot['specs']:
                profiles = spec_data.pop('profiles')
                spec = _save_clean(SimcBenchmarkSpec(panel=panel, **spec_data))
                for profile_data in profiles:
                    talent_string_id = profile_data.pop('talent_string_id', None)
                    _save_clean(SimcBenchmarkProfile(
                        panel_spec=spec, talent_string_id=talent_string_id,
                        **profile_data,
                    ))
            for scenario_data in snapshot['scenarios']:
                _save_clean(SimcBenchmarkScenario(panel=panel, **scenario_data))
            for candidate_data in snapshot['candidates']:
                _save_clean(SimcBenchmarkCandidate(panel=panel, **candidate_data))
    except IntegrityError:
        duplicate = SimcBenchmarkPanel.objects.filter(slug=snapshot['slug'])
        if panel.pk:
            duplicate = duplicate.exclude(pk=panel.pk)
        if duplicate.exists():
            _error('slug 已存在', 'slug')
        raise
    return panel, build_execution_plan(panel, validate_for_execution=False)


def _candidate_snapshot(candidate):
    return {
        'candidate_key': candidate.key, 'candidate_label': candidate.label,
        'candidate_params': deepcopy(candidate.params),
        'candidate_type': candidate.candidate_type, 'icon_url': candidate.icon_url,
        'effect': candidate.effect,
        'source_label': candidate.source_label,
    }


_STRENGTH_TRINKET_SPECS = frozenset({
    'deathknight_blood', 'deathknight_frost', 'deathknight_unholy',
    'paladin_protection', 'paladin_retribution',
    'warrior_arms', 'warrior_fury', 'warrior_protection',
})
_AGILITY_TRINKET_SPECS = frozenset({
    'demonhunter_devourer', 'demonhunter_havoc', 'demonhunter_vengeance',
    'druid_feral', 'druid_guardian',
    'hunter_beast_mastery', 'hunter_marksmanship', 'hunter_survival',
    'monk_brewmaster', 'monk_windwalker',
    'rogue_assassination', 'rogue_outlaw', 'rogue_subtlety',
    'shaman_enhancement',
})
_INTELLECT_TRINKET_SPECS = frozenset({
    'druid_balance', 'evoker_devastation',
    'mage_arcane', 'mage_fire', 'mage_frost',
    'priest_shadow', 'shaman_elemental',
    'warlock_affliction', 'warlock_demonology', 'warlock_destruction',
})
_VERSATILITY_TRINKET_IDS = {
    'agility': 142506, 'intellect': 142507, 'strength': 142508,
}


def _freeze_trinket_benchmark_preset(spec_key, benchmark_profile):
    if spec_key in _STRENGTH_TRINKET_SPECS:
        primary_stat = 'strength'
    elif spec_key in _AGILITY_TRINKET_SPECS:
        primary_stat = 'agility'
    elif spec_key in _INTELLECT_TRINKET_SPECS:
        primary_stat = 'intellect'
    else:
        _error(f'专精 {spec_key} 没有标准饰品主属性映射')
    item_level = benchmark_profile['item_level']
    return {
        'trinket1': '',
        'trinket2': (
            f'id={_VERSATILITY_TRINKET_IDS[primary_stat]},'
            f'ilevel={item_level},bonus_id=607'
        ),
    }


def _freeze_case_candidates(spec_key, applicable):
    profiles = [item.params.get('benchmark_profile') for item in applicable]
    marked = [profile for profile in profiles if profile is not None]
    if marked and len(marked) != len(applicable):
        _error(f'专精 {spec_key} 的候选混用了不同 Benchmark Profile 语义')
    if marked and any(profile != marked[0] for profile in marked[1:]):
        _error(f'专精 {spec_key} 的候选 Benchmark Profile 不一致')

    baseline = {
        'candidate_key': 'baseline', 'candidate_label': 'Baseline',
        'candidate_params': {'candidate_type': 'base', 'is_base': True},
        'candidate_type': 'base', 'icon_url': '', 'source_label': '',
    }
    candidates = [_candidate_snapshot(item) for item in applicable]
    if not marked:
        return [baseline] + candidates

    preset = _freeze_trinket_benchmark_preset(spec_key, marked[0])
    baseline['candidate_params']['equipment_preset'] = deepcopy(preset)
    for candidate in candidates:
        candidate['candidate_params'].pop('benchmark_profile', None)
        candidate['candidate_params']['equipment_preset'] = deepcopy(preset)
    return [baseline] + candidates


def _freeze_option_gain_candidates(option_value):
    option = _EXTRA_OPTION_BY_VALUE.get(option_value)
    if option is None:
        _error('Panel 的 comparison_option 不是支持的额外选项')
    label = option['label']
    return [
        {
            'candidate_key': 'baseline',
            'candidate_label': f'不开启{label}',
            'candidate_type': 'option_toggle',
            'candidate_params': {
                'candidate_type': 'option_toggle',
                'option_value': option_value,
                'enabled': False,
            },
            'icon_url': '', 'effect': '', 'source_label': '',
        },
        {
            'candidate_key': 'option_enabled',
            'candidate_label': f'开启{label}',
            'candidate_type': 'option_toggle',
            'candidate_params': {
                'candidate_type': 'option_toggle',
                'option_value': option_value,
                'enabled': True,
            },
            'icon_url': '', 'effect': '', 'source_label': '',
        },
    ]


def _freeze_comparison_candidates(comparison_config):
    """Freeze a base run and one structured Composer override run."""
    return [
        {
            'candidate_key': 'baseline', 'candidate_label': '基准配置',
            'candidate_type': 'scenario_override',
            'candidate_params': {
                'candidate_type': 'scenario_override', 'simulation_params': {},
            },
            'icon_url': '', 'effect': '', 'source_label': '',
        },
        {
            'candidate_key': 'comparison',
            'candidate_label': comparison_config['label'],
            'candidate_type': 'scenario_override',
            'candidate_params': {
                'candidate_type': 'scenario_override',
                'simulation_params': deepcopy(comparison_config['simulation_params']),
            },
            'icon_url': '', 'effect': '', 'source_label': '',
        },
    ]


def _resource_display_snapshot(spec, selected, apl):
    """Freeze display/version identities, deliberately excluding bodies and paths."""
    return {
        'profile': {
            'id': selected.profile_id, 'name': selected.profile.name,
            'source': selected.profile.source, 'system_key': selected.profile.system_key,
            'sync_version': selected.profile.sync_version,
            'class_name': selected.profile.class_name, 'spec': selected.profile.spec,
        },
        'talent_string': ({
            'id': selected.talent_string_id, 'name': selected.talent_string.name,
            'spec': selected.talent_string.spec,
            'hero_talent_names': list(selected.talent_string.hero_talent_names or []),
        } if selected.talent_string_id else None),
        'apl': {
            'id': apl.pk, 'name': apl.name, 'source': apl.source,
            'spec': apl.spec, 'sync_version': apl.sync_version,
            'validation_revision': apl.validation_revision,
            'validation_game_build': apl.validation_game_build,
        },
        'template': {
            'id': spec.template_id, 'name': spec.template.name,
            'source': spec.template.source, 'spec': spec.template.spec,
            'sync_version': spec.template.sync_version,
        },
        'backend': {
            'id': spec.backend_id, 'identifier': spec.backend.identifier,
            'name': spec.backend.name, 'platform': spec.backend.platform,
            'current_version': spec.backend.current_version,
        },
    }


def _panel_snapshot_queryset(*, enabled_only):
    profile_rows = SimcBenchmarkProfile.objects.select_related(
        'profile', 'apl', 'talent_string', 'talent_string__default_apl',
    ).order_by('display_order', 'id')
    specs = SimcBenchmarkSpec.objects.select_related('apl', 'template', 'backend').order_by('display_order', 'id')
    scenarios = SimcBenchmarkScenario.objects.order_by('display_order', 'id')
    candidates = SimcBenchmarkCandidate.objects.order_by('display_order', 'id')
    if enabled_only:
        profile_rows = profile_rows.filter(is_enabled=True)
        specs = specs.filter(is_enabled=True)
        scenarios = scenarios.filter(is_enabled=True)
        candidates = candidates.filter(is_enabled=True)
    specs = specs.prefetch_related(Prefetch(
        'profiles', queryset=profile_rows, to_attr='_snapshot_profiles',
    ))
    return SimcBenchmarkPanel.objects.prefetch_related(
        Prefetch('specs', queryset=specs, to_attr='_snapshot_specs'),
        Prefetch('scenarios', queryset=scenarios, to_attr='_snapshot_scenarios'),
        Prefetch('candidates', queryset=candidates, to_attr='_snapshot_candidates'),
    )


def _locked_panel_snapshot_queryset():
    """Lock only config rows in Panel→Spec→Profile→Scenario→Candidate order.

    Resource FKs are deliberately not joined: ``SELECT .. FOR UPDATE`` plus
    ``select_related`` can lock Backend/Profile/APL/Template implicitly and violate
    the global task persistence order.
    """
    profiles = SimcBenchmarkProfile.objects.select_for_update().filter(
        is_enabled=True,
    ).order_by('display_order', 'id')
    specs = SimcBenchmarkSpec.objects.select_for_update().filter(
        is_enabled=True,
    ).order_by('display_order', 'id').prefetch_related(Prefetch(
        'profiles', queryset=profiles, to_attr='_snapshot_profiles',
    ))
    scenarios = SimcBenchmarkScenario.objects.select_for_update().filter(
        is_enabled=True,
    ).order_by('display_order', 'id')
    candidates = SimcBenchmarkCandidate.objects.select_for_update().filter(
        is_enabled=True,
    ).order_by('display_order', 'id')
    return SimcBenchmarkPanel.objects.select_for_update().prefetch_related(
        Prefetch('specs', queryset=specs, to_attr='_snapshot_specs'),
        Prefetch('scenarios', queryset=scenarios, to_attr='_snapshot_scenarios'),
        Prefetch('candidates', queryset=candidates, to_attr='_snapshot_candidates'),
    )


@transaction.atomic
def build_execution_plan(panel, validate_for_execution=True, *, lock=True):
    """Build a deterministic plan from one freshly queried DB snapshot.

    ``lock=False`` is the optimistic preflight view used before expensive binary
    validation. The default remains the historical locked planning contract.
    """
    queryset = (_locked_panel_snapshot_queryset() if lock
                else _panel_snapshot_queryset(enabled_only=True))
    try:
        panel = queryset.get(pk=panel.pk)
    except SimcBenchmarkPanel.DoesNotExist:
        _error('Panel 不存在', 'panel')
    specs = panel._snapshot_specs
    scenarios = panel._snapshot_scenarios
    candidates = panel._snapshot_candidates
    if lock:
        # Locks above cover configuration only. Load display resources afterwards
        # with ordinary batched reads; task persistence acquires their locks in the
        # global Backend→Profile→APL→Template order.
        profile_rows = [selected for row in specs for selected in row._snapshot_profiles]
        profiles = SimcProfile.objects.in_bulk({row.profile_id for row in profile_rows})
        talents = SimcTalentString.objects.in_bulk({row.talent_string_id for row in profile_rows if row.talent_string_id})
        apl_ids = ({row.apl_id for row in specs}
                   | {row.apl_id for row in profile_rows if row.apl_id}
                   | {talent.default_apl_id for talent in talents.values() if talent.default_apl_id})
        apls = SimcApl.objects.in_bulk(apl_ids)
        templates = SimcContentTemplate.objects.in_bulk({row.template_id for row in specs})
        backends = SimcBackendBinary.objects.in_bulk({row.backend_id for row in specs})
        for row in specs:
            row.apl = apls[row.apl_id]
            row.template = templates[row.template_id]
            row.backend = backends[row.backend_id]
        for row in profile_rows:
            row.profile = profiles[row.profile_id]
            if row.apl_id:
                row.apl = apls[row.apl_id]
            if row.talent_string_id:
                row.talent_string = talents[row.talent_string_id]
                if row.talent_string.default_apl_id:
                    row.talent_string.default_apl = apls[row.talent_string.default_apl_id]
    if not specs: _error('没有 enabled 专精，无法执行')
    if not scenarios: _error('没有 enabled 场景，无法执行')
    is_option_gain = panel.benchmark_type == SimcBenchmarkPanel.BENCHMARK_TYPE_OPTION_GAIN
    comparison_config = deepcopy(panel.comparison_config or {}) if is_option_gain else {}
    if is_option_gain:
        case_candidates = (
            _freeze_comparison_candidates(comparison_config)
            if comparison_config else _freeze_option_gain_candidates(panel.comparison_option)
        )
        candidates = []
    elif panel.benchmark_type != SimcBenchmarkPanel.BENCHMARK_TYPE_STANDARD:
        _error('Panel 的 benchmark_type 无效')
    elif any(item.candidate_type != 'gear_swap' for item in candidates):
        _error('持久化候选只允许 gear_swap；baseline 由系统注入')

    cases = []
    for spec in specs:
        profiles = spec._snapshot_profiles
        if not profiles:
            _error(f'专精 {spec.spec_key} 没有 enabled Profile')
        applicable = [
            item for item in candidates
            if not item.spec_keys or spec.spec_key in item.spec_keys
        ]
        if not is_option_gain:
            case_candidates = _freeze_case_candidates(spec.spec_key, applicable)
        for scenario in scenarios:
            for selected in profiles:
                if not selected.talent_string_id:
                    _error(
                        f'专精 {spec.spec_key} 的 Profile {selected.profile_id} 未绑定独立天赋字符串；'
                        '历史配置可继续查看，但必须补全后才能新建任务',
                        'talent_string_id',
                    )
                simulation_params = deepcopy(scenario.simulation_params)
                additional_inputs = [
                    str(value).strip() for value in (
                        spec.additional_simc_input,
                        simulation_params.get('additional_simc_input', ''),
                    ) if str(value).strip()
                ]
                if additional_inputs:
                    simulation_params['additional_simc_input'] = '\n'.join(additional_inputs)
                else:
                    simulation_params.pop('additional_simc_input', None)
                if is_option_gain and not comparison_config:
                    simulation_params['extra_options'] = [
                        value for value in simulation_params.get('extra_options', [])
                        if value != panel.comparison_option
                    ]
                effective_apl = (
                    selected.apl if selected.apl_id else
                    (selected.talent_string.default_apl if selected.talent_string.default_apl_id else spec.apl)
                )
                cases.append({
                    'spec_key': spec.spec_key, 'spec_label': spec.label,
                    'class_name': spec.class_name,
                    'scenario_key': scenario.key, 'scenario_label': scenario.name,
                    'profile_key': benchmark_profile_key(
                        selected.profile_id, selected.talent_string_id,
                    ),
                    'profile_label': selected.profile.name,
                    'profile_id': selected.profile_id,
                    'talent_string_id': selected.talent_string_id,
                    'apl_id': effective_apl.pk,
                    'template_id': spec.template_id, 'backend_id': spec.backend_id,
                    'simulation_params': simulation_params,
                    'candidates': deepcopy(case_candidates),
                    'resources': _resource_display_snapshot(spec, selected, effective_apl),
                })
    case_count = len(cases)
    run_count = sum(len(item['candidates']) for item in cases)
    return {
        'panel': {
            'id': panel.pk, 'name': panel.name, 'slug': panel.slug,
            'description': panel.description,
            'benchmark_type': panel.benchmark_type,
            'comparison_option': panel.comparison_option,
            'comparison_config': deepcopy(panel.comparison_config or {}),
        },
        'specs': [{
            'id': row.pk, 'class_name': row.class_name, 'spec_key': row.spec_key,
            'display_label': row.label,
        } for row in specs],
        'scenarios': [{
            'id': row.pk, 'key': row.key, 'label': row.name,
            'simulation_params': deepcopy(row.simulation_params),
        } for row in scenarios],
        'candidates': [{
            'id': row.pk, 'key': row.key, 'label': row.label,
            'candidate_type': row.candidate_type, 'icon_url': row.icon_url,
            'source_label': row.source_label, 'params': deepcopy(row.params),
        } for row in candidates],
        'cases': cases, 'case_count': case_count, 'run_count': run_count,
    }


def serialize_panel_config(panel):
    """Return public configuration metadata, never resource bodies, paths or user ids."""
    try:
        panel = _panel_snapshot_queryset(enabled_only=False).get(pk=panel.pk)
    except SimcBenchmarkPanel.DoesNotExist:
        _error('Panel 不存在', 'panel')
    result = {
        'id': panel.pk, 'name': panel.name, 'slug': panel.slug,
        'description': panel.description, 'benchmark_type': panel.benchmark_type,
        'comparison_option': panel.comparison_option,
        'comparison_config': deepcopy(panel.comparison_config or {}),
        'is_active': panel.is_active,
        'is_public': panel.is_public, 'schedule_enabled': panel.schedule_enabled,
        'interval_seconds': panel.interval_seconds, 'queue_priority': panel.queue_priority,
        'next_run_at': panel.next_run_at,
        'specs': [], 'scenarios': [], 'candidates': [],
    }
    for spec in panel._snapshot_specs:
        profiles = []
        for selected in spec._snapshot_profiles:
            effective_apl = (
                selected.apl if selected.apl_id else
                (selected.talent_string.default_apl if (
                    selected.talent_string_id and selected.talent_string.default_apl_id
                ) else spec.apl)
            )
            profiles.append({
                'id': selected.pk, 'profile_id': selected.profile_id,
                'label': selected.label, 'profile_name': selected.profile.name,
                'talent_string_id': selected.talent_string_id,
                'talent_string_name': selected.talent_string.name if selected.talent_string_id else '',
                'apl': {
                    'id': effective_apl.pk, 'name': effective_apl.name,
                    'inherited': not bool(selected.apl_id),
                },
                'is_enabled': selected.is_enabled, 'display_order': selected.display_order,
            })
        result['specs'].append({
            'id': spec.pk, 'class_name': spec.class_name, 'spec_key': spec.spec_key,
            'label': spec.label, 'is_enabled': spec.is_enabled, 'display_order': spec.display_order,
            'additional_simc_input': spec.additional_simc_input,
            'apl': {'id': spec.apl_id, 'name': spec.apl.name},
            'template': {'id': spec.template_id, 'name': spec.template.name},
            'backend': {'id': spec.backend_id, 'identifier': spec.backend.identifier, 'name': spec.backend.name},
            'profiles': profiles,
        })
    result['scenarios'] = [{
        'id': row.pk, 'key': row.key, 'name': row.name,
        'simulation_params': deepcopy(row.simulation_params),
        'is_enabled': row.is_enabled, 'display_order': row.display_order,
    } for row in panel._snapshot_scenarios]
    result['candidates'] = (
        []
        if panel.benchmark_type == SimcBenchmarkPanel.BENCHMARK_TYPE_OPTION_GAIN
        else [{
            'id': row.pk, 'key': row.key, 'label': row.label,
            'candidate_type': row.candidate_type, 'params': deepcopy(row.params),
            'spec_keys': deepcopy(row.spec_keys), 'icon_url': row.icon_url,
            'effect': row.effect, 'source_label': row.source_label, 'is_enabled': row.is_enabled,
            'display_order': row.display_order,
        } for row in panel._snapshot_candidates]
    )
    return deepcopy(result)


@transaction.atomic
def duplicate_panel_config(panel, user_id):
    """Copy reusable Panel configuration without creating execution records."""
    try:
        source = SimcBenchmarkPanel.objects.select_for_update().get(pk=panel.pk)
    except SimcBenchmarkPanel.DoesNotExist:
        _error('Panel 不存在', 'panel')

    payload = serialize_panel_config(source)
    payload.pop('id', None)
    payload.pop('slug', None)
    payload['name'] = f'{source.name[:196]}（副本）'
    payload['is_public'] = False
    payload['schedule_enabled'] = False
    payload['next_run_at'] = None

    for spec in payload['specs']:
        spec.pop('id', None)
        spec['apl_id'] = spec.pop('apl')['id']
        spec['template_id'] = spec.pop('template')['id']
        spec['backend_id'] = spec.pop('backend')['id']
        for profile in spec['profiles']:
            profile.pop('id', None)
            profile.pop('profile_name', None)
            apl = profile.pop('apl', None)
            if apl and not apl.get('inherited'):
                profile['apl_id'] = apl['id']
    for scenario in payload['scenarios']:
        scenario.pop('id', None)
    for candidate in payload['candidates']:
        candidate.pop('id', None)
        candidate.pop('effect', None)

    return replace_panel_config(payload, user_id)
