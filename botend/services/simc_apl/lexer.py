"""Line lexer for the top-level SimC APL language."""

import re
from dataclasses import dataclass
from enum import Enum
from typing import Tuple

from .ast import SourcePosition, SourceRange


_LINE_BREAK_RE = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")


class LineKind(Enum):
    BLANK = "blank"
    COMMENT = "comment"
    ACTION = "action"
    INVALID = "invalid"


@dataclass(frozen=True)
class LineToken:
    kind: LineKind
    text: str
    range: SourceRange


def _source_lines(source: str) -> list[str]:
    """Return logical lines without inventing a line after a final newline."""
    if not source:
        return []
    lines = _LINE_BREAK_RE.split(source)
    # A trailing newline terminates the preceding logical line; it does not add
    # another line node. The document range still points to the next line.
    if lines[-1] == "":
        lines.pop()
    return lines


def source_end_position(source: str) -> SourcePosition:
    """Return the end position using the lexer's logical line boundaries."""
    line = 1
    offset = 0
    for match in _LINE_BREAK_RE.finditer(source):
        line += 1
        offset = match.end()
    return SourcePosition(line, len(source) - offset + 1)


def _has_closed_quotes(text: str) -> bool:
    quote = None
    escaped = False
    for char in text:
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
    return quote is None


def lex(source: str) -> Tuple[LineToken, ...]:
    tokens = []
    for line_number, text in enumerate(_source_lines(source), start=1):
        stripped = text.lstrip()
        if not stripped:
            kind = LineKind.BLANK
        elif stripped.startswith("#"):
            kind = LineKind.COMMENT
        elif stripped.startswith("actions") and _has_closed_quotes(stripped):
            kind = LineKind.ACTION
        else:
            kind = LineKind.INVALID
        tokens.append(LineToken(
            kind=kind,
            text=text,
            range=SourceRange(
                SourcePosition(line_number, 1),
                SourcePosition(line_number, len(text) + 1),
            ),
        ))
    return tuple(tokens)
