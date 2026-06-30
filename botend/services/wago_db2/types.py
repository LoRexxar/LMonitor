from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DB2RecordRef:
    table: str
    record_id: int
    push_id: int = 0
    build: str = ""
    locale: str = ""
    row: dict[str, Any] | None = None
    source: str = ""


@dataclass
class DB2Object:
    kind: str
    object_id: int | str
    title: str = ""
    subtitle: str = ""
    category: str = ""
    source_records: list[DB2RecordRef] = field(default_factory=list)
    related_records: list[DB2RecordRef] = field(default_factory=list)
    summary_fields: list[dict[str, Any]] = field(default_factory=list)
    raw_fields: list[dict[str, Any]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass
class WagoDB2Graph:
    objects: list[DB2Object] = field(default_factory=list)
    unresolved_records: list[DB2RecordRef] = field(default_factory=list)
    table_stats: dict[str, int] = field(default_factory=dict)
