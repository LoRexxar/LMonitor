"""SimulationCraft expression lexer and Pratt parser."""

from dataclasses import dataclass
import re
from typing import List, Optional, Tuple

from .ast import (
    BinaryExpression, CallExpression, IdentifierExpression, NumberExpression,
    ParseIssue, SourcePosition, SourceRange, UnaryExpression,
)


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    start: int
    end: int


_OPERATORS = (
    "~!=", "~<=", "~>=", "==", "&&", "||", "%%", "^^", "!=", "!~", "<=",
    ">=", "<?", ">?", "~=", "~<", "~>", "+", "-", "*", "%", "@",
    "!", "=", "<", ">", "~", "&", "^", "|", "(", ")",
)
_PRECEDENCE = {
    "|": 1, "^": 2, "&": 3,
    "=": 4, "!=": 4, "<": 4, "<=": 4, ">": 4, ">=": 4,
    "~": 4, "!~": 4, "~=": 4, "~!=": 4, "~<": 4, "~<=": 4,
    "~>": 4, "~>=": 4,
    "<?": 5, ">?": 5, "+": 6, "-": 6, "*": 7, "%": 7, "%%": 7,
}
_UNARY = {"!", "-", "+", "@"}
MAX_EXPRESSION_DEPTH = 128
IDENTIFIER_SEGMENT_RE = re.compile(r"[A-Za-z0-9_]+\Z")


def is_valid_identifier(name: str) -> bool:
    """Return whether every dotted SimC expression identifier segment is valid."""
    parts = name.split('.') if name else []
    return bool(parts) and bool(re.match(r"[A-Za-z]", parts[0])) and all(
        IDENTIFIER_SEGMENT_RE.fullmatch(part) for part in parts)


class _ExpressionTooDeep(Exception):
    """Internal control flow used after emitting a depth diagnostic."""


def tokenize(text: str) -> Tuple[List[Token], List[Tuple[str, int, int, Optional[str]]]]:
    tokens: List[Token] = []
    errors = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if char.isdigit():
            end = index + 1
            while end < len(text) and text[end].isdigit():
                end += 1
            if end < len(text) and text[end] == ".":
                end += 1
                while end < len(text) and text[end].isdigit():
                    end += 1
            tokens.append(Token("number", text[index:end], index, end))
            index = end
            continue
        if char.isalpha():
            end = index + 1
            while end < len(text) and (text[end].isalnum() or text[end] in "_."):
                end += 1
            tokens.append(Token("identifier", text[index:end], index, end))
            if not is_valid_identifier(text[index:end]):
                errors.append(("invalid-expression-identifier", index, end, None))
            index = end
            continue
        operator = next((op for op in _OPERATORS if text.startswith(op, index)), None)
        if operator is None:
            suggestion = "%" if char == "/" else None
            errors.append(("invalid-division-operator" if char == "/" else
                           "unknown-expression-token", index, index + 1, suggestion))
            index += 1
            continue
        end = index + len(operator)
        if operator == "^^":
            operator = "^"
        if operator in ("==", "&&", "||"):
            errors.append((
                "invalid-equality-operator" if operator == "==" else "invalid-logical-operator",
                index, end, {"==": "=", "&&": "&", "||": "|"}[operator],
            ))
            operator = {"==": "=", "&&": "&", "||": "|"}[operator]
        tokens.append(Token("operator", operator, index, end))
        index = end
    tokens.append(Token("eof", "", len(text), len(text)))
    return tokens, errors


