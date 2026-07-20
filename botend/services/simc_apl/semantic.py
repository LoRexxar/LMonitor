"""Document-wide symbols and conservative semantic checks for SimC APL.

This layer only reports facts provable from one parsed document.  Runtime action,
option, spell, talent, and buff validity belongs to SimulationCraft/catalog checks.
"""

from dataclasses import dataclass, field
import re
from typing import Dict, Iterator, List, Optional, Tuple

from .ast import (
    ActionAssignment,
    ActionEntry,
    BinaryExpression,
    CallExpression,
    Document,
    Expression,
    IdentifierExpression,
    Option,
    ParseIssue,
    SourcePosition,
    SourceRange,
    UnaryExpression,
)
from .names import is_valid_action_list_name

_VARIABLE_NAME_RE = re.compile(r"[A-Za-z0-9_]+\Z")
_LIST_ACTIONS = {"call_action_list", "run_action_list"}
_VARIABLE_ACTIONS = {"variable", "cycling_variable"}


@dataclass(frozen=True)
class ActionListDefinition:
    name: str
    operator: str
    range: SourceRange
    assignment: ActionAssignment


@dataclass(frozen=True)
class VariableDefinition:
    name: str
    range: SourceRange
    action: ActionEntry


@dataclass(frozen=True)
class ReferenceSite:
    name: str
    range: SourceRange
    source_list: Optional[str]
    kind: str


@dataclass
class DocumentSymbols:
    """Symbols and reference sites retained in document order."""

    # None is the main unnamed list; display it as "default" without
    # colliding with the explicitly named, upstream-ignored actions.default.
    action_lists: Dict[Optional[str], List[ActionListDefinition]] = field(default_factory=dict)
    variables: Dict[str, List[VariableDefinition]] = field(default_factory=dict)
    list_references: List[ReferenceSite] = field(default_factory=list)
    variable_references: List[ReferenceSite] = field(default_factory=list)
    actions: List[Tuple[str, SourceRange, Optional[str]]] = field(default_factory=list)
    expression_identifiers: List[Tuple[str, SourceRange, Optional[str]]] = field(default_factory=list)


@dataclass(frozen=True)
class SemanticResult:
    symbols: DocumentSymbols
    diagnostics: Tuple[ParseIssue, ...]


def _options(action: ActionEntry) -> Dict[str, Option]:
    # SimC action options are effectively consumed by name.  Keeping the last
    # parsed occurrence is sufficient for these narrow document checks.
    return {
        option.name: option for option in action.options
        if isinstance(option, Option)
    }


def _identifiers(expression: Optional[Expression]) -> Iterator[IdentifierExpression]:
    if expression is None:
        return
    if isinstance(expression, IdentifierExpression):
        yield expression
    elif isinstance(expression, UnaryExpression):
        yield from _identifiers(expression.operand)
    elif isinstance(expression, BinaryExpression):
        yield from _identifiers(expression.left)
        yield from _identifiers(expression.right)
    elif isinstance(expression, CallExpression):
        yield from _identifiers(expression.argument)


def _variable_name_range(identifier: IdentifierExpression) -> SourceRange:
    """Select ``foo`` rather than all of ``variable.foo`` for editor fixes."""
    prefix_length = len("variable.")
    return SourceRange(
        SourcePosition(identifier.range.start.line,
                       identifier.range.start.column + prefix_length),
        identifier.range.end,
    )


def _issue(code: str, message: str, source_range: SourceRange,
           severity: str = "warning") -> ParseIssue:
    return ParseIssue(code=code, message=message, range=source_range,
                      severity=severity)


def _trimmed_option_value(option: Option) -> Tuple[str, SourceRange]:
    value = option.value.strip()
    leading = len(option.value) - len(option.value.lstrip())
    return value, SourceRange(
        SourcePosition(option.value_range.start.line,
                       option.value_range.start.column + leading),
        SourcePosition(option.value_range.start.line,
                       option.value_range.start.column + leading + len(value)),
    )


