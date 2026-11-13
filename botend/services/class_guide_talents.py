"""Shared article boundary: never expose a planner embed as a game import code."""
import re

from botend.constants.wow import SPEC_IDENTITY_MAP
from botend.services.class_guide_codec import decode_alphabet


def prepare_talent_data(data, *, expected_spec=None):
    """Project copyable code without mutating the stored source dictionary.

    Native headers prove format/identity only, not current patch or game validity.
    Maxroll embeds require their exact historical tree and the source adapter.
    """
    result = dict(data)
    candidate = str(data.get('code') or data.get('source_code') or '')
    result.update(code='', converted=False)
    result.pop('conversion_error', None)
    try:
        if not re.fullmatch(r'[A-Za-z0-9+/_-]{26,4096}', candidate):
            raise ValueError('天赋代码缺少完整导入头或字符无效')
        normalized = candidate.replace('-', '+').replace('_', '/')
        raw = decode_alphabet(normalized)
        version, spec = raw[0], int.from_bytes(raw[1:3], 'little')
        if spec not in SPEC_IDENTITY_MAP or (expected_spec is not None and spec != expected_spec):
            raise ValueError('天赋代码专精与文章不符或无法识别')
        if version == 130:
            from botend.wow.talents.maxroll import convert_maxroll_code
            result.setdefault('source_code', candidate)
            normalized = convert_maxroll_code(candidate, expected_spec=expected_spec)
        elif version != 2:
            raise ValueError('暂不支持此天赋来源编码版本')
        result.update(code=normalized, converted=True)
    except (ValueError, OSError) as exc:
        result.setdefault('source_code', candidate)
        result['conversion_error'] = '天赋代码暂不可用：' + str(exc)
    return result
