# -*- coding: utf-8 -*-
"""Portal 天赋模拟器页面与 API。

该页面复用已有 WowTalentNodeMetadata、TalentBuildCodeService 和 render_model，
只新增面向前端交互的轻量包装：
- GET /portal/talents/ 页面
- GET /portal/api/talents/simulator/ 读取完整可渲染树
- POST /portal/api/talents/simulator/encode/ 根据选择状态编码 Blizzard 导入字符串
"""

from __future__ import annotations

import json
from collections import defaultdict

from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.views import View

from botend.constants.wow import CLASS_CN, CLASS_SPEC_MAP, SPEC_CN, SPEC_ICON, SPEC_ROLE
from botend.constants.hero_talents import (
    hero_subtree_name_by_id,
    hero_subtree_name_zh,
    spec_hero_subtree_names,
)
from botend.templatetags.wow_tags import wow_icon
from botend.wow.talents.build_code import TalentBuildCodeDecoder, _build_node_key
from botend.wow.talents.metadata import TalentMetadataProvider
from botend.wow.talents.service import TalentBuildCodeService
from botend.wow.talents.versioning import TalentVersionResolver
from botend.wow.talents.view_model import build_talent_view_model


DEFAULT_CLASS = 'DeathKnight'
DEFAULT_SPEC = 'Blood'


def _validate_spec(class_name, spec_name):
    specs = CLASS_SPEC_MAP.get(class_name)
    if not specs or spec_name not in specs:
        raise Http404


def _load_profile_for_simulator(profile_id, class_name, spec_name):
    profile_id = _to_int(profile_id)
    if not profile_id:
        return None
    from botend.models import PlayerSpecTopPlayer

    return PlayerSpecTopPlayer.objects.filter(
        id=profile_id,
        class_name=class_name,
        spec_name=spec_name,
    ).first()


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _build_specs_payload():
    specs = []
    for class_name, spec_names in CLASS_SPEC_MAP.items():
        class_specs = []
        for spec_name in spec_names:
            class_specs.append({
                'class_name': class_name,
                'spec_name': spec_name,
                'class_cn': CLASS_CN.get(class_name, class_name),
                'spec_cn': SPEC_CN.get(spec_name, spec_name),
                'role': SPEC_ROLE.get((class_name, spec_name), 'dps'),
                'icon': SPEC_ICON.get((class_name, spec_name), ''),
            })
        specs.append({
            'class_name': class_name,
            'class_cn': CLASS_CN.get(class_name, class_name),
            'specs': class_specs,
        })
    return specs


def _node_key(node):
    tree_type = node.get('tree_type') or 'spec'
    identity = node.get('node_id') or node.get('talent_id') or node.get('spell_id') or node.get('display_spell_id')
    return f'{tree_type}:{identity}' if identity else ''


def _parse_selected_nodes(raw_nodes):
    if isinstance(raw_nodes, str):
        try:
            raw_nodes = json.loads(raw_nodes or '[]')
        except Exception:
            raw_nodes = []
    if not isinstance(raw_nodes, list):
        return []

    selected = []
    for item in raw_nodes:
        if not isinstance(item, dict):
            continue
        points = _to_int(item.get('points'), 0)
        if points <= 0:
            continue
        selected_item = {
            'tree_type': item.get('tree_type') or 'spec',
            'node_id': _to_int(item.get('node_id') or item.get('nodeID')) or None,
            'talent_id': _to_int(item.get('talent_id') or item.get('talentID')) or None,
            'spell_id': _to_int(item.get('spell_id') or item.get('spellID')) or None,
            'display_spell_id': _to_int(item.get('display_spell_id') or item.get('displaySpellID')) or None,
            'points': points,
        }
        if item.get('purchased') is False:
            selected_item['purchased'] = False
        if item.get('choice_selection') is not None:
            selected_item['choice_selection'] = _to_int(item.get('choice_selection'), 0)
        selected.append(selected_item)
    return selected


def _decorate_render_model(render_model):
    """补充前端需要但模板 tag 无法在 JS 中计算的字段。"""
    if not isinstance(render_model, dict):
        return {}
    for tree in render_model.get('trees') or []:
        for node in tree.get('nodes') or []:
            node['node_key'] = node.get('node_key') or _node_key(node)
            node['icon_url'] = wow_icon(node.get('icon'), 'small') if node.get('icon') else wow_icon('', 'small')
            node['display_name'] = node.get('name_zh') or node.get('name') or '未命名天赋'
            node['display_desc'] = node.get('description_zh') or node.get('description') or ''
            for option in node.get('choice_options') or []:
                option['icon_url'] = wow_icon(option.get('icon'), 'small') if option.get('icon') else wow_icon('', 'small')
                option['display_name'] = option.get('name_zh') or option.get('name') or '未命名'
                option['display_desc'] = option.get('description_zh') or option.get('description') or ''
    return render_model


