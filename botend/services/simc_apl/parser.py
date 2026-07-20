"""Top-level parser for SimulationCraft action priority lists."""

import re
from typing import Iterator, Optional, Tuple

from .ast import (
    ActionAssignment,
    ActionEntry,
    BlankLine,
    CommentLine,
    Document,
    InvalidActionEntry,
    InvalidLine,
    InvalidOption,
    Option,
    ParseIssue,
    SourcePosition,
    SourceRange,
)
from .lexer import LineKind, LineToken, lex, source_end_position

_ASSIGNMENT_RE = re.compile(
    r"actions(?:\.(?P<list>[A-Za-z_][A-Za-z0-9_]*))?"
    r"(?P<operator>\+=|=)(?P<body>.*)\Z"
)
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_CLOSING_BRACKET = {"(": ")", "[": "]", "{": "}"}


def _range(line: int, start_offset: int, end_offset: int) -> SourceRange:
    return SourceRange(
        SourcePosition(line, start_offset + 1),
        SourcePosition(line, end_offset + 1),
    )


def _split_top_level(text: str, delimiter: str) -> Iterator[Tuple[int, int]]:
    """Yield slices, ignoring delimiters in correctly nested brackets or quotes."""
    start = 0
    brackets = []
    quote: Optional[str] = None
    escaped = False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char in _CLOSING_BRACKET:
            brackets.append(char)
        elif char in ")]}" and brackets:
            # A mismatched closer must not lower nesting and expose delimiters.
            if _CLOSING_BRACKET[brackets[-1]] == char:
                brackets.pop()
        elif char == delimiter and not brackets:
            yield start, index
            start = index + 1
    yield start, len(text)


def _parse_option(text: str, line: int, absolute_start: int):
    option_range = _range(line, absolute_start, absolute_start + len(text))
    equals = text.find("=")
    if equals <= 0:
        return InvalidOption(
            raw=text,
            range=option_range,
            code="invalid-option",
            message="Expected an option in name=value form.",
        )
    name = text[:equals]
    if not _NAME_RE.fullmatch(name):
        return InvalidOption(
            raw=text,
            range=option_range,
            code="invalid-option",
            message="Expected a valid option name before '='.",
        )
    return Option(
        name=name,
        value=text[equals + 1:],
        range=option_range,
        name_range=_range(line, absolute_start, absolute_start + equals),
        value_range=_range(
            line, absolute_start + equals + 1, absolute_start + len(text)),
    )


def _parse_entry(text: str, line: int, absolute_start: int):
    pieces = list(_split_top_level(text, ","))
    name_start, name_end = pieces[0]
    name = text[name_start:name_end]
    entry_range = _range(line, absolute_start, absolute_start + len(text))
    if not _NAME_RE.fullmatch(name):
        return InvalidActionEntry(
            raw=text,
            range=entry_range,
            code="invalid-action-entry",
            message="Expected an action name.",
        )
    options = []
    for position, (start, end) in enumerate(pieces[1:], start=1):
        # Current upstream contains a tolerated trailing comma on an action.
        if start == end == len(text) and position == len(pieces) - 1:
            continue
        options.append(_parse_option(
            text[start:end], line, absolute_start + start))
    return ActionEntry(
        name=name,
        options=tuple(options),
        range=entry_range,
        name_range=_range(
            line, absolute_start + name_start, absolute_start + name_end),
    )


def _structure_issues(text: str, line: int, absolute_start: int):
    issues = []
    brackets = []
    quote: Optional[str] = None
    escaped = False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char in _CLOSING_BRACKET:
            brackets.append((char, index))
        elif char in ")]}" and brackets and _CLOSING_BRACKET[brackets[-1][0]] == char:
            brackets.pop()
        elif char in ")]}":
            issues.append(ParseIssue(
                code="mismatched-closing-bracket",
                message="Closing bracket does not match the current opening bracket.",
                range=_range(
                    line, absolute_start + index, absolute_start + index + 1),
            ))
    for _, index in brackets:
        issues.append(ParseIssue(
            code="unclosed-bracket",
            message="Opening bracket is not closed.",
            range=_range(line, absolute_start + index, absolute_start + index + 1),
        ))
    return issues


def _parse_assignment(token: LineToken):
    indent_length = len(token.text) - len(token.text.lstrip())
    content = token.text[indent_length:]
    match = _ASSIGNMENT_RE.fullmatch(content)
    if not match:
        return None
    body = match.group("body")
    body_offset = indent_length + match.start("body")
    if body.startswith("/"):
        body = body[1:]
        body_offset += 1
    if not body:
        return None

    actions = [
        _parse_entry(body[start:end], token.range.start.line, body_offset + start)
        for start, end in _split_top_level(body, "/")
    ]

    list_name = match.group("list")
    list_name_range = None
    if list_name is not None:
        list_name_range = _range(
            token.range.start.line,
            indent_length + match.start("list"),
            indent_length + match.end("list"),
        )
    assignment = ActionAssignment(
        text=token.text,
        indent=token.text[:indent_length],
        list_name=list_name,
        operator=match.group("operator"),
        actions=tuple(actions),
        range=_range(
            token.range.start.line, indent_length, len(token.text)),
        list_name_range=list_name_range,
    )
    issues = _structure_issues(body, token.range.start.line, body_offset)
    for action in actions:
        if isinstance(action, InvalidActionEntry):
            issues.append(action.issue)
        else:
            issues.extend(
                option.issue for option in action.options
                if isinstance(option, InvalidOption)
            )
    return assignment, issues


def _invalid(token: LineToken) -> InvalidLine:
    issue = ParseIssue(
        code="invalid-line",
        message="Expected an APL action assignment, blank line, or whole-line comment.",
        range=token.range,
    )
    return InvalidLine(text=token.text, range=token.range, issue=issue)


def parse(source: str) -> Document:
    lines = []
    issues = []
    for token in lex(source):
        if token.kind == LineKind.BLANK:
            node = BlankLine(token.text, token.range)
        elif token.kind == LineKind.COMMENT:
            node = CommentLine(token.text, token.range)
        elif token.kind == LineKind.ACTION:
            result = _parse_assignment(token)
            if result is None:
                node = _invalid(token)
            else:
                node, local_issues = result
                issues.extend(local_issues)
        else:
            node = _invalid(token)
        lines.append(node)
        if isinstance(node, InvalidLine):
            issues.append(node.issue)
    return Document(
        source=source,
        lines=tuple(lines),
        range=SourceRange(SourcePosition(1, 1), source_end_position(source)),
        issues=tuple(issues),
    )
