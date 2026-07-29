"""大秘境技能说明的来源等级、校验与元数据工具。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


QUALITY_MECHANIC_ONLY = "mechanic_only"
QUALITY_RENDERED_EXTERNAL = "rendered_external"
QUALITY_EXACT_RENDERED = "exact_rendered"
QUALITY_MANUAL_OVERRIDE = "manual_override"

SOURCE_WAGO_DB2 = "wago_db2_template"
SOURCE_WOWHEAD_TOOLTIP = "wowhead_tooltip"
SOURCE_WOWHEAD_TOOLTIP_REFERENCE = "wowhead_tooltip_reference"
SOURCE_WOW_CLIENT = "wow_client_tooltip"
SOURCE_MANUAL = "manual"

QUALITY_RANK = {
    "": 0,
    QUALITY_MECHANIC_ONLY: 20,
    QUALITY_RENDERED_EXTERNAL: 70,
    QUALITY_EXACT_RENDERED: 100,
    QUALITY_MANUAL_OVERRIDE: 200,
}

DESCRIPTION_PROVENANCE_KEYS = {
    "description_source",
    "description_quality",
    "client_version",
    "client_build",
    "client_interface_version",
    "client_locale",
    "difficulty_id",
    "tooltip_line_type",
    "tooltip_capture_source",
    "tooltip_captured_at",
    "tooltip_imported_at",
    "tooltip_manifest_hash",
    "tooltip_snapshot_hash",
    "tooltip_collector_schema_version",
    "wowhead_tooltip_url",
    "wowhead_tooltip_source",
    "wowhead_reference_spell_ids",
    "wowhead_reference_sources",
    "wowhead_locale",
    "wowhead_data_env",
    "wowhead_environment",
    "wowhead_difficulty_id",
    "wowhead_version_scope",
    "wowhead_build_exact",
}

_ISOLATED_X_RE = re.compile(r"(?<![A-Za-z])x(?![A-Za-z])", re.IGNORECASE)
_CLIENT_BUILD_RE = re.compile(
    r"^(?P<version>\d+\.\d+\.\d+)\.(?P<number>\d+)$"
)


def description_quality(metadata: dict[str, Any] | None) -> str:
    metadata = metadata if isinstance(metadata, dict) else {}
    explicit = str(metadata.get("description_quality") or "").strip()
    if explicit in QUALITY_RANK:
        return explicit
    source = str(metadata.get("description_source") or "").strip()
    if source == SOURCE_WOW_CLIENT:
        return QUALITY_EXACT_RENDERED
    if source == SOURCE_MANUAL:
        return QUALITY_MANUAL_OVERRIDE
    if (
        source in (SOURCE_WOWHEAD_TOOLTIP, SOURCE_WOWHEAD_TOOLTIP_REFERENCE)
        or metadata.get("wowhead_tooltip_source")
        or metadata.get("wowhead_reference_sources")
    ):
        return QUALITY_RENDERED_EXTERNAL
    return ""


def description_rank(metadata_or_quality: dict[str, Any] | str | None) -> int:
    if isinstance(metadata_or_quality, dict):
        quality = description_quality(metadata_or_quality)
    else:
        quality = str(metadata_or_quality or "").strip()
    return QUALITY_RANK.get(quality, 0)


def should_preserve_description(
    existing_metadata: dict[str, Any] | None,
    incoming_quality: str,
) -> bool:
    return description_rank(existing_metadata) > description_rank(incoming_quality)


def preserve_description_provenance(
    target: dict[str, Any],
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    existing = existing if isinstance(existing, dict) else {}
    for key in DESCRIPTION_PROVENANCE_KEYS:
        if key in existing:
            target[key] = existing[key]
    return target


def build_description_metadata(
    *,
    source: str,
    quality: str,
    **values: Any,
) -> dict[str, Any]:
    result = {
        "description_source": source,
        "description_quality": quality,
    }
    for key, value in values.items():
        if value not in (None, ""):
            result[key] = value
    return result


def parse_full_build(value: Any) -> tuple[str, str]:
    match = _CLIENT_BUILD_RE.fullmatch(str(value or "").strip())
    if not match:
        return "", ""
    return match.group("version"), match.group("number")


def metadata_client_full_build(metadata: dict[str, Any] | None) -> str:
    metadata = metadata if isinstance(metadata, dict) else {}
    version = str(metadata.get("client_version") or "").strip()
    number = str(metadata.get("client_build") or "").strip()
    return f"{version}.{number}" if version and number else ""


def is_clean_rendered_description(value: Any) -> bool:
    text = str(value or "").strip()
    if not text or "$" in text or _ISOLATED_X_RE.search(text):
        return False
    lowered = text.lower()
    return not any(
        marker in lowered
        for marker in (
            "一段时间秒",
            "一定码",
            "spelldesc",
            "spelltooltip",
        )
    )


def spell_snapshot_provenance(metadata: dict[str, Any] | None) -> dict[str, Any]:
    metadata = metadata if isinstance(metadata, dict) else {}
    result = {}
    for key in (
        "description_source",
        "description_quality",
        "client_version",
        "client_build",
        "client_locale",
        "difficulty_id",
        "tooltip_captured_at",
    ):
        value = metadata.get(key)
        if value not in (None, ""):
            result[key] = value
    return result


def build_manifest_core(
    *,
    data_version_key: str,
    full_build: str,
    locale: str,
    difficulty_id: int,
    spell_ids,
) -> dict[str, Any]:
    client_version, client_build = parse_full_build(full_build)
    return {
        "schema_version": 1,
        "data_version_key": str(data_version_key or ""),
        "expected_full_build": str(full_build or ""),
        "expected_client_version": client_version,
        "expected_client_build": client_build,
        "locale": str(locale or ""),
        "difficulty_id": int(difficulty_id or 0),
        "spell_ids": sorted({
            int(spell_id)
            for spell_id in spell_ids
            if int(spell_id or 0) > 0
        }),
    }


def manifest_hash(manifest_core: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            manifest_core,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