def _choice_selection_for_node(node, decoded_state):
    if not decoded_state:
        return 0
    try:
        return int(decoded_state.get('choice_selection') or 0)
    except (TypeError, ValueError):
        return 0


def _choice_selection_from_payload(node):
    options = node.get('choice_options') or []
    explicit = node.get('choice_selection')
    if explicit is not None:
        selected = _to_int(explicit, 0)
        if 0 <= selected < len(options):
            return selected
    node_spell_ids = {_to_int(node.get('spell_id'), 0), _to_int(node.get('display_spell_id'), 0)}
    node_names = {str(node.get('name') or '').strip(), str(node.get('name_zh') or '').strip()}
    for index, option in enumerate(options):
        option_spell_ids = {_to_int(option.get('spell_id'), 0), _to_int(option.get('display_spell_id'), 0)}
        option_names = {str(option.get('name') or '').strip(), str(option.get('name_zh') or '').strip()}
        if node_spell_ids.intersection(option_spell_ids) or (node_names - {''}).intersection(option_names - {''}):
            return index
    return 0


def _map_decoded_states_to_full_nodes(full_nodes, decoder_nodes, decoded_states):
    """Map canonical decoder states onto visible metadata nodes.

    A TraitNode can move between metadata display buckets across DB2 snapshots
    (notably spec apex nodes appearing as ``hero_anchor`` in decoder metadata).
    The bitstream identity is still the same node/talent/spell, so match through
    the service's canonical aliases instead of requiring the display tree_type
    to be identical.
    """
    if not decoded_states:
        return {}
    decoder_by_alias = {}
    for decoder_node in decoder_nodes or []:
        state = decoded_states.get(_build_node_key(decoder_node))
        if state is None:
            continue
        for alias in TalentBuildCodeService._node_alias_keys_for_matching(decoder_node):
            decoder_by_alias.setdefault(alias, state)

    mapped = {}
    for node in full_nodes or []:
        state = decoded_states.get(_build_node_key(node))
        if state is None:
            for alias in TalentBuildCodeService._node_alias_keys_for_matching(node):
                state = decoder_by_alias.get(alias)
                if state is not None:
                    break
        if state is not None:
            mapped[_build_node_key(node)] = state
    return mapped


def _merge_nodes_for_simulator(full_nodes, decoded_states=None, active_hero_subtree=None):
    """把完整元数据转为模拟器渲染节点。

    不走角色详情的默认“只保留一棵英雄树”策略，而是由前端传入/导入串推导
    active_hero_subtree。这样页面可以在两个英雄树之间稳定切换。
    """
    decoded_states = decoded_states or {}
    hero_root_key = ''
    if active_hero_subtree and not decoded_states:
        hero_roots = [
            node for node in full_nodes or []
            if (node.get('tree_type') or 'spec') == 'hero'
            and (node.get('db2_subtree_id') or 0) == active_hero_subtree
            and not (node.get('parents') or [])
        ]
        if hero_roots:
            hero_root = min(hero_roots, key=lambda node: (
                _to_int(node.get('row'), 0),
                _to_int(node.get('column'), 0),
                _to_int(node.get('talent_id'), 0),
            ))
            hero_root_key = _build_node_key(hero_root)
    merged = []
    for node in full_nodes or []:
        if (node.get('tree_type') or 'spec') == 'hero':
            subtree = node.get('db2_subtree_id') or 0
            if not active_hero_subtree or subtree != active_hero_subtree:
                continue
        payload = dict(node)
        key = _build_node_key(payload)
        state = decoded_states.get(key)
        if state is None:
            points = _to_int(payload.get('points'), 0)
            payload['points'] = points
            payload['selected'] = bool(payload.get('selected') or points > 0)
            state = {}
        else:
            points = _to_int(state.get('points'), 0)
            payload['points'] = points
            payload['selected'] = bool(state.get('selected') or points > 0)
            payload['purchased'] = state.get('purchased', True)
        if key == hero_root_key:
            payload['points'] = 1
            payload['selected'] = True
            payload['purchased'] = False
        if payload.get('choice_options'):
            selected_index = _choice_selection_for_node(payload, state) if state else _choice_selection_from_payload(payload)
            payload['choice_selection'] = selected_index
            options = payload.get('choice_options') or []
            if 0 <= selected_index < len(options) and payload['selected']:
                selected_option = options[selected_index]
                if selected_option.get('display_spell_id'):
                    payload['display_spell_id'] = selected_option.get('display_spell_id')
                if selected_option.get('spell_id'):
                    payload['spell_id'] = selected_option.get('spell_id')
                if selected_option.get('talent_id'):
                    payload['talent_id'] = selected_option.get('talent_id')
                if selected_option.get('icon'):
                    payload['icon'] = selected_option.get('icon')
                if selected_option.get('name'):
                    payload['name'] = selected_option.get('name')
                if selected_option.get('description'):
                    payload['description'] = selected_option.get('description')
                if selected_option.get('description_zh'):
                    payload['description_zh'] = selected_option.get('description_zh')
        merged.append(payload)
    return merged


