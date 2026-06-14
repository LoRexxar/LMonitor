# -*- coding: utf-8 -*-

import copy
import json
from functools import lru_cache

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
        payload = cls._build_payload(build_code, talents_json)
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
    def _build_payload(cls, build_code, talents_json):
        payload = []

        if isinstance(talents_json, list):
            for item in talents_json:
                if not isinstance(item, dict):
                    continue
                normalized = dict(item)
                item_build_code = cls._extract_build_code_from_node(normalized)
                if item_build_code:
                    continue
                payload.append(normalized)

        if build_code:
            payload.insert(0, {
                'tree_type': 'build_code',
                'talent_code': build_code,
                'talent_id': None,
                'spell_id': None,
                'points': 0,
            })
        return payload

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
