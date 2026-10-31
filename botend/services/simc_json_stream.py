"""Compact stdlib JSON with bounded aggregate fragments and the C encoder.

Large containers are split; scalar strings retain stdlib's scalar-size cost.
No full actor JSON string or long-lived cache is constructed.
"""
import json

_MAX_NODES = 1024
_MAX_CHARACTERS = 131072
_ARRAY_BATCH = 8


def _bounded_aggregate(value):
    pending = [value]
    remaining = _MAX_NODES - 1
    characters = _MAX_CHARACTERS
    while pending:
        item = pending.pop()
        kind = type(item)
        if kind is str:
            characters -= len(item)
            if characters < 0:
                return False
        elif kind is dict or isinstance(item, dict):
            count = 2 * len(item)
            if count > remaining:
                return False
            remaining -= count
            pending.extend(item)
            pending.extend(item.values())
        elif kind is list or kind is tuple or isinstance(item, (list, tuple)):
            count = len(item)
            if count > remaining:
                return False
            remaining -= count
            pending.extend(item)
        elif isinstance(item, str):
            characters -= len(item)
            if characters < 0:
                return False
    return True


def iter_json_text(value, *, ensure_ascii=False, allow_nan=True):
    """Yield the same compact text as json.dumps without aggregating a graph."""
    encoder = json.JSONEncoder(
        ensure_ascii=ensure_ascii, allow_nan=allow_nan, separators=(',', ':'),
    )
    active = set()

    def encode_key(key):
        if isinstance(key, str):
            return encoder.encode(key)
        if key is None:
            return '"null"'
        if isinstance(key, (int, float)):
            return encoder.encode(encoder.encode(key))
        raise TypeError(
            'keys must be str, int, float, bool or None, not '
            + type(key).__name__
        )

    def fragments(item):
        if _bounded_aggregate(item):
            yield from encoder.iterencode(item, _one_shot=True)
            return
        identity = id(item)
        if identity in active:
            raise ValueError('Circular reference detected')
        if isinstance(item, dict):
            active.add(identity)
            yield '{'
            try:
                for ordinal, (key, child) in enumerate(item.items()):
                    if ordinal:
                        yield ','
                    yield encode_key(key)
                    yield ':'
                    yield from fragments(child)
            finally:
                active.remove(identity)
            yield '}'
        elif isinstance(item, (list, tuple)):
            active.add(identity)
            yield '['
            try:
                for start in range(0, len(item), _ARRAY_BATCH):
                    batch = item[start:start + _ARRAY_BATCH]
                    if start:
                        yield ','
                    if _bounded_aggregate(batch):
                        yield encoder.encode(batch)[1:-1]
                    else:
                        for ordinal, child in enumerate(batch):
                            if ordinal:
                                yield ','
                            yield from fragments(child)
            finally:
                active.remove(identity)
            yield ']'
        else:
            yield from encoder.iterencode(item, _one_shot=True)

    yield from fragments(value)