def _filter_hero_subtrees_for_spec(full_nodes, class_name, spec_name):
    """只保留当前专精可选择的两棵英雄树。

    DB2 元数据按职业树会带出该职业全部 hero subtree；但游戏内每个专精只能
    在两棵英雄树中二选一。这里用常量中的专精关系按 subtree canonical name
    过滤，避免页面出现 3 选 1。
    """
    allowed_names = set(spec_hero_subtree_names(class_name, spec_name))
    if not allowed_names:
        return list(full_nodes or [])
    filtered = []
    for node in full_nodes or []:
        if (node.get('tree_type') or 'spec') != 'hero':
            filtered.append(node)
            continue
        subtree_name = hero_subtree_name_by_id(node.get('db2_subtree_id'))
        if subtree_name in allowed_names:
            filtered.append(node)
    return filtered


def _hero_subtrees(full_nodes):
    groups = defaultdict(list)
    for node in full_nodes or []:
        if (node.get('tree_type') or 'spec') == 'hero':
            subtree = node.get('db2_subtree_id') or 0
            groups[subtree].append(node)
    payload = []
    for subtree_id, nodes in sorted(groups.items(), key=lambda item: item[0]):
        subtree_name = hero_subtree_name_by_id(subtree_id)
        first_named = next((n for n in nodes if n.get('name')), nodes[0] if nodes else {})
        payload.append({
            'id': subtree_id,
            'name': subtree_name or first_named.get('name') or f'Hero Subtree {subtree_id}',
            'title': hero_subtree_name_zh(subtree_name) or first_named.get('name') or f'英雄树 {subtree_id}',
            'node_count': len(nodes),
        })
    return payload


def _active_hero_subtree(full_nodes, decoded_states=None, requested=None):
    subtrees = _hero_subtrees(full_nodes)
    valid_ids = {item['id'] for item in subtrees}
    requested = _to_int(requested, 0)
    if requested in valid_ids:
        return requested
    if decoded_states:
        subtree_points = defaultdict(int)
        for node in full_nodes or []:
            if (node.get('tree_type') or 'spec') != 'hero':
                continue
            state = decoded_states.get(_build_node_key(node)) or {}
            if state.get('selected'):
                subtree_points[node.get('db2_subtree_id') or 0] += _to_int(state.get('points'), 0)
        if subtree_points:
            return max(subtree_points.items(), key=lambda item: item[1])[0]
    if len(subtrees) == 1:
        return subtrees[0]['id']
    return 0


def build_simulator_payload(class_name, spec_name, build_code='', hero_subtree=None, version_key='', profile_id=None):
    _validate_spec(class_name, spec_name)
    profile = _load_profile_for_simulator(profile_id, class_name, spec_name)
    profile_talents = getattr(profile, 'talents_json', None) if profile else None
    profile_build_code = str(getattr(profile, 'talent_build_code', '') or '').strip() if profile else ''
    build_code = str(build_code or profile_build_code or '').strip()

    talent_version = TalentVersionResolver.resolve(version_key=version_key, usage=TalentVersionResolver.USAGE_SIMULATOR)
    provider = TalentMetadataProvider(talent_version=talent_version, version_key=version_key, usage=TalentVersionResolver.USAGE_SIMULATOR)
    full_nodes = _filter_hero_subtrees_for_spec(
        provider.get_full_tree_nodes(class_name, spec_name),
        class_name,
        spec_name,
    )
    decoder_nodes = provider.get_decoder_node_list(class_name) if build_code else []
    decoded_states = TalentBuildCodeDecoder.decode_node_states(build_code, decoder_nodes) if build_code and decoder_nodes else {}
    decoded_states = _map_decoded_states_to_full_nodes(full_nodes, decoder_nodes, decoded_states)
    if profile_talents:
        merged_nodes = TalentBuildCodeService.build_full_payload(
            class_name=class_name,
            spec_name=spec_name,
            talent_build_code=build_code,
            talents_json=profile_talents,
            talent_version=talent_version,
            version_key=version_key,
            usage=TalentVersionResolver.USAGE_SIMULATOR,
        )
        merged_nodes = [node for node in merged_nodes if not TalentBuildCodeService._extract_build_code_from_node(node)]
        full_nodes = _filter_hero_subtrees_for_spec(merged_nodes, class_name, spec_name)
        active_subtree = _active_hero_subtree(full_nodes, decoded_states=None, requested=hero_subtree)
        merged_nodes = _merge_nodes_for_simulator(full_nodes, decoded_states=None, active_hero_subtree=active_subtree)
        build_code = TalentBuildCodeService._canonicalize_build_code_from_payload(
            merged_nodes,
            class_name=class_name,
            spec_name=spec_name,
            reference_build_code=build_code,
            talent_version=talent_version,
            version_key=version_key,
            usage=TalentVersionResolver.USAGE_SIMULATOR,
        ) or build_code
    else:
        active_subtree = _active_hero_subtree(full_nodes, decoded_states=decoded_states, requested=hero_subtree)
        merged_nodes = _merge_nodes_for_simulator(full_nodes, decoded_states=decoded_states, active_hero_subtree=active_subtree)
    view_model = build_talent_view_model(merged_nodes, class_name=class_name, spec_name=spec_name)
    render_model = _decorate_render_model(view_model.get('render_model') or {})
    return {
        'class_name': class_name,
        'spec_name': spec_name,
        'class_cn': CLASS_CN.get(class_name, class_name),
        'spec_cn': SPEC_CN.get(spec_name, spec_name),
        'spec_icon': SPEC_ICON.get((class_name, spec_name), ''),
        'talent_version': TalentVersionResolver.serialize(talent_version),
        'hero_subtrees': _hero_subtrees(full_nodes),
        'active_hero_subtree': active_subtree,
        'render_model': render_model,
        'build_code': build_code or '',
        'parse_status': 'success' if build_code and decoded_states else ('empty' if full_nodes else 'missing'),
    }


