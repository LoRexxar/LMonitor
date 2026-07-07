# -*- coding: utf-8 -*-

import copy
import json
from collections import defaultdict
from functools import lru_cache

from botend.wow.talents.build_code import TalentBuildCodeDecoder, TalentBuildCodeEncoder, _build_node_key, _node_alias_keys
from botend.wow.talents.metadata import TalentMetadataProvider
from botend.wow.talents.parser import normalize_talent_payload
from botend.wow.talents.view_model import build_talent_view_model
from botend.wow.talents.versioning import TalentVersionResolver


class TalentBuildCodeService:
    @classmethod
    def extract_build_code(cls, talent_build_code='', talents_json=None):
        value = str(talent_build_code or '').strip()
        if value:
            return value

        if isinstance(talents_json, str):
            return str(talents_json or '').strip()

        if isinstance(talents_json, dict):
            return cls._extract_build_code_from_node(talents_json)

        if isinstance(talents_json, list):
            for item in talents_json:
                if not isinstance(item, dict):
                    continue
                value = cls._extract_build_code_from_node(item)
                if value:
                    return value
        return ''

    @classmethod
    def build_api_view(cls, talent_build_code='', talents_json=None, class_name='', spec_name='', talent_version=None, version_key='', usage=TalentVersionResolver.USAGE_PLAYER_TREE):
        build_code = cls.extract_build_code(talent_build_code, talents_json)
        if not build_code and talents_json:
            build_code = cls.encode_build_code_from_nodes(talents_json, class_name=class_name, spec_name=spec_name, talent_version=talent_version, version_key=version_key, usage=usage)
        payload = cls.build_full_payload(
            class_name=class_name,
            spec_name=spec_name,
            talent_build_code=build_code,
            talents_json=talents_json,
            talent_version=talent_version,
            version_key=version_key,
            usage=usage,
        )
        effective_build_code = cls._extract_build_code_from_payload(payload) or build_code
        if not payload:
            return {
                'talent_build_code': '',
                'has_talent_build_code': False,
                'talent_parse_status': 'missing',
                'talent_view_model': {},
                'talent_render_model': {},
            }

        try:
            view_model = cls._build_view_model_cached(
                class_name or '',
                spec_name or '',
                cls._version_cache_key(talent_version, version_key, usage),
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            )
        except Exception:
            return {
                'talent_build_code': effective_build_code,
                'has_talent_build_code': bool(effective_build_code),
                'talent_parse_status': 'failed',
                'talent_view_model': {},
                'talent_render_model': {'build_code': effective_build_code},
            }

        render_model = copy.deepcopy(view_model.get('render_model') or {})
        render_model['build_code'] = effective_build_code
        response_view_model = copy.deepcopy(view_model)
        response_view_model['build_code'] = effective_build_code
        response_view_model['render_model'] = render_model
        return {
            'talent_build_code': effective_build_code,
            'has_talent_build_code': bool(effective_build_code),
            'talent_parse_status': 'success' if (build_code or response_view_model.get('nodes')) else 'missing',
            'talent_view_model': response_view_model,
            'talent_render_model': render_model,
        }

    @classmethod
    def encode_build_code_from_nodes(cls, talents_json=None, class_name='', spec_name='', reference_build_code='', talent_version=None, version_key='', usage=TalentVersionResolver.USAGE_SIMULATOR):
        selected_nodes = cls._normalize_nodes_for_encoding(talents_json, class_name=class_name, spec_name=spec_name)
        if not selected_nodes:
            return ''

        provider = TalentMetadataProvider(talent_version=talent_version, version_key=version_key, usage=usage)
        # Blizzard import strings encode the whole class decoder list, not only
        # the current spec's render tree. Using get_full_tree_nodes(class/spec)
        # shifts bits for other spec nodes / hero_anchor nodes and produces
        # strings that import with missing hero talents or wrong point totals.
        decoder_nodes = provider.get_decoder_node_list(class_name)
        if not decoder_nodes:
            return ''

        reference = str(reference_build_code or '').strip() or cls._find_reference_build_code(class_name, spec_name)
        if not reference:
            return ''
        return TalentBuildCodeEncoder.encode_node_states(reference, decoder_nodes, selected_nodes)

    @staticmethod
    def _find_reference_build_code(class_name='', spec_name=''):
        try:
            from botend.models import PlayerSpecTopPlayer
        except Exception:
            return ''
        row = (
            PlayerSpecTopPlayer.objects
            .filter(class_name=class_name, spec_name=spec_name)
            .exclude(talent_build_code='')
            .only('talent_build_code')
            .first()
        )
        return str(getattr(row, 'talent_build_code', '') or '').strip()

    @classmethod
    def _normalize_nodes_for_encoding(cls, talents_json=None, class_name='', spec_name=''):
        payload_model = normalize_talent_payload(talents_json or [], class_name=class_name, spec_name=spec_name)
        nodes = []
        for item in payload_model.get('nodes') or []:
            if not isinstance(item, dict) or cls._extract_build_code_from_node(item):
                continue
            node = dict(item)
            if node.get('id') and not node.get('node_id'):
                node['node_id'] = node.get('id')
            if node.get('talentID') and not node.get('talent_id'):
                node['talent_id'] = node.get('talentID')
            if node.get('spellID') and not node.get('spell_id'):
                node['spell_id'] = node.get('spellID')
            nodes.append(node)
        return nodes

    @classmethod
    def build_full_payload(cls, class_name='', spec_name='', talent_build_code='', talents_json=None, talent_version=None, version_key='', usage=TalentVersionResolver.USAGE_PLAYER_TREE):
        build_code = cls.extract_build_code(talent_build_code, talents_json)
        if not build_code and talents_json:
            build_code = cls.encode_build_code_from_nodes(talents_json, class_name=class_name, spec_name=spec_name, talent_version=talent_version, version_key=version_key, usage=usage)
        payload_model = normalize_talent_payload(talents_json or [], class_name=class_name, spec_name=spec_name)
        selected_nodes = []
        for item in payload_model.get('nodes') or []:
            if not isinstance(item, dict):
                continue
            if cls._extract_build_code_from_node(item):
                continue
            selected_nodes.append(dict(item))

        provider = TalentMetadataProvider(talent_version=talent_version, version_key=version_key, usage=usage)
        full_nodes = provider.get_full_tree_nodes(class_name, spec_name)

        # --- build code decoding uses ALL nodes (all hero subtrees + hero_anchor) ---
        # Blizzard build code encodes the entire class tree (one bit per TraitNode),
        # sorted by talent_id (TraitNode ID). We need the full node list for correct
        # bit alignment.
        decoder_nodes = provider.get_decoder_node_list(class_name) if build_code else []
        decoded_states = TalentBuildCodeDecoder.decode_node_states(build_code, decoder_nodes) if build_code and decoder_nodes else {}
        if build_code and decoded_states and talent_version is None and not version_key:
            fallback = cls._find_apex_compatible_version_payload(
                build_code,
                class_name=class_name,
                spec_name=spec_name,
                current_provider=provider,
                current_full_nodes=full_nodes,
                current_decoder_nodes=decoder_nodes,
                current_decoded_states=decoded_states,
            )
            if fallback:
                provider = fallback['provider']
                full_nodes = fallback['full_nodes']
                decoder_nodes = fallback['decoder_nodes']
                decoded_states = fallback['decoded_states']
        if build_code and decoded_states and selected_nodes:
            decoded_states = cls._prefer_structured_nodes_when_build_code_looks_stale(
                decoded_states,
                selected_nodes,
                decoder_nodes,
                class_name=class_name,
                spec_name=spec_name,
            )

        # --- hero subtree filtering for RENDERING (not decoding) ---
        # The rendering only shows the active hero subtree's nodes.
        # When build_code is available, use decoded states to determine active subtree
        # (more reliable than old talents_json which may have stale subtree_id=0).
        render_nodes = full_nodes
        if render_nodes:
            hero_by_subtree = defaultdict(list)
            for node in render_nodes:
                if (node.get('tree_type') or 'spec') == 'hero':
                    subtree = node.get('db2_subtree_id') or 0
                    hero_by_subtree[subtree].append(node)
            if len(hero_by_subtree) > 1:
                active_subtree = None
                if decoded_states:
                    # Determine active subtree from decoded states
                    subtree_points = defaultdict(int)
                    for node in decoder_nodes:
                        if (node.get('tree_type') or 'spec') == 'hero':
                            key = _build_node_key(node)
                            state = decoded_states.get(key)
                            if state and state.get('selected'):
                                subtree_points[node.get('db2_subtree_id', 0)] += state.get('points', 0)
                    if subtree_points:
                        active_subtree = max(subtree_points, key=subtree_points.get)
                if active_subtree is None and selected_nodes:
                    # Fallback to selected_nodes from talents_json
                    node_id_to_subtree = {}
                    for node in render_nodes:
                        if (node.get('tree_type') or 'spec') == 'hero':
                            nid = node.get('node_id')
                            if nid:
                                node_id_to_subtree[nid] = node.get('db2_subtree_id') or 0
                    subtree_points = defaultdict(int)
                    for sn in selected_nodes:
                        if (sn.get('tree_type') or 'spec') == 'hero':
                            nid = sn.get('node_id')
                            subtree = node_id_to_subtree.get(nid)
                            if subtree:
                                subtree_points[subtree] += sn.get('points', 0) or 0
                    if subtree_points:
                        active_subtree = max(subtree_points, key=subtree_points.get)
                if active_subtree is None:
                    active_subtree = max(hero_by_subtree, key=lambda k: len(hero_by_subtree[k]))
                render_nodes = [
                    n for n in render_nodes
                    if (n.get('tree_type') or 'spec') != 'hero'
                    or (n.get('db2_subtree_id') or 0) == active_subtree
                ]

        merged_nodes = cls._merge_full_tree_nodes(
            render_nodes,
            selected_nodes,
            decoded_states=decoded_states,
            has_build_code=bool(build_code),
            decoder_nodes=decoder_nodes,
        ) if render_nodes else selected_nodes
        # Keep the original Raider.IO/Blizzard import string authoritative.
        # Historical structured talents_json can lag behind new tree nodes (notably
        # 12.1 apex pools). Re-encoding a valid profile build code from stale
        # structured nodes drops those bits and makes player details hide apex
        # talents after every refresh. Structured nodes are still used above as a
        # render fallback when the build code is clearly stale or missing.
        if build_code:
            merged_nodes.insert(0, {
                'tree_type': 'build_code',
                'talent_code': build_code,
                'talent_id': None,
                'spell_id': None,
                'points': 0,
            })
        return merged_nodes

    @staticmethod
    def _version_cache_key(talent_version=None, version_key='', usage=TalentVersionResolver.USAGE_SIMULATOR):
        if talent_version is not None:
            return getattr(talent_version, 'key', None) or getattr(talent_version, 'id', None) or 'provided'
        return str(version_key or '') or str(usage or '')

    @classmethod
    def _find_apex_compatible_version_payload(
        cls,
        build_code,
        class_name='',
        spec_name='',
        current_provider=None,
        current_full_nodes=None,
        current_decoder_nodes=None,
        current_decoded_states=None,
    ):
        """Find an active talent version that can decode apex points.

        12.1 import strings include bottom apex point pools. If the current
        player-tree default still points at an older retail metadata version, the
        decoder list is shorter/different and those apex nodes decode as 0/4.
        When no explicit version was requested, prefer another active version
        only if it actually decodes apex points while the current version does
        not.
        """
        if not build_code or not class_name:
            return None
        if cls._decoded_states_have_spec_apex_points(current_full_nodes or [], current_decoder_nodes or [], current_decoded_states or {}):
            return None

        current_key = getattr(getattr(current_provider, 'version', None), 'key', '') or getattr(current_provider, 'version_cache_key', '')
        try:
            candidates = TalentVersionResolver.list_active()
        except Exception:
            return None

        for version in candidates:
            candidate_key = getattr(version, 'key', '')
            if candidate_key and candidate_key == current_key:
                continue
            try:
                candidate_provider = TalentMetadataProvider(talent_version=version)
                candidate_decoder_nodes = candidate_provider.get_decoder_node_list(class_name)
                if not candidate_decoder_nodes:
                    continue
                candidate_states = TalentBuildCodeDecoder.decode_node_states(build_code, candidate_decoder_nodes)
                candidate_full_nodes = candidate_provider.get_full_tree_nodes(class_name, spec_name)
                if not candidate_full_nodes:
                    continue
                if not cls._decoded_states_have_spec_apex_points(candidate_full_nodes, candidate_decoder_nodes, candidate_states):
                    continue
                return {
                    'provider': candidate_provider,
                    'decoder_nodes': candidate_decoder_nodes,
                    'decoded_states': candidate_states,
                    'full_nodes': candidate_full_nodes,
                }
            except Exception:
                continue
        return None

    @staticmethod
    def _decoded_states_have_spec_apex_points(full_nodes, decoder_nodes, decoded_states):
        if not full_nodes or not decoder_nodes or not decoded_states:
            return False
        apex_aliases = set()
        for node in full_nodes:
            if not isinstance(node, dict) or not node.get('is_apex_talent'):
                continue
            for alias in TalentBuildCodeService._node_alias_keys_for_matching(node):
                apex_aliases.add(alias)
        if not apex_aliases:
            return False

        for node in decoder_nodes:
            if not isinstance(node, dict):
                continue
            node_aliases = TalentBuildCodeService._node_alias_keys_for_matching(node)
            if not (apex_aliases & node_aliases):
                continue
            state = decoded_states.get(_build_node_key(node)) or {}
            if bool(state.get('selected')) and int(state.get('points') or 0) > 0:
                return True
        return False

    @staticmethod
    def _decoded_states_have_apex_points(decoder_nodes, decoded_states):
        if not decoder_nodes or not decoded_states:
            return False
        for node in decoder_nodes:
            if not isinstance(node, dict):
                continue
            if not node.get('is_apex_talent'):
                continue
            state = decoded_states.get(_build_node_key(node)) or {}
            if bool(state.get('selected')) and int(state.get('points') or 0) > 0:
                return True
        return False

    @staticmethod
    def _prefer_structured_nodes_when_build_code_looks_stale(decoded_states, selected_nodes, decoder_nodes, class_name='', spec_name=''):
        """Fallback to structured talents when a stored build code is clearly stale.

        Some historical PlayerSpecTopPlayer/Ranking rows contain Blizzard import
        strings generated with a spec-only node list. Those strings decode with a
        much lower point total and commonly lose the entire hero subtree. The
        structured talents_json captured from WCL/Raider.IO still contains the
        correct selected nodes, so use it for rendering rather than letting the
        stale import string overwrite good data.
        """
        selected_lookup = {}
        for node in selected_nodes or []:
            key = TalentBuildCodeService._build_node_key(node)
            points = int((node or {}).get('points') or (node or {}).get('rank') or 0)
            if key and points > 0:
                selected_lookup[key] = dict(node, points=points, selected=True)
        if not selected_lookup:
            return decoded_states

        selected_total = sum(int(node.get('points') or 0) for node in selected_lookup.values())
        decoded_total = sum(int(state.get('points') or 0) for state in (decoded_states or {}).values())
        selected_hero = sum(
            int(node.get('points') or 0)
            for node in selected_lookup.values()
            if (node.get('tree_type') or 'spec') == 'hero'
        )
        decoded_hero = 0
        for node in decoder_nodes or []:
            if (node.get('tree_type') or 'spec') != 'hero':
                continue
            state = (decoded_states or {}).get(_build_node_key(node)) or {}
            decoded_hero += int(state.get('points') or 0)

        missing_multi_point_nodes = {
            key: node
            for key, node in selected_lookup.items()
            if int(node.get('points') or 0) > 1 and key not in (decoded_states or {})
        }

        decoded_alias_lookup = {}
        for node in decoder_nodes or []:
            state = (decoded_states or {}).get(_build_node_key(node))
            if not state:
                continue
            for alias in TalentBuildCodeService._node_alias_keys_for_matching(node):
                decoded_alias_lookup.setdefault(alias, state)
            for option in node.get('choice_options') or []:
                for alias in TalentBuildCodeService._node_alias_keys_for_matching(option):
                    decoded_alias_lookup.setdefault(alias, state)

        missing_structured_points = 0
        for node in selected_lookup.values():
            node_points = int(node.get('points') or 0)
            if node_points <= 0:
                continue
            decoded_state = None
            for alias in TalentBuildCodeService._node_alias_keys_for_matching(node):
                decoded_state = decoded_alias_lookup.get(alias)
                if decoded_state:
                    break
            decoded_points = int((decoded_state or {}).get('points') or 0)
            if decoded_points < node_points:
                missing_structured_points += node_points - decoded_points

        # Keep valid import strings authoritative. Only fall back when the code
        # is obviously stale/misaligned: it loses a hero subtree, decodes far
        # fewer points than the structured payload, omits a structured multi-rank
        # pool such as 12.1 apex talents, or decodes a similar total while many
        # concrete structured nodes are missing/zero by alias (node-order drift).
        missing_structured_threshold = max(5, int(selected_total * 0.1)) if selected_total else 5
        has_structured_misalignment = missing_structured_points >= missing_structured_threshold
        if not ((selected_hero > 0 and decoded_hero < selected_hero) or (selected_total and decoded_total < selected_total - 5) or missing_multi_point_nodes or has_structured_misalignment):
            return decoded_states

        structured_states = {
            key: {
                'selected': True,
                'points': int(node.get('points') or 0),
                'is_choice_node': bool(node.get('is_choice_node')),
                'choice_selection': int(node.get('choice_selection') or 0) if node.get('choice_selection') is not None else 0,
            }
            for key, node in selected_lookup.items()
        }
        if missing_multi_point_nodes and not ((selected_hero > 0 and decoded_hero == 0) or (selected_total and decoded_total < selected_total - 5)):
            merged_states = dict(decoded_states or {})
            merged_states.update({key: structured_states[key] for key in missing_multi_point_nodes})
            return merged_states
        return structured_states

    @staticmethod
    @lru_cache(maxsize=256)
    def _build_view_model_cached(class_name, spec_name, version_cache_key, payload_key):
        payload = json.loads(payload_key)
        return build_talent_view_model(payload, class_name=class_name, spec_name=spec_name)

    @staticmethod
    def _extract_build_code_from_payload(payload):
        for node in payload or []:
            if isinstance(node, dict):
                value = TalentBuildCodeService._extract_build_code_from_node(node)
                if value:
                    return value
        return ''

    @staticmethod
    def _extract_build_code_from_node(node):
        if not isinstance(node, dict):
            return ''
        return str(
            node.get('talent_code')
            or node.get('build_code')
            or node.get('talentBuildCode')
            or ''
        ).strip()

    @staticmethod
    def _merge_full_tree_nodes(full_nodes, selected_nodes, decoded_states=None, has_build_code=False, decoder_nodes=None):
        if not full_nodes:
            return [dict(item) for item in selected_nodes]

        decoded_states = decoded_states or {}
        selected_lookup = {}
        selected_alias_lookup = {}
        for node in selected_nodes:
            key = TalentBuildCodeService._build_node_key(node)
            if key:
                selected_lookup[key] = dict(node)
            for alias in TalentBuildCodeService._node_alias_keys_for_matching(node):
                selected_alias_lookup.setdefault(alias, dict(node))

        decoded_alias_lookup = {}
        if decoded_states:
            alias_source_nodes = list(full_nodes or []) + list(decoder_nodes or []) + list(selected_nodes or [])
            for node in alias_source_nodes:
                state = decoded_states.get(TalentBuildCodeService._build_node_key(node))
                if state:
                    for alias in TalentBuildCodeService._node_alias_keys_for_matching(node):
                        decoded_alias_lookup.setdefault(alias, state)
                    for option in node.get('choice_options') or []:
                        for alias in TalentBuildCodeService._node_alias_keys_for_matching(option):
                            decoded_alias_lookup.setdefault(alias, state)

        merged = []
        for base_node in full_nodes:
            payload = dict(base_node)
            key = TalentBuildCodeService._build_node_key(payload)
            selected_node = selected_lookup.get(key) or TalentBuildCodeService._find_selected_node_by_alias(payload, selected_alias_lookup)
            decoded_state = decoded_states.get(key) or TalentBuildCodeService._find_decoded_state_by_alias(payload, decoded_alias_lookup) or {}

            # metadata enrichment from raw talents_json (name/icon/max_points/parents)
            if selected_node:
                if selected_node.get('display_spell_id'):
                    payload['display_spell_id'] = selected_node.get('display_spell_id')
                    payload['spell_id'] = selected_node.get('display_spell_id')
                elif selected_node.get('spell_id'):
                    payload['display_spell_id'] = selected_node.get('spell_id')
                    payload['spell_id'] = selected_node.get('spell_id')
                for field_name in ('name', 'icon', 'parents', 'choice_options', 'is_choice_node'):
                    if selected_node.get(field_name) not in (None, '', []):
                        payload[field_name] = selected_node.get(field_name)
                if selected_node.get('max_points') not in (None, '', []):
                    try:
                        selected_max_points = int(selected_node.get('max_points') or 0)
                        base_max_points = int(payload.get('max_points') or 0)
                    except (TypeError, ValueError):
                        selected_max_points = 0
                        base_max_points = 0
                    if selected_max_points > base_max_points:
                        payload['max_points'] = selected_max_points

            # selection state priority: build code decode > raw talents_json
            if has_build_code:
                # build code 解码是唯一权威来源
                if decoded_state:
                    payload['points'] = decoded_state.get('points', 0)
                    payload['selected'] = bool(decoded_state.get('selected', False))
                    if decoded_state.get('is_choice_node'):
                        payload['is_choice_node'] = True
                    if payload.get('choice_options') and decoded_state.get('is_choice_node') and decoded_state.get('choice_selection') is not None:
                        selected_index = int(decoded_state.get('choice_selection') or 0)
                        options = payload.get('choice_options') or []
                        if 0 <= selected_index < len(options):
                            selected_option = options[selected_index]
                            if selected_option.get('display_spell_id'):
                                payload['display_spell_id'] = selected_option.get('display_spell_id')
                            if selected_option.get('spell_id'):
                                payload['spell_id'] = selected_option.get('spell_id')
                            if selected_option.get('talent_id'):
                                payload['talent_id'] = selected_option.get('talent_id')
                else:
                    # build code 可用但节点未解码 → 明确标为未选中
                    payload['points'] = 0
                    payload['selected'] = False
            elif selected_node:
                # no build code available: raw talents_json is the only source
                payload['points'] = selected_node.get('points', payload.get('points', 0))
                payload['selected'] = bool(selected_node.get('selected', payload.get('points', 0) > 0))
            merged.append(payload)

        return merged

    @staticmethod
    def _find_selected_node_by_alias(node, selected_alias_lookup):
        if not selected_alias_lookup:
            return None
        for alias in TalentBuildCodeService._node_alias_keys_for_matching(node):
            selected_node = selected_alias_lookup.get(alias)
            if selected_node:
                return selected_node
        for option in (node or {}).get('choice_options') or []:
            for alias in TalentBuildCodeService._node_alias_keys_for_matching(option):
                selected_node = selected_alias_lookup.get(alias)
                if selected_node:
                    return selected_node
        return None

    @staticmethod
    def _find_decoded_state_by_alias(node, decoded_alias_lookup):
        if not decoded_alias_lookup:
            return None
        for alias in TalentBuildCodeService._node_alias_keys_for_matching(node):
            state = decoded_alias_lookup.get(alias)
            if state:
                return state
        for option in (node or {}).get('choice_options') or []:
            for alias in TalentBuildCodeService._node_alias_keys_for_matching(option):
                state = decoded_alias_lookup.get(alias)
                if state:
                    return state
        return None

    @staticmethod
    def _node_alias_keys_for_matching(node):
        keys = set(_node_alias_keys(dict(node or {}, tree_type=(node or {}).get('tree_type') or 'spec')))
        # Hero anchor apex nodes and spec render apex nodes can represent the
        # same TraitNode/Spell while using different tree_type labels. Match
        # across both labels so decoded build-code state is not lost during
        # render-tree filtering/aggregation.
        for tree_type in ('class', 'spec', 'hero', 'hero_anchor'):
            for alias in _node_alias_keys(dict(node or {}, tree_type=tree_type)):
                keys.add(alias)
        return keys

    @staticmethod
    def _build_node_key(node):
        if not isinstance(node, dict):
            return ''
        tree_type = node.get('tree_type') or 'spec'
        node_identity = node.get('node_id') or node.get('talent_id') or node.get('spell_id') or node.get('display_spell_id')
        return f'{tree_type}:{node_identity}' if node_identity else ''
