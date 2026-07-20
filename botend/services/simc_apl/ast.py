"""Stable, database-independent syntax tree for top-level SimC APL source.

All positions are 1-based Python code-point columns. ``SourceRange.end`` is exclusive.
"""

from dataclasses import dataclass, field
from typing import Optional, Tuple, Union


@dataclass(frozen=True, order=True)
class SourcePosition:
    line: int
    column: int


@dataclass(frozen=True)
class SourceRange:
    start: SourcePosition
    end: SourcePosition


@dataclass(frozen=True)
class ParseIssue:
    code: str
    message: str
    range: SourceRange
    suggestion: Optional[str] = None
    severity: str = "error"


@dataclass(frozen=True)
class NumberExpression:
    value: str
    range: SourceRange


@dataclass(frozen=True)
class IdentifierExpression:
    name: str
    range: SourceRange


@dataclass(frozen=True)
class UnaryExpression:
    operator: str
    operand: "Expression"
    range: SourceRange


@dataclass(frozen=True)
class BinaryExpression:
    operator: str
    left: "Expression"
    right: "Expression"
    range: SourceRange


@dataclass(frozen=True)
class CallExpression:
    function: str
    argument: "Expression"
    range: SourceRange


Expression = Union[
    NumberExpression, IdentifierExpression, UnaryExpression,
    BinaryExpression, CallExpression,
]


@dataclass(frozen=True)
class BlankLine:
    text: str
    range: SourceRange


@dataclass(frozen=True)
class CommentLine:
    text: str
    range: SourceRange


@dataclass(frozen=True)
class InvalidLine:
    text: str
    range: SourceRange
    issue: ParseIssue


@dataclass(frozen=True)
class Option:
    name: str
    value: str
    range: SourceRange
    name_range: SourceRange
    value_range: SourceRange
    expression: Optional[Expression] = None
    expression_selector: Optional[str] = None
    selector_range: Optional[SourceRange] = None


@dataclass(frozen=True)
class InvalidOption:
    raw: str
    range: SourceRange
    code: str
    message: str

    @property
    def issue(self) -> ParseIssue:
        return ParseIssue(self.code, self.message, self.range)


OptionNode = Union[Option, InvalidOption]


@dataclass(frozen=True)
class ActionEntry:
    name: str
    options: Tuple[OptionNode, ...]
    range: SourceRange
    name_range: SourceRange


@dataclass(frozen=True)
class InvalidActionEntry:
    raw: str
    range: SourceRange
    code: str
    message: str

    @property
    def issue(self) -> ParseIssue:
        return ParseIssue(self.code, self.message, self.range)


ActionNode = Union[ActionEntry, InvalidActionEntry]


@dataclass(frozen=True)
class ActionAssignment:
    text: str
    indent: str
    list_name: Optional[str]
    operator: str
    actions: Tuple[ActionNode, ...]
    range: SourceRange
    list_name_range: Optional[SourceRange] = None


DocumentLine = Union[BlankLine, CommentLine, ActionAssignment, InvalidLine]


@dataclass(frozen=True)
class Document:
    source: str
    lines: Tuple[DocumentLine, ...]
    range: SourceRange
    issues: Tuple[ParseIssue, ...] = field(default_factory=tuple)
