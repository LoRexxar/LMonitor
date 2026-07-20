"""Shared lexical rules for SimulationCraft action-list names."""

import re


ACTION_LIST_NAME_PATTERN = r"[A-Za-z0-9_][A-Za-z0-9_-]*"
_ACTION_LIST_NAME_RE = re.compile(ACTION_LIST_NAME_PATTERN + r"\Z")


def is_valid_action_list_name(name: str) -> bool:
    """Return whether *name* is a static ASCII action-list identifier."""
    return _ACTION_LIST_NAME_RE.fullmatch(name) is not None
