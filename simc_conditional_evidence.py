"""Conditional evidence wire protocol v1 (stdlib only).

Version is carried by the private storage namespace/metadata, not the envelope.
The envelope has exactly two UTF-8 text fields; neither text is normalized.
This is transport validation, not proof that both fields came from the same run.
"""
from __future__ import annotations

import gzip
import json
import math
import zlib

PROTOCOL_VERSION = 1
MAX_PREPARED_BYTES = 1 * 1024 * 1024
MAX_REPORT_BYTES = 20 * 1024 * 1024
MAX_ENVELOPE_BYTES = 32 * 1024 * 1024
MAX_COMPRESSED_BYTES = 8 * 1024 * 1024
CHUNK_BYTES = 64 * 1024


class EvidenceError(ValueError):
    """Invalid, ambiguous or oversized evidence; messages never contain inputs."""


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError('Duplicate JSON key')
        result[key] = value
    return result


def _constant(_):
    raise EvidenceError('Non-finite JSON number')


def _float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise EvidenceError('Non-finite JSON number')
    return parsed


def _json(text):
    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_float)
    except (ValueError, RecursionError, OverflowError):
        raise EvidenceError('Invalid JSON document') from None


def _text(value, limit):
    if not isinstance(value, str) or len(value) > limit:
        raise EvidenceError('Invalid or oversized evidence text')
    try:
        if len(value.encode('utf-8')) > limit:
            raise EvidenceError('Oversized UTF-8 evidence text')
    except UnicodeError:
        raise EvidenceError('Invalid UTF-8 evidence text') from None


def _validate(value):
    if not isinstance(value, dict) or set(value) != {'prepared_input', 'report_json'}:
        raise EvidenceError('Invalid v1 envelope fields')
    _text(value['prepared_input'], MAX_PREPARED_BYTES)
    _text(value['report_json'], MAX_REPORT_BYTES)
    if not isinstance(_json(value['report_json']), dict):
        raise EvidenceError('Report JSON must be an object')
    return value


def encode(prepared_input: str, report_json: str) -> bytes:
    """Return a single gzip member containing the v1 UTF-8 JSON envelope."""
    value = _validate({'prepared_input': prepared_input, 'report_json': report_json})
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    raw = bytearray()
    for chunk in encoder.iterencode(value):
        data = chunk.encode('utf-8')
        if len(raw) + len(data) > MAX_ENVELOPE_BYTES:
            raise EvidenceError('Oversized evidence envelope')
        raw.extend(data)
    compressed = gzip.compress(raw, mtime=0)
    if len(compressed) > MAX_COMPRESSED_BYTES:
        raise EvidenceError('Oversized compressed evidence')
    return compressed


def decode(compressed: bytes) -> dict[str, str]:
    """Bound output before allocation; reject extra members/trailing bytes."""
    if not isinstance(compressed, bytes) or not 0 < len(compressed) <= MAX_COMPRESSED_BYTES:
        raise EvidenceError('Invalid compressed evidence size')
    inflater = zlib.decompressobj(wbits=31)
    try:
        raw = inflater.decompress(compressed, MAX_ENVELOPE_BYTES + 1)
        if len(raw) > MAX_ENVELOPE_BYTES or inflater.unconsumed_tail:
            raise EvidenceError('Oversized evidence envelope')
        if not inflater.eof or inflater.unused_data:
            raise EvidenceError('Truncated or trailing gzip data')
        text = raw.decode('utf-8')
    except (zlib.error, UnicodeError):
        raise EvidenceError('Invalid gzip or UTF-8 evidence') from None
    return _validate(_json(text))
