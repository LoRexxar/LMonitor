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


def validate_payload(content, mode='structural', *, authoritative_validator=None,
                     validation_context=None):
    document, semantic, errors = validate_document(content)
    issues = tuple(document.issues) + tuple(semantic.diagnostics)
    result = {
        "structural_valid": not errors, "authoritative_valid": None,
        "diagnostics": [diagnostic_dict(issue) for issue in issues],
        "range_contract": {"base": 1, "end": "exclusive", "unit": "unicode_code_point"},
    }
    if mode in ('authoritative', 'both'):
        if authoritative_validator is None:
            result['authoritative_status'] = 'structural_only'
            result['authoritative_error'] = {
                'code': 'validation_context_unavailable',
                'message': 'Authoritative validation requires one unambiguous Profile.',
            }
        elif errors:
            result['authoritative_status'] = 'skipped_structural_errors'
        else:
            authoritative = authoritative_validator.validate(
                content, validation_context=validation_context or {})
            result.update({key: value for key, value in authoritative.items()
                           if key != 'diagnostics'})
            result['diagnostics'].extend(authoritative.get('diagnostics', ()))
    return result