def _collect(document: Document) -> Tuple[DocumentSymbols, List[ParseIssue]]:
    symbols = DocumentSymbols()
    diagnostics: List[ParseIssue] = []
    effective: Dict[Optional[str], List[ActionAssignment]] = {}

    # Retain assignment definitions/history for document symbols and reset
    # warnings, while separately constructing the sequence SimC actually uses.
    for line in document.lines:
        if not isinstance(line, ActionAssignment):
            continue
        list_name = line.list_name
        definition_range = line.list_name_range or SourceRange(
            line.range.start,
            SourcePosition(line.range.start.line,
                           line.range.start.column + len("actions")),
        )
        if list_name == "default":
            diagnostics.append(_issue(
                "ignored-default-action-list",
                "The explicitly named action list 'default' is ignored by SimC.",
                definition_range,
            ))
            continue

        prior = symbols.action_lists.setdefault(list_name, [])
        display_name = list_name if list_name is not None else "default"
        if prior and line.operator == "=":
            diagnostics.append(_issue(
                "action-list-reset",
                f"This '=' resets action list '{display_name}' and overwrites its earlier entries.",
                definition_range,
            ))
        prior.append(ActionListDefinition(
            display_name, line.operator, definition_range, line,
        ))
        if line.operator == "=":
            effective[list_name] = [line]
        else:
            effective.setdefault(list_name, []).append(line)

    effective_lines = {
        id(line) for assignments in effective.values() for line in assignments
    }
    # Revisit the source so effective actions and references remain in document
    # order even when assignments for several lists are interleaved.
    for line in document.lines:
        if not isinstance(line, ActionAssignment) or id(line) not in effective_lines:
            continue
        list_name = line.list_name
        for action in line.actions:
            if not isinstance(action, ActionEntry):
                continue
            symbols.actions.append((action.name, action.name_range, list_name))
            options = _options(action)

            if action.name in _LIST_ACTIONS:
                name = options.get("name")
                if name is None:
                    diagnostics.append(_issue(
                        "missing-action-list-name",
                        f"{action.name} requires a name option.",
                        action.name_range,
                        "error",
                    ))
                else:
                    reference_name, reference_range = _trimmed_option_value(name)
                    if not reference_name:
                        diagnostics.append(_issue(
                            "empty-action-list-name",
                            f"{action.name} requires a non-empty list name.",
                            reference_range,
                            "error",
                        ))
                    elif not is_valid_action_list_name(reference_name):
                        diagnostics.append(_issue(
                            "invalid-action-list-name",
                            "Action-list names may contain only ASCII letters, digits, "
                            "underscores, and hyphens.",
                            reference_range,
                            "error",
                        ))
                    else:
                        symbols.list_references.append(ReferenceSite(
                            reference_name, reference_range, list_name, action.name,
                        ))

            if action.name in _VARIABLE_ACTIONS:
                name = options.get("name")
                if name is None or not name.value:
                    diagnostics.append(_issue(
                        "missing-variable-name",
                        f"{action.name} requires a non-empty name option.",
                        name.value_range if name is not None else action.name_range,
                        "error",
                    ))
                elif _VARIABLE_NAME_RE.fullmatch(name.value):
                    canonical_name = name.value.casefold()
                    symbols.variables.setdefault(canonical_name, []).append(
                        VariableDefinition(name.value, name.value_range, action))
                op = options.get("op")
                if op is not None and op.value == "setif":
                    missing = [key for key in ("condition", "value", "value_else")
                               if key not in options or not options[key].value]
                    if missing:
                        diagnostics.append(_issue(
                            "incomplete-variable-setif",
                            "variable op=setif requires condition, value, and value_else; "
                            f"missing {', '.join(missing)}.",
                            op.value_range,
                            "error",
                        ))

            for option in action.options:
                if not isinstance(option, Option):
                    continue
                for identifier in _identifiers(option.expression):
                    symbols.expression_identifiers.append(
                        (identifier.name, identifier.range, list_name))
                    folded = identifier.name.casefold()
                    if folded.startswith("variable."):
                        variable_name = identifier.name[len("variable."):]
                        if variable_name and "." not in variable_name:
                            symbols.variable_references.append(ReferenceSite(
                                variable_name, _variable_name_range(identifier),
                                list_name, "variable",
                            ))
    return symbols, diagnostics


def _cycle_references(symbols: DocumentSymbols) -> set:
    """Return indexes of references whose edge lies in a call-graph cycle."""
    graph = {name: [] for name in symbols.action_lists}
    for index, reference in enumerate(symbols.list_references):
        if reference.name in graph:
            graph.setdefault(reference.source_list, []).append((reference.name, index))

    cyclic = set()
    # For each edge u->v, it is cyclic exactly when v can reach u.  Documents
    # are small, and this direct formulation also naturally marks every edge in
    # an SCC while excluding edges merely leading into one.
    for source, edges in graph.items():
        for target, index in edges:
            stack = [target]
            seen = set()
            while stack:
                node = stack.pop()
                if node == source:
                    cyclic.add(index)
                    break
                if node in seen:
                    continue
                seen.add(node)
                stack.extend(next_node for next_node, _ in graph.get(node, ()))
    return cyclic


def analyze(document: Document) -> SemanticResult:
    """Build document symbols and return deterministic semantic diagnostics."""
    symbols, diagnostics = _collect(document)

    for reference in symbols.list_references:
        if reference.name not in symbols.action_lists:
            diagnostics.append(_issue(
                "undefined-action-list",
                f"Action list '{reference.name}' is not defined in this document.",
                reference.range,
                "error",
            ))

    referenced_lists = {reference.name for reference in symbols.list_references}
    # The unnamed main list and precombat are engine entry points rather than
    # explicit call targets. Other named lists with no incoming reference are informational.
    for name, definitions in symbols.action_lists.items():
        if name not in {None, "precombat"} and name not in referenced_lists:
            diagnostics.append(_issue(
                "unreferenced-action-list",
                f"Action list '{name}' is defined but never referenced.",
                definitions[0].range,
                "info",
            ))

    known_variables = set(symbols.variables)
    for reference in symbols.variable_references:
        if reference.name.casefold() not in known_variables:
            diagnostics.append(_issue(
                "undefined-variable",
                f"Variable '{reference.name}' is never defined in this document.",
                reference.range,
                "warning",
            ))

    for index in sorted(_cycle_references(symbols)):
        reference = symbols.list_references[index]
        diagnostics.append(_issue(
            "recursive-action-list",
            f"{reference.kind} creates a recursive action-list cycle through "
            f"'{reference.name}'.",
            reference.range,
            "warning",
        ))

    diagnostics.sort(key=lambda item: (
        item.range.start.line, item.range.start.column,
        item.range.end.line, item.range.end.column, item.code,
    ))
    return SemanticResult(symbols, tuple(diagnostics))
