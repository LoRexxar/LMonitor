"""Conservative document-local completion service.

This module deliberately has no catalog/database dependency. Runtime symbols are
served by the symbols endpoint; completions here are facts recoverable from the
current document's AST and semantic symbol table.
"""

from .parser import parse
from .semantic import analyze


def _prefix(content, line, column):
    lines = str(content).splitlines() or ['']
    index = max(0, min(len(lines) - 1, int(line or 1) - 1))
    return lines[index][:max(0, int(column or 1) - 1)]


def _item(name, kind):
    return {"label": name, "insert_text": name, "kind": kind}


def complete_document(content, line, column, *unused, **kwargs):
    """Return only document-local symbols and grammar snippets.

    Positional context arguments are accepted for compatibility with the initial
    API implementation, but intentionally ignored.
    """
    prefix = _prefix(content, line, column)
    document = parse(content)
    semantic = analyze(document)
    symbols = semantic.symbols
    items = []

    # At the beginning of an APL line, suggest the only assignment form the
    # parser recognizes. Do not attempt to invent runtime action names.
    stripped = prefix.lstrip()
    if not stripped or (stripped and '=' not in stripped and not stripped.startswith('actions')):
        items.append(_item('actions=', 'keyword'))

    # Complete action-list names only after a document-local call/run name=.
    if 'call_action_list,name=' in prefix or 'run_action_list,name=' in prefix:
        needle = prefix.rsplit('name=', 1)[-1].strip()
        names = {name for name in symbols.action_lists if name is not None}
        items.extend(_item(name, 'action_list') for name in sorted(names)
                     if name.startswith(needle))

    # Complete variable.foo references from semantic definitions, preserving the
    # conservative document-only contract.
    if 'variable.' in prefix:
        needle = prefix.rsplit('variable.', 1)[-1].split()[0]
        items.extend(_item(definition.name, 'variable')
                     for name, definitions in sorted(symbols.variables.items())
                     if name.startswith(needle)
                     for definition in definitions[:1])

    unique = []
    seen = set()
    for item in items:
        key = (item['kind'], item['insert_text'])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique[:100]
