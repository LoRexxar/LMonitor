"""Validation, transactional persistence and execution planning for SimC benchmarks.

The public snapshots in this module deliberately contain only resource ids and display
metadata. Executable resource bodies remain behind the existing task/version service.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from typing import NoReturn

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import IntegrityError, transaction
from django.db.models import Prefetch, Q

from botend.constants.wow import SPEC_CN
from botend.models import (
    SimcApl, SimcBackendBinary, SimcBenchmarkCandidate, SimcBenchmarkPanel,
    SimcBenchmarkProfile, SimcBenchmarkScenario, SimcBenchmarkSpec,
    SimcContentTemplate, SimcProfile, WowItemSnapshot,
)
from botend.services.simc_player_config import (
    EQUIPMENT_SLOTS, EQUIPMENT_SLOT_ALIASES, canonical_simc_spec_identity,
    is_supported_simc_spec_identity, normalize_battlenet_class_name,
    normalize_gear_candidate_value,
    SUPPORTED_SIMC_SPEC_IDENTITIES,
)
from botend.services.simc_task_service import (
    SIMULATION_PARAMS_WHITELIST, TaskCreationError, validate_resource_ownership,
)
from botend.services.simc_composer import validate_simulation_options
from botend.services.simc_candidate_options import normalize_controlled_simc_options

MAX_SPECS = len(SUPPORTED_SIMC_SPEC_IDENTITIES)
MAX_PROFILES_PER_SPEC = 5
MAX_SCENARIOS = 8
MAX_GEAR_RAW_VALUE_CHARS = 2048
MAX_CANDIDATE_PARAMS_BYTES = 16 * 1024
MAX_PANEL_CONFIG_BYTES = 2 * 1024 * 1024
# Keep the catalog bound aligned with the existing panel/candidate payload budgets.
MAX_CANDIDATES = MAX_PANEL_CONFIG_BYTES // MAX_CANDIDATE_PARAMS_BYTES
MAX_CASES = 120
MAX_RUNS_PER_TASK = MAX_CANDIDATES + 1

_PANEL_FIELDS = {
    'name', 'slug', 'description', 'is_active', 'is_public', 'schedule_enabled',
    'interval_seconds', 'next_run_at',
}
_ITEM_OPTION_KEYS = {
    'id', 'ilevel', 'item_level', 'bonus_id', 'bonus_ids', 'gem_id', 'gems',
    'enchant_id', 'crafted_stats', 'crafting_quality', 'drop_level',
    'content_tuning', 'suffix', 'upgrade',
}
_SAFE_KEY = re.compile(r'^[a-z0-9][a-z0-9_-]{0,99}$')


def benchmark_resource_access_q(kind, user_id):
    """Return the single access policy used by benchmark writes and option lists."""
    if kind == 'backend':
        return Q(is_active=True)
    if kind == 'template':
        return Q(is_active=True, is_selectable=True) & (
            Q(owner_user_id=user_id) | Q(owner_user_id__isnull=True)
        )
    if kind == 'apl':
        return Q(is_active=True, is_selectable=True) & (
            Q(owner_user_id=user_id) | Q(owner_user_id__isnull=True) | Q(is_system=True)
        )
    if kind == 'profile':
        system_profile = (
            Q(user_id__isnull=True, source=SimcProfile.SOURCE_SIMC_UPSTREAM,
              is_active=True, system_key__isnull=False)
            & ~Q(system_key='')
        )
        return Q(user_id=user_id, is_active=True) | system_profile
    raise ValueError(f'unknown benchmark resource kind: {kind}')


def benchmark_resource_querysets(user_id):
    """Query resources selectable by a panel whose immutable owner is ``user_id``."""
    models_by_name = {
        'backends': (SimcBackendBinary, 'backend'),
        'templates': (SimcContentTemplate, 'template'),
        'apls': (SimcApl, 'apl'),
        'profiles': (SimcProfile, 'profile'),
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
        if not _same_spec(apl.spec, expected_class, expected_spec):
            _error(f'{spec_key}: APL specialization mismatch', 'resources')
        if not _same_spec(template.spec, expected_class, expected_spec, allow_generic=True):
            _error(f'{spec_key}: Template specialization mismatch', 'resources')
        profile_class = normalize_battlenet_class_name(profile.class_name)
        if profile_class and profile_class != expected_class:
            _error(f'{spec_key}: Profile class mismatch', 'resources')
        if not _same_spec(profile.spec, expected_class, expected_spec):
            _error(f'{spec_key}: Profile specialization mismatch', 'resources')
        resolved[spec_key] = {
            'apl': apl, 'template': template, 'backend': backend, 'profile': profile,
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


def _resource(model, resource_id, kind, user_id):
    try:
        resource = model.objects.get(
            benchmark_resource_access_q(kind, user_id),
            pk=_id(resource_id, f'{kind}_id'),
        )
    except model.DoesNotExist:
        _error(f'{kind} 资源不存在', f'{kind}_id')
    try:
        validate_resource_ownership(resource, kind, user_id)
    except TaskCreationError as exc:
        _error(str(exc), f'{kind}_id')
    return resource


def _normalize_simulation_params(value):
    value = _require_dict(value, 'simulation_params')
    unknown = sorted(set(value) - SIMULATION_PARAMS_WHITELIST)
    if unknown:
        _error(f'simulation_params 包含未知字段: {", ".join(unknown)}', 'simulation_params')
    # JSON scalars only. In particular, nested objects/lists are not executable options.
    for key, item in value.items():
        if item is not None and not isinstance(item, (str, int, float, bool)):
            _error(f'simulation_params.{key} 类型无效', 'simulation_params')
        if isinstance(item, float) and not math.isfinite(item):
            _error(f'simulation_params.{key} 必须是有限数值', 'simulation_params')
    options_error = validate_simulation_options(value)
    if options_error:
        _error(options_error, 'simulation_params')
    return deepcopy(value)


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
            if set(params) - {'candidate_type', 'is_base', 'gear_swap', 'simc_options'}:
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
            unknown = set(params) - {'slot', 'raw_value', 'simc_options'}
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


def _benchmark_item_display_metadata(item_id):
    """Resolve display-only item data before the candidate is frozen into an Execution."""
    item = WowItemSnapshot.objects.filter(item_id=item_id).only(
        'name_zh', 'name', 'icon',
    ).first()
    if item is None:
        return '', ''
    label = str(item.name_zh or item.name or '').strip()
    icon_name = str(item.icon or '').strip().split('?', 1)[0].rsplit('/', 1)[-1]
    while icon_name.rsplit('.', 1)[-1].lower() in {'jpg', 'jpeg', 'png', 'gif', 'webp'}:
        icon_name = icon_name.rsplit('.', 1)[0]
    icon_url = f'/static/wow_icons/small/{icon_name}.jpg' if icon_name else ''
    return label, icon_url


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
    if len(candidates) > MAX_CANDIDATES: _error(f'candidates 最多 {MAX_CANDIDATES} 项', 'candidates')

    normalized = {
        'name': _text(payload.get('name'), 'name', max_length=200),
        'slug': _key(payload.get('slug'), 'slug'),
        'description': _text(payload.get('description', ''), 'description', required=False,
                             max_length=10000),
        'is_active': _strict_bool(payload.get('is_active'), 'is_active', True),
        'is_public': _strict_bool(payload.get('is_public'), 'is_public', False),
        'schedule_enabled': _strict_bool(payload.get('schedule_enabled'), 'schedule_enabled', False),
        'interval_seconds': payload.get('interval_seconds', 86400),
        'next_run_at': payload.get('next_run_at'),
        'specs': [], 'scenarios': [], 'candidates': [],
    }
    if type(normalized['interval_seconds']) is not int or normalized['interval_seconds'] <= 0:
        _error('interval_seconds 必须是正整数', 'interval_seconds')

    seen_specs = set()
    for index, raw in enumerate(specs):
        raw = _require_dict(raw, f'specs[{index}]')
        unknown = set(raw) - {
            'class_name', 'spec_key', 'label', 'apl_id', 'template_id', 'backend_id',
            'profiles', 'is_enabled', 'display_order',
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

        profile_payload = raw.get('profiles', [])
        profile_payload = _require_list(profile_payload, 'profiles')
        if not profile_payload:
            default = _default_profile(spec_key)
            profile_payload = [{'profile_id': default.pk, 'label': default.name}]
        if len(profile_payload) > MAX_PROFILES_PER_SPEC:
            _error(f'每个 spec 最多 {MAX_PROFILES_PER_SPEC} 个 profiles', 'profiles')
        normalized_profiles, seen_profiles = [], set()
        for profile_index, profile_raw in enumerate(profile_payload):
            if type(profile_raw) is int:
                profile_raw = {'profile_id': profile_raw}
            profile_raw = _require_dict(profile_raw, f'profiles[{profile_index}]')
            unknown_profile = set(profile_raw) - {'profile_id', 'label', 'is_enabled', 'display_order'}
            if unknown_profile: _error('profile 包含未知字段', 'profiles')
            profile = _resource(SimcProfile, profile_raw.get('profile_id'), 'profile', user_id)
            if profile.pk in seen_profiles: _error('profiles 包含重复 Profile', 'profiles')
            seen_profiles.add(profile.pk)
            profile_class = normalize_battlenet_class_name(profile.class_name)
            if profile_class and profile_class != expected_class:
                _error('Profile 职业不一致', 'profiles')
            if not _same_spec(profile.spec, expected_class, expected_spec): _error('Profile 专精不一致', 'profiles')
            normalized_profiles.append({
                'profile_id': profile.pk,
                'label': _text(profile_raw.get('label', profile.name), 'profile.label',
                               max_length=200),
                'is_enabled': _strict_bool(profile_raw.get('is_enabled'), 'profile.is_enabled', True),
                'display_order': _order(profile_raw.get('display_order'), 'profile.display_order', profile_index),
            })
        normalized['specs'].append({
            'class_name': class_name, 'spec_key': spec_key,
            'label': SPEC_CN.get(
                ''.join(part.capitalize() for part in expected_spec.split('_')), expected_spec,
            ),
            'apl_id': apl.pk, 'template_id': template.pk, 'backend_id': backend.pk,
            'profiles': normalized_profiles,
            'is_enabled': _strict_bool(raw.get('is_enabled'), 'spec.is_enabled', True),
            'display_order': _order(raw.get('display_order'), 'spec.display_order', index),
        })

    seen_scenarios = set()
    for index, raw in enumerate(scenarios):
        raw = _require_dict(raw, f'scenarios[{index}]')
        unknown = set(raw) - {'key', 'name', 'simulation_params', 'is_enabled', 'display_order'}
        if unknown: _error('scenario 包含未知字段', 'scenarios')
        key = _key(raw.get('key'), 'scenario.key')
        if key in seen_scenarios: _error(f'重复 scenario key: {key}', 'scenarios')
        seen_scenarios.add(key)
        normalized['scenarios'].append({
            'key': key, 'name': _text(raw.get('name'), 'scenario.name', max_length=200),
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
        metadata_label, metadata_icon_url = _benchmark_item_display_metadata(item_id)
        requested_label = _text(raw.get('label', ''), 'candidate.label', required=False, max_length=200)
        icon_url = metadata_icon_url or _text(raw.get('icon_url', ''), 'icon_url', required=False,
                                                max_length=500)
        if icon_url and not icon_url.startswith('/static/'):
            try:
                URLValidator()(icon_url)
            except ValidationError:
                _error('icon_url 必须是有效 URL', 'icon_url')
        variant_suffix = requested_label.rpartition(' · ')[2] if ' · ' in requested_label else ''
        candidate_label = (
            f'{metadata_label} · {variant_suffix}'
            if metadata_label and variant_suffix
            else (metadata_label or f'物品 {item_id}')
        )
        if item_level:
            candidate_label = f'{candidate_label} · {item_level}'
        normalized['candidates'].append({
            'key': key,
            'label': candidate_label,
            'candidate_type': candidate_type,
            'params': params,
            'spec_keys': spec_keys,
            'icon_url': icon_url,
            'source_label': _text(raw.get('source_label', f'物品 #{item_id}'), 'source_label',
                                  required=False, max_length=200),
            'is_enabled': _strict_bool(raw.get('is_enabled'), 'candidate.is_enabled', True),
            'display_order': _order(raw.get('display_order'), 'candidate.display_order', index),
        })
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
                    _save_clean(SimcBenchmarkProfile(panel_spec=spec, **profile_data))
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
        'source_label': candidate.source_label,
    }


def _resource_display_snapshot(spec, selected):
    """Freeze display/version identities, deliberately excluding bodies and paths."""
    return {
        'profile': {
            'id': selected.profile_id, 'name': selected.profile.name,
            'source': selected.profile.source, 'system_key': selected.profile.system_key,
            'sync_version': selected.profile.sync_version,
            'class_name': selected.profile.class_name, 'spec': selected.profile.spec,
        },
        'apl': {
            'id': spec.apl_id, 'name': spec.apl.name, 'source': spec.apl.source,
            'spec': spec.apl.spec, 'sync_version': spec.apl.sync_version,
            'validation_revision': spec.apl.validation_revision,
            'validation_game_build': spec.apl.validation_game_build,
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
    profile_rows = SimcBenchmarkProfile.objects.select_related('profile').order_by('display_order', 'id')
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
        apls = SimcApl.objects.in_bulk({row.apl_id for row in specs})
        templates = SimcContentTemplate.objects.in_bulk({row.template_id for row in specs})
        backends = SimcBackendBinary.objects.in_bulk({row.backend_id for row in specs})
        profile_rows = [selected for row in specs for selected in row._snapshot_profiles]
        profiles = SimcProfile.objects.in_bulk({row.profile_id for row in profile_rows})
        for row in specs:
            row.apl = apls[row.apl_id]
            row.template = templates[row.template_id]
            row.backend = backends[row.backend_id]
        for row in profile_rows:
            row.profile = profiles[row.profile_id]
    if not specs: _error('没有 enabled 专精，无法执行')
    if not scenarios: _error('没有 enabled 场景，无法执行')
    if any(item.candidate_type != 'gear_swap' for item in candidates):
        _error('持久化候选只允许 gear_swap；baseline 由系统注入')

    baseline = {
        'candidate_key': 'baseline', 'candidate_label': 'Baseline',
        'candidate_params': {'candidate_type': 'base', 'is_base': True},
        'candidate_type': 'base', 'icon_url': '', 'source_label': '',
    }
    cases = []
    for spec in specs:
        profiles = spec._snapshot_profiles
        if not profiles:
            _error(f'专精 {spec.spec_key} 没有 enabled Profile')
        applicable = [
            item for item in candidates
            if not item.spec_keys or spec.spec_key in item.spec_keys
        ]
        case_candidates = [baseline] + [_candidate_snapshot(item) for item in applicable]
        if validate_for_execution and len(case_candidates) > MAX_RUNS_PER_TASK:
            _error(f'专精 {spec.spec_key} 每 Task runs 超过 {MAX_RUNS_PER_TASK}')
        for scenario in scenarios:
            for selected in profiles:
                cases.append({
                    'spec_key': spec.spec_key, 'spec_label': spec.label,
                    'class_name': spec.class_name,
                    'scenario_key': scenario.key, 'scenario_label': scenario.name,
                    'profile_key': str(selected.profile_id), 'profile_label': selected.label,
                    'profile_id': selected.profile_id, 'apl_id': spec.apl_id,
                    'template_id': spec.template_id, 'backend_id': spec.backend_id,
                    'simulation_params': deepcopy(scenario.simulation_params),
                    'candidates': deepcopy(case_candidates),
                    'resources': _resource_display_snapshot(spec, selected),
                })
    case_count = len(cases)
    run_count = sum(len(item['candidates']) for item in cases)
    if validate_for_execution and case_count > MAX_CASES:
        _error(f'执行 cases 超过 {MAX_CASES}（当前 {case_count}）')
    return {
        'panel': {
            'id': panel.pk, 'name': panel.name, 'slug': panel.slug,
            'description': panel.description,
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
        'description': panel.description, 'is_active': panel.is_active,
        'is_public': panel.is_public, 'schedule_enabled': panel.schedule_enabled,
        'interval_seconds': panel.interval_seconds, 'next_run_at': panel.next_run_at,
        'specs': [], 'scenarios': [], 'candidates': [],
    }
    for spec in panel._snapshot_specs:
        result['specs'].append({
            'id': spec.pk, 'class_name': spec.class_name, 'spec_key': spec.spec_key,
            'label': spec.label, 'is_enabled': spec.is_enabled, 'display_order': spec.display_order,
            'apl': {'id': spec.apl_id, 'name': spec.apl.name},
            'template': {'id': spec.template_id, 'name': spec.template.name},
            'backend': {'id': spec.backend_id, 'identifier': spec.backend.identifier, 'name': spec.backend.name},
            'profiles': [{
                'id': selected.pk, 'profile_id': selected.profile_id,
                'label': selected.label, 'profile_name': selected.profile.name,
                'is_enabled': selected.is_enabled, 'display_order': selected.display_order,
            } for selected in spec._snapshot_profiles],
        })
    result['scenarios'] = [{
        'id': row.pk, 'key': row.key, 'name': row.name,
        'simulation_params': deepcopy(row.simulation_params),
        'is_enabled': row.is_enabled, 'display_order': row.display_order,
    } for row in panel._snapshot_scenarios]
    result['candidates'] = [{
        'id': row.pk, 'key': row.key, 'label': row.label,
        'candidate_type': row.candidate_type, 'params': deepcopy(row.params),
        'spec_keys': deepcopy(row.spec_keys), 'icon_url': row.icon_url,
        'source_label': row.source_label, 'is_enabled': row.is_enabled,
        'display_order': row.display_order,
    } for row in panel._snapshot_candidates]
    return deepcopy(result)
