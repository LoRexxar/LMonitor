"""Shared publication validation for parsed SimC APL documents."""

from .parser import parse
from .semantic import analyze


def validate_document(source):
    """Parse once and return the document, semantics, and all error diagnostics."""
    document = parse(source)
    semantic = analyze(document)
    errors = tuple(issue for issue in (*document.issues, *semantic.diagnostics)
                   if issue.severity == 'error')
    return document, semantic, errors