class PortalTalentSimulatorView(View):
    def get(self, request):
        class_name = request.GET.get('class') or DEFAULT_CLASS
        spec_name = request.GET.get('spec') or (CLASS_SPEC_MAP.get(class_name) or [DEFAULT_SPEC])[0]
        _validate_spec(class_name, spec_name)
        context = {
            'class_name': class_name,
            'spec_name': spec_name,
            'specs_payload': _build_specs_payload(),
            'talent_versions': [TalentVersionResolver.serialize(v) for v in TalentVersionResolver.list_active()],
            'default_talent_version': TalentVersionResolver.serialize(TalentVersionResolver.get_default(TalentVersionResolver.USAGE_SIMULATOR)),
        }
        return render(request, 'portal/talent_simulator.html', context)


class PortalTalentSimulatorAPIView(View):
    def get(self, request):
        class_name = request.GET.get('class') or DEFAULT_CLASS
        spec_name = request.GET.get('spec') or (CLASS_SPEC_MAP.get(class_name) or [DEFAULT_SPEC])[0]
        build_code = request.GET.get('code') or ''
        hero_subtree = request.GET.get('hero_subtree') or request.GET.get('hero') or ''
        version_key = request.GET.get('version') or request.GET.get('version_key') or ''
        profile_id = request.GET.get('profile_id') or request.GET.get('profile') or ''
        try:
            payload = build_simulator_payload(
                class_name,
                spec_name,
                build_code=build_code,
                hero_subtree=hero_subtree,
                version_key=version_key,
                profile_id=profile_id,
            )
        except Http404:
            return JsonResponse({'success': False, 'error': '未知职业或专精'}, status=404)
        except ValueError as exc:
            return JsonResponse({'success': False, 'error': str(exc)}, status=404)
        return JsonResponse({'success': True, **payload})


class PortalTalentSimulatorEncodeAPIView(View):
    def post(self, request):
        try:
            body = json.loads(request.body.decode('utf-8') or '{}')
        except Exception:
            body = {}
        class_name = body.get('class_name') or body.get('class') or DEFAULT_CLASS
        spec_name = body.get('spec_name') or body.get('spec') or (CLASS_SPEC_MAP.get(class_name) or [DEFAULT_SPEC])[0]
        reference_build_code = str(body.get('reference_build_code') or body.get('build_code') or '').strip()
        version_key = str(body.get('version') or body.get('version_key') or '').strip()
        selected_nodes = _parse_selected_nodes(body.get('selected_nodes') or [])
        try:
            _validate_spec(class_name, spec_name)
        except Http404:
            return JsonResponse({'success': False, 'error': '未知职业或专精'}, status=404)

        build_code = TalentBuildCodeService.encode_build_code_from_nodes(
            selected_nodes,
            class_name=class_name,
            spec_name=spec_name,
            reference_build_code=reference_build_code,
            version_key=version_key,
            usage=TalentVersionResolver.USAGE_SIMULATOR,
        )
        return JsonResponse({
            'success': bool(build_code),
            'build_code': build_code,
            'error': '' if build_code else '当前专精缺少可复用的参考导入头，暂时无法生成 Blizzard 导入字符串',
        })
