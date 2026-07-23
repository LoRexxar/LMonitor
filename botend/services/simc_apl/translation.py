"""Demand extraction and conservative, typed APL name translation.

This module deliberately uses the existing SimC parser/semantic model rather than
searching arbitrary text.  It only translates action names and the named object
inside buff/debuff/dot/cooldown/talent expressions.
"""

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Tuple


# SimC control actions are not game spells and must never enter Wago lookup.
CONTROL_ACTIONS = frozenset({
    "call_action_list", "cycling_variable", "pool_resource", "run_action_list",
    "snapshot_stats", "variable", "wait", "auto_attack", "potion", "use_item",
})

from .ast import ActionEntry, IdentifierExpression
from .parser import parse
from .semantic import analyze

_PREFIX_KINDS = {
    "buff": "buff",
    "debuff": "debuff",
    "dot": "dot",
    "cooldown": "cooldown",
    "talent": "talent",
}


@dataclass(frozen=True)
class TranslationDemand:
    kind: str
    token: str
    control: bool = False


@dataclass(frozen=True)
class _Span:
    start: int
    end: int
    kind: str
    token: str


def _line_offsets(source: str) -> List[int]:
    offsets = [0]
    for index, char in enumerate(source):
        if char == "\n":
            offsets.append(index + 1)
    return offsets


def _absolute(offsets: List[int], position) -> int:
    return offsets[position.line - 1] + position.column - 1


def _component_span(
    source: str, identifier: str, start: int, end: int, kind: str,
) -> Tuple[int, int, str]:
    prefix = kind + "."
    if not identifier.casefold().startswith(prefix) or len(identifier) <= len(prefix):
        return start, start, ""
    tail = identifier[len(prefix):]
    token = tail.split(".", 1)[0]
    identifier_start = source.find(identifier, start, end)
    if identifier_start < 0:
        return start, start, ""
    token_start = identifier_start + len(prefix)
    return token_start, token_start + len(token), token


def _parsed_spans(source: str) -> Tuple[List[_Span], bool]:
    document = parse(source)
    semantic = analyze(document)
    if document.issues or any(issue.severity == "error" for issue in semantic.diagnostics):
        return [], False
    offsets = _line_offsets(source)
    spans: List[_Span] = []
    for name, source_range, _list_name in semantic.symbols.actions:
        start = _absolute(offsets, source_range.start)
        end = _absolute(offsets, source_range.end)
        spans.append(_Span(start, end, "action", name))
    for identifier, source_range, _list_name in semantic.symbols.expression_identifiers:
        start = _absolute(offsets, source_range.start)
        end = _absolute(offsets, source_range.end)
        kind = _PREFIX_KINDS.get(identifier.split(".", 1)[0].casefold())
        if not kind:
            continue
        token_start, token_end, token = _component_span(
            source, identifier, start, end, kind,
        )
        if token:
            spans.append(_Span(token_start, token_end, kind, token))
    spans.sort(key=lambda item: (item.start, item.end, item.kind, item.token))
    return spans, True


def extract_translation_demands(source: str) -> Tuple[TranslationDemand, ...]:
    """Return unique typed names in source order; invalid APL returns no demands."""
    spans, valid = _parsed_spans(source)
    if not valid:
        return ()
    seen = set()
    result = []
    for span in spans:
        key = (span.kind, span.token.casefold())
        if key in seen:
            continue
        seen.add(key)
        result.append(TranslationDemand(
            span.kind, span.token,
            span.kind == "action" and span.token.casefold() in CONTROL_ACTIONS,
        ))
    return tuple(result)


def resolve_demand_mappings(
    demands: Iterable[TranslationDemand],
    facts: Iterable[Mapping],
    localized: Mapping[Tuple[str, int], str],
):
    """Resolve typed demands using authoritative IDs only; never match names."""
    by_key = {}
    for fact in facts:
        key = (str(fact.get("symbol_kind") or ""),
               str(fact.get("token") or "").casefold())
        by_key.setdefault(key, []).append(fact)
    mapping, failures = {}, []
    for demand in demands:
        key = (demand.kind, demand.token.casefold())
        if demand.control:
            failures.append((demand.kind, demand.token, "control_action"))
            continue
        candidates = by_key.get(key, [])
        if len(candidates) != 1:
            failures.append((demand.kind, demand.token,
                             "no_authoritative_identity" if not candidates
                             else "conflicting_authoritative_identity"))
            continue
        fact = candidates[0]
        # A symbol is only eligible for the requested typed kind.  In
        # particular, talent demands must never fall back to a spell_id.
        if demand.kind == 'talent' and fact.get('trait_id') is None:
            failures.append((demand.kind, demand.token, 'missing_authoritative_id'))
            continue
        if demand.kind != 'talent' and fact.get('trait_id') is not None:
            failures.append((demand.kind, demand.token, 'conflicting_authoritative_identity'))
            continue

        identity_type = "trait" if demand.kind == "talent" else "spell"
        identity_id = fact.get("trait_id") if identity_type == "trait" else fact.get("spell_id")
        if not isinstance(identity_id, int) or isinstance(identity_id, bool) or identity_id <= 0:
            failures.append((demand.kind, demand.token, "missing_authoritative_id"))
            continue
        chinese = localized.get((identity_type, identity_id))
        if not isinstance(chinese, str) or not chinese.strip():
            failures.append((demand.kind, demand.token, "missing_current_zh_snapshot"))
            continue
        mapping[(demand.kind, demand.token.casefold())] = chinese
    return mapping, tuple(failures)


def translate_apl_ranges(source: str, mapping: Dict[Tuple[str, str], str]) -> str:
    """Translate only parser-proven demand ranges, preserving all other bytes."""
    spans, valid = _parsed_spans(source)
    if not valid:
        return source
    replacements = []
    for span in spans:
        value = mapping.get((span.kind, span.token))
        if value is None:
            value = mapping.get((span.kind, span.token.casefold()))
        if value is not None:
            replacements.append((span.start, span.end, value))
    for start, end, value in sorted(replacements, reverse=True):
        source = source[:start] + value + source[end:]
    return source
