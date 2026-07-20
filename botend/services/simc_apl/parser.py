"""Top-level parser for SimulationCraft action priority lists."""

import re
from dataclasses import replace
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
from .expression import parse_expression
from .lexer import LineKind, LineToken, lex, source_end_position

_ASSIGNMENT_RE = re.compile(
    r"actions(?:\.(?P<list>[A-Za-z_][A-Za-z0-9_]*))?"
    r"(?P<operator>\+=|=)(?P<body>.*)\Z"
)
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_CLOSING_BRACKET = {"(": ")", "[": "]", "{": "}"}
_GENERAL_EXPRESSION_OPTIONS = {
    "if", "target_if", "interrupt_if", "cancel_if", "early_chain_if",
}
_VARIABLE_ACTIONS = {"variable", "cycling_variable"}
_VARIABLE_EXPRESSION_OPTIONS = {"value", "value_else", "condition"}


def _range(line: int, start_offset: int, end_offset: int) -> SourceRange:
    return SourceRange(
        SourcePosition(line, start_offset + 1),
        SourcePosition(line, end_offset + 1),
    )


def _inline_comment_start(text: str) -> Optional[int]:
    """Return the first unquoted '#'; quoted hashes are ordinary value text."""
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
        elif char in "\"'":
            quote = char
        elif char == "#":
            return index
    return None


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


def _split_actions(text: str) -> Iterator[Tuple[int, int]]:
    """Retain a narrow, structurally unambiguous one-token division typo."""
    slices = list(_split_top_level(text, "/"))
    merged = []
    index = 0
    while index < len(slices):
        start, end = slices[index]
        if index + 1 < len(slices):
            next_start, next_end = slices[index + 1]
            current = text[start:end]
            following = text[next_start:next_end]
            in_expression = re.search(
                r",(?:if|target_if|interrupt_if|cancel_if|early_chain_if)="
                r"[^,]*[A-Za-z0-9_.]$",
                current,
            )
            # Real action names in the upstream corpus are descriptive. A
            # single operand at end-of-line is safely retained for the lexer.
            if in_expression and re.fullmatch(r"[A-Za-z0-9]", following):
                slices[index + 1] = (start, next_end)
                index += 1
                continue
        merged.append((start, end))
        index += 1
    yield from merged


def _expression_details(action_name: str, option: Option):
    expression_text = None
    expression_range = option.value_range
    selector = None
    selector_range = None
    invalid_selector = None
    if option.name in _GENERAL_EXPRESSION_OPTIONS:
        expression_text = option.value
    elif action_name in _VARIABLE_ACTIONS and option.name in _VARIABLE_EXPRESSION_OPTIONS:
        expression_text = option.value
    elif action_name == "wait" and option.name == "sec":
        expression_text = option.value

    if option.name == "target_if" and expression_text is not None:
        match = re.match(r"([A-Za-z_][A-Za-z0-9_]*):(.*)\Z", expression_text)
        if match:
            raw_selector = match.group(1)
            selector_range = SourceRange(
                option.value_range.start,
                SourcePosition(option.value_range.start.line,
                               option.value_range.start.column + len(raw_selector)),
            )
            if raw_selector.lower() in {"first", "min", "max"}:
                selector = raw_selector.lower()
            else:
                invalid_selector = selector_range
            expression_text = match.group(2)
            expression_range = SourceRange(
                SourcePosition(option.value_range.start.line,
                               option.value_range.start.column + match.start(2)),
                option.value_range.end,
            )
    return (expression_text, expression_range, selector, selector_range,
            invalid_selector)


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
        option = _parse_option(text[start:end], line, absolute_start + start)
        if isinstance(option, Option):
            (expression_text, expression_range, selector, selector_range,
             _) = _expression_details(name, option)
            if expression_text is not None:
                expression, _ = parse_expression(expression_text, expression_range)
                option = replace(
                    option, expression=expression, expression_selector=selector,
                    selector_range=selector_range,
                )
        options.append(option)
    return ActionEntry(
        name=name,
        options=tuple(options),
        range=entry_range,
        name_range=_range(
            line, absolute_start + name_start, absolute_start + name_end),
    )


def _expression_issues(action):
    issues = []
    if not isinstance(action, ActionEntry):
        return issues
    for option in action.options:
        if not isinstance(option, Option):
            continue
        (expression_text, expression_range, _, _,
         invalid_selector) = _expression_details(action.name, option)
        if invalid_selector is not None:
            issues.append(ParseIssue(
                code="invalid-target-if-selector",
                message="target_if selector must be first, min, or max.",
                range=invalid_selector,
            ))
        if expression_text is not None:
            # Top-level structure diagnostics own malformed non-expression
            # brackets. Avoid noisy cascades while retaining the action AST.
            if any(char in expression_text for char in "[]{}"):
                continue
            _, local_issues = parse_expression(expression_text, expression_range)
            issues.extend(local_issues)
    return issues


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
    comment_start = _inline_comment_start(content)
    comment_issue = None
    if comment_start is not None:
        comment_issue = ParseIssue(
            code="inline-comment",
            message="Inline comments are not accepted by SimC; move this to its own line.",
            range=_range(
                token.range.start.line,
                indent_length + comment_start,
                len(token.text),
            ),
            severity="warning",
        )
        content = content[:comment_start].rstrip()
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
        for start, end in _split_actions(body)
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
    issues = [comment_issue] if comment_issue is not None else []
    issues.extend(_structure_issues(body, token.range.start.line, body_offset))
    for action in actions:
        if isinstance(action, InvalidActionEntry):
            issues.append(action.issue)
        else:
            issues.extend(
                option.issue for option in action.options
                if isinstance(option, InvalidOption)
            )
            issues.extend(_expression_issues(action))
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