class _Parser:
    def __init__(self, text: str, value_range: SourceRange):
        self.text = text
        self.line = value_range.start.line
        self.column = value_range.start.column
        self.tokens, lexical_errors = tokenize(text)
        self.index = 0
        self.issues = [self.issue(code, start, end, suggestion) for
                       code, start, end, suggestion in lexical_errors]

    def source_range(self, start: int, end: int) -> SourceRange:
        return SourceRange(
            SourcePosition(self.line, self.column + start),
            SourcePosition(self.line, self.column + end),
        )

    def issue(self, code: str, start: int, end: int,
              suggestion: Optional[str] = None) -> ParseIssue:
        messages = {
            "empty-expression": "Expected a SimC expression.",
            "unknown-expression-token": "Unknown token in SimC expression.",
            "invalid-expression-identifier": "Invalid dotted SimC expression identifier.",
            "invalid-division-operator": "SimC division uses '%'; '/' separates actions.",
            "invalid-equality-operator": "SimC equality uses '=' rather than '=='.",
            "invalid-logical-operator": "SimC logical operators are '&' and '|'.",
            "unclosed-parenthesis": "Opening parenthesis is not closed.",
            "mismatched-parenthesis": "Closing parenthesis has no matching opening parenthesis.",
            "missing-expression-operand": "Expected an expression operand.",
            "missing-expression-operator": "Expected an operator between expression operands.",
            "invalid-consecutive-unary": "This consecutive unary sequence cannot be constructed by SimC.",
            "expression-too-deep": "SimC expression nesting is too deep.",
        }
        return ParseIssue(code, messages[code], self.source_range(start, end), suggestion)

    @property
    def current(self) -> Token:
        return self.tokens[self.index]

    def advance(self) -> Token:
        token = self.current
        self.index += 1
        return token

    def parse(self):
        if self.current.kind == "eof":
            self.issues.append(self.issue("empty-expression", 0, 0))
            return None
        try:
            expression = self.parse_precedence(1, 0)
        except _ExpressionTooDeep:
            return None
        if self.current.text == ")":
            token = self.advance()
            self.issues.append(self.issue("mismatched-parenthesis", token.start, token.end))
        elif self.current.kind != "eof":
            token = self.current
            self.issues.append(self.issue("missing-expression-operator", token.start, token.end))
        return expression

    def parse_precedence(self, minimum: int, depth: int):
        left = self.parse_prefix(depth)
        while self.current.text in _PRECEDENCE and _PRECEDENCE[self.current.text] >= minimum:
            operator = self.advance()
            precedence = _PRECEDENCE[operator.text]
            if self.current.kind == "eof":
                self.issues.append(self.issue(
                    "missing-expression-operand", operator.start, operator.end,
                ))
                return left
            right = self.parse_precedence(precedence + 1, depth + 1)
            if right is None:
                return left
            if left is None:
                left = right
            else:
                left = BinaryExpression(operator.text, left, right,
                                        SourceRange(left.range.start, right.range.end))
        return left

    def parse_prefix(self, depth: int):
        token = self.current
        if depth >= MAX_EXPRESSION_DEPTH:
            self.issues.append(self.issue(
                "expression-too-deep", token.start, max(token.end, token.start + 1),
            ))
            raise _ExpressionTooDeep
        if token.text in _UNARY:
            self.advance()
            if self.current.text in _UNARY and not (
                self.current.text == "-" and
                self.index + 1 < len(self.tokens) and
                self.tokens[self.index + 1].kind == "number"
            ):
                self.issues.append(self.issue(
                    "invalid-consecutive-unary", self.current.start, self.current.end,
                ))
            operand = self.parse_prefix(depth + 1)
            if operand is None:
                return None
            return UnaryExpression(token.text, operand,
                                   SourceRange(self.source_range(token.start, token.end).start,
                                               operand.range.end))
        if token.kind == "number":
            self.advance()
            return NumberExpression(token.text, self.source_range(token.start, token.end))
        if token.kind == "identifier":
            self.advance()
            if self.current.text == "(" and token.text.lower() in ("floor", "ceil"):
                opening = self.advance()
                argument = self.parse_precedence(1, depth + 1)
                if self.current.text != ")":
                    self.issues.append(self.issue("unclosed-parenthesis", opening.start, opening.end))
                    return argument
                closing = self.advance()
                if argument is None:
                    return None
                return CallExpression(token.text.lower(), argument,
                                      self.source_range(token.start, closing.end))
            return IdentifierExpression(token.text, self.source_range(token.start, token.end))
        if token.text == "(":
            opening = self.advance()
            if self.current.text == ")":
                closing = self.advance()
                self.issues.append(self.issue("missing-expression-operand", closing.start, closing.end))
                return None
            value = self.parse_precedence(1, depth + 1)
            if self.current.text != ")":
                self.issues.append(self.issue("unclosed-parenthesis", opening.start, opening.end))
                return value
            closing = self.advance()
            if value is not None:
                object.__setattr__(value, "range", self.source_range(opening.start, closing.end))
            return value
        if token.kind == "eof":
            self.issues.append(self.issue("missing-expression-operand", token.start, token.end))
        elif token.text == ")":
            self.issues.append(self.issue("missing-expression-operand", token.start, token.end))
        else:
            self.advance()
            self.issues.append(self.issue("missing-expression-operand", token.start, token.end))
        return None


def parse_expression(text: str, value_range: SourceRange):
    parser = _Parser(text, value_range)
    try:
        expression = parser.parse()
    except RecursionError:
        # Last-resort protection if a future grammar path omits explicit depth
        # accounting. User-authored text must never crash document parsing.
        token = parser.current
        if not any(issue.code == "expression-too-deep" for issue in parser.issues):
            parser.issues.append(parser.issue(
                "expression-too-deep", token.start, max(token.end, token.start + 1),
            ))
        expression = None
    return expression, parser.issues
