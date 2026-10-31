"""Bounded-text I/O for [actor, unresolved, raw_action_count] handoffs.

The decoded graph is still materialized. On the normal streaming path, extra
wire text is O(read size + largest scalar); geometric scalar growth can retain
roughly twice the scalar size. A per-document table shares object keys, as in
stdlib, and is cleared on completion. Stdlib's C JSON decoder handles complete
small subtrees and every scalar: no Decimal, float conversion, key sorting, or
alternative NaN/surrogate policy is applied.

Rare deeply nested inputs can exhaust the Python streaming parser's recursion
before stdlib's C decoder would. Seekable streams then rewind to their original
offset and use the stdlib decoder directly: this compatibility fallback is NOT text-memory-bounded
and can materialize the entire remaining document. Nonseekable streams retain
bounded reads but may raise RecursionError earlier than json.loads. Neither path
changes the interpreter recursion limit or the normal production read bounds.
"""
import json
from pathlib import Path
import re

from botend.services.simc_json_stream import iter_json_text

_READ_SIZE = 65536
_WHITESPACE = re.compile(r'[ \t\n\r]*')
_NUMBER = re.compile(r'[0-9eE+.\-]+')


def write_profile_product(path, product):
    """Keep the existing compact UTF-8 wire, without a whole-product str/bytes."""
    with Path(path).open('w', encoding='utf-8') as output:
        output.writelines(iter_json_text(product, ensure_ascii=False))


def read_profile_product(source):
    """Read a path or text stream, requiring one three-element array and EOF.

    Product/identity validation remains the caller's responsibility. Duplicate
    keys retain json.loads' last-value-wins and original insertion order. Escaped
    lone surrogates, arbitrary integers (including the interpreter digit limit),
    binary floats/negative zero, NaN and infinities use stdlib semantics. Raw
    invalid UTF-8 is rejected by the same strict text decoding as read_text.
    """
    if hasattr(source, 'read'):
        return _read_stream(source)
    with Path(source).open('r', encoding='utf-8') as stream:
        return _read_stream(stream)


def _validate_product(product):
    if not isinstance(product, list) or len(product) != 3:
        raise ValueError('必须是 [actor, unresolved, raw_action_count]')
    return product


def _read_stream(stream):
    try:
        start = stream.tell() if stream.seekable() else None
    except (AttributeError, OSError):
        start = None
    try:
        return _JSONReader(stream).read()
    except RecursionError:
        if start is None:
            raise
        stream.seek(start)
    # Leave the exception handler first so its recursive partial graph and
    # traceback are released before the unbounded compatibility allocation.
    # Decode directly at the legacy call depth. json.load -> loads adds two
    # wrapper frames and would reject some arrays that json.loads accepted.
    text = stream.read()
    start = _WHITESPACE.match(text).end()
    product, end = json.JSONDecoder().raw_decode(text, start)
    if _WHITESPACE.match(text, end).end() != len(text):
        raise json.JSONDecodeError('Extra data', text, end)
    return _validate_product(product)


class _JSONReader:
    def __init__(self, stream):
        self.stream = stream
        self.buffer = ''
        self.pos = 0
        self.eof = False
        self.key_memo = memo = {}

        def object_pairs(pairs):
            return {memo.setdefault(key, key): value for key, value in pairs}

        # raw_decode resets its own memo after each subtree. Share keys across
        # those subtrees AND _object, without global interning or a self cycle.
        self.decoder = json.JSONDecoder(object_pairs_hook=object_pairs)

    def _ensure(self, count):
        """Retain only unconsumed text; every physical read has a fixed cap."""
        available = len(self.buffer) - self.pos
        if available >= count or self.eof:
            return
        pieces = [self.buffer[self.pos:]]
        while available < count and not self.eof:
            piece = self.stream.read(_READ_SIZE)
            if not piece:
                self.eof = True
            else:
                pieces.append(piece)
                available += len(piece)
        self.buffer = ''.join(pieces)
        self.pos = 0

    def _peek(self):
        self._ensure(1)
        return self.buffer[self.pos:self.pos + 1]

    def _skip_space(self):
        while True:
            self.pos = _WHITESPACE.match(self.buffer, self.pos).end()
            if self.pos < len(self.buffer) or self.eof:
                return
            self._ensure(1)

    def _error(self, message):
        raise json.JSONDecodeError(message, self.buffer, self.pos)

    def _take(self, character):
        self._skip_space()
        if self._peek() != character:
            self._error(f'Expecting {character!r}')
        self.pos += 1

    def _string(self):
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer, self.pos)
            except json.JSONDecodeError as exc:
                # A split escape can report an invalid unicode escape rather
                # than an unterminated string. Other malformed strings fail now.
                incomplete = (
                    exc.msg.startswith('Unterminated string')
                    or (exc.msg == 'Invalid \\uXXXX escape'
                        and len(self.buffer) - exc.pos < 6)
                )
                if self.eof or not incomplete:
                    raise
            else:
                self.pos = end
                return value
            # Geometric retries keep a large individual scalar linear in work.
            self._ensure(max(2, 2 * (len(self.buffer) - self.pos)))

    def _number(self):
        end = self.pos
        while True:
            match = _NUMBER.match(self.buffer, end)
            if match:
                end = match.end()
            if end < len(self.buffer) or self.eof:
                break
            previous = end - self.pos
            self._ensure(max(2, 2 * previous))
            end = self.pos + previous
        # decode, not float()/Decimal: preserve integers, -0 and -0.0, overflow,
        # and rejection of leading zeroes/incomplete exponents/invalid signs.
        value = self.decoder.decode(self.buffer[self.pos:end])
        self.pos = end
        return value

    def _value(self):
        self._skip_space()
        character = self._peek()
        if character in ('[', '{'):
            try:
                value, end = self.decoder.raw_decode(self.buffer, self.pos)
            except json.JSONDecodeError:
                pass
            else:
                self.pos = end
                return value
            # Never grow text for an aggregate. Descend only when the current
            # bounded window cannot be decoded as one complete subtree.
            return self._array() if character == '[' else self._object()
        if character == '"':
            return self._string()
        if character == '-':
            self._ensure(2)
            if self.buffer[self.pos:self.pos + 2] == '-I':
                self._ensure(9)
                value, self.pos = self.decoder.raw_decode(self.buffer, self.pos)
                return value
        if character and character in '-0123456789':
            return self._number()
        literal_size = {'t': 4, 'f': 5, 'n': 4, 'N': 3, 'I': 8}.get(character)
        if literal_size:
            self._ensure(literal_size)
            value, self.pos = self.decoder.raw_decode(self.buffer, self.pos)
            return value
        self._error('Expecting value')

    def _array(self):
        self._take('[')
        result = []
        self._skip_space()
        if self._peek() == ']':
            self.pos += 1
            return result
        while True:
            result.append(self._value())
            self._skip_space()
            if self._peek() == ']':
                self.pos += 1
                return result
            self._take(',')

    def _object(self):
        self._take('{')
        result = {}
        self._skip_space()
        if self._peek() == '}':
            self.pos += 1
            return result
        while True:
            self._skip_space()
            if self._peek() != '"':
                self._error('Expecting property name enclosed in double quotes')
            key = self._string()
            key = self.key_memo.setdefault(key, key)
            self._take(':')
            result[key] = self._value()
            self._skip_space()
            if self._peek() == '}':
                self.pos += 1
                return result
            self._take(',')

    def read(self):
        try:
            product = self._value()
            self._skip_space()
            if self._peek():
                self._error('Extra data')
            return _validate_product(product)
        finally:
            self.key_memo.clear()
