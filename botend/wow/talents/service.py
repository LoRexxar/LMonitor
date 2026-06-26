# -*- coding: utf-8 -*-

import copy
import json
from functools import lru_cache

from botend.wow.talents.build_code import TalentBuildCodeDecoder, TalentBuildCodeEncoder
from botend.wow.talents.metadata import TalentMetadataProvider
from botend.wow.talents.parser import normalize_talent_payload
from botend.wow.talents.view_model import build_talent_view_model


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
    def build_api_view(cls, talent_build_code='', talents_json=None, class_name='', spec_name=''):
        build_code = cls.extract_build_code(talent_build_code, talents_json)
        if not build_code and talents_json:
            build_code = cls.encode_build_code_from_nodes(talents_json, class_name=class_name, spec_name=spec_name)
        payload = cls.build_full_payload(
            class_name=class_name,
            spec_name=spec_name,
            talent_build_code=build_code,
            talents_json=talents_json,
        )
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
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            )
        except Exception:
            return {
                'talent_build_code': build_code,
                'has_talent_build_code': bool(build_code),
                'talent_parse_status': 'failed',
                'talent_view_model': {},
                'talent_render_model': {'build_code': build_code},
            }

        render_model = copy.deepcopy(view_model.get('render_model') or {})
        render_model['build_code'] = build_code
        response_view_model = copy.deepcopy(view_model)
        response_view_model['build_code'] = build_code
        response_view_model['render_model'] = render_model
        return {
            'talent_build_code': build_code,
            'has_talent_build_code': bool(build_code),
            'talent_parse_status': 'success' if (build_code or response_view_model.get('nodes')) else 'missing',
            'talent_view_model': response_view_model,
            'talent_render_model': render_model,
        }

    @classmethod
    def encode_build_code_from_nodes(cls, talents_json=None, class_name='', spec_name='', reference_build_code=''):
        selected_nodes = cls._normalize_nodes_for_encoding(talents_json, class_name=class_name, spec_name=spec_name)
        if not selected_nodes:
            return ''

        provider = TalentMetadataProvider()
        full_nodes = provider.get_full_tree_nodes(class_name, spec_name)
        if not full_nodes:
            return ''

        reference = str(reference_build_code or '').strip() or cls._find_reference_build_code(class_name, spec_name)
        if not reference:
            return ''
        return TalentBuildCodeEncoder.encode_node_states(reference, full_nodes, selected_nodes)

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
    def build_full_payload(cls, class_name='', spec_name='', talent_build_code='', talents_json=None):
        build_code = cls.extract_build_code(talent_build_code, talents_json)
        if not build_code and talents_json:
            build_code = cls.encode_build_code_from_nodes(talents_json, class_name=class_name, spec_name=spec_name)
        payload_model = normalize_talent_payload(talents_json or [], class_name=class_name, spec_name=spec_name)
        selected_nodes = []
        for item in payload_model.get('nodes') or []:
            if not isinstance(item, dict):
                continue
            if cls._extract_build_code_from_node(item):
                continue
            selected_nodes.append(dict(item))

        provider = TalentMetadataProvider()
        full_nodes = provider.get_full_tree_nodes(class_name, spec_name)
        decoded_states = TalentBuildCodeDecoder.decode_node_states(build_code, full_nodes) if build_code and full_nodes else {}
        merged_nodes = cls._merge_full_tree_nodes(full_nodes, selected_nodes, decoded_states=decoded_states) if full_nodes else selected_nodes
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
    @lru_cache(maxsize=256)
    def _build_view_model_cached(class_name, spec_name, payload_key):
        payload = json.loads(payload_key)
        return build_talent_view_model(payload, class_name=class_name, spec_name=spec_name)

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
    def _merge_full_tree_nodes(full_nodes, selected_nodes, decoded_states=None):
        if not full_nodes:
            return [dict(item) for item in selected_nodes]

        decoded_states = decoded_states or {}
        selected_lookup = {}
        for node in selected_nodes:
            key = TalentBuildCodeService._build_node_key(node)
            if key:
                selected_lookup[key] = dict(node)

        merged = []
        for base_node in full_nodes:
            payload = dict(base_node)
            key = TalentBuildCodeService._build_node_key(payload)
            selected_node = selected_lookup.get(key)
            decoded_state = decoded_states.get(key) or {}
            if selected_node:
                payload['points'] = selected_node.get('points', payload.get('points', 0))
                payload['selected'] = bool(selected_node.get('selected', payload.get('points', 0) > 0))
                if selected_node.get('display_spell_id'):
                    payload['display_spell_id'] = selected_node.get('display_spell_id')
                    payload['spell_id'] = selected_node.get('display_spell_id')
                for field_name in ('name', 'icon', 'max_points', 'parents', 'choice_options', 'is_choice_node'):
                    if selected_node.get(field_name) not in (None, '', []):
                        payload[field_name] = selected_node.get(field_name)
            else:
                payload['points'] = decoded_state.get('points', payload.get('points', 0) or 0)
                payload['selected'] = bool(decoded_state.get('selected', False))
                if decoded_state.get('is_choice_node'):
                    payload['is_choice_node'] = True
                if payload.get('choice_options') and decoded_state.get('choice_selection') is not None:
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
            merged.append(payload)

        return merged

    @staticmethod
    def _build_node_key(node):
        if not isinstance(node, dict):
            return ''
        tree_type = node.get('tree_type') or 'spec'
        node_identity = node.get('node_id') or node.get('talent_id') or node.get('spell_id') or node.get('display_spell_id')
        return f'{tree_type}:{node_identity}' if node_identity else ''
