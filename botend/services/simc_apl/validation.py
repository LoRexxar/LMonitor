"""Public structural validation helpers for the editor API."""

from .parser import parse
from .semantic import analyze


def validate_document(source):
    document = parse(source)
    semantic = analyze(document)
    errors = tuple(issue for issue in (*document.issues, *semantic.diagnostics)
                   if issue.severity == 'error')
    return document, semantic, errors


def _position(value):
    return {"line": value.line, "column": value.column}


def diagnostic_dict(issue):
    return {
        "source": "structural", "severity": issue.severity, "code": issue.code,
        "message": issue.message,
        "range": {"start": _position(issue.range.start), "end": _position(issue.range.end)},
    }


def validate_payload(content):
    document, semantic, errors = validate_document(content)
    issues = tuple(document.issues) + tuple(semantic.diagnostics)
    return {
        "structural_valid": not errors, "authoritative_valid": None,
        "diagnostics": [diagnostic_dict(issue) for issue in issues],
        "range_contract": {"base": 1, "end": "exclusive", "unit": "unicode_code_point"},
    }
