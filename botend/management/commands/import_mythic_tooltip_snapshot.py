"""导入 WoW PTR AddOn 采集的精确大秘境技能 Tooltip。"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone as datetime_timezone
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from botend.models import (
    MythicDungeonAbility,
    MythicDungeonDataVersion,
    MythicDungeonSpell,
)
from botend.mythic_planner.mdt_converter import LuaParseError, LuaValueParser
from botend.mythic_planner.spell_tooltips import (
    QUALITY_EXACT_RENDERED,
    QUALITY_MANUAL_OVERRIDE,
    SOURCE_WOW_CLIENT,
    build_description_metadata,
    build_manifest_core,
    description_quality,
    is_clean_rendered_description,
    manifest_hash,
    parse_full_build,
)


SNAPSHOT_VARIABLE = "LMonitorMythicTooltipExport"
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
CJK_RE = re.compile(r"[\u3400-\u9fff]")


class Command(BaseCommand):
    help = "导入 WoW PTR AddOn SavedVariables 中的精确大秘境技能说明"

    def add_arguments(self, parser):
        parser.add_argument(
            "--input",
            required=True,
            help="LMonitorMythicTooltipCollector.lua SavedVariables 文件路径",
        )
        parser.add_argument(
            "--version-key",
            default="",
            help="MDT 数据版本；默认使用当前生效版本",
        )
        parser.add_argument(
            "--expected-build",
            default="",
            help="期望完整 build；默认读取目标版本技能快照",
        )
        parser.add_argument("--locale", default="zhCN", help="期望客户端语言")
        parser.add_argument(
            "--difficulty-id",
            type=int,
            default=8,
            help="期望 Tooltip 难度 ID；史诗钥石为 8",
        )
        parser.add_argument(
            "--min-coverage",
            type=float,
            default=0.0,
            help="最低精确渲染覆盖率，范围 0～1",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="只解析、校验和统计，不写数据库",
        )

    def handle(self, *args, **options):
        input_path = Path(str(options.get("input") or "")).expanduser()
        snapshot, snapshot_bytes = self._read_snapshot(input_path)
        version = self._resolve_version(str(options.get("version_key") or "").strip())
        expected_build = self._resolve_expected_build(
            version,
            str(options.get("expected_build") or "").strip(),
        )
        locale = str(options.get("locale") or "").strip()
        difficulty_id = int(options.get("difficulty_id") or 0)
        minimum = float(options.get("min_coverage") or 0.0)
        if minimum < 0 or minimum > 1:
            raise CommandError("--min-coverage 必须在 0～1 之间")
        if difficulty_id <= 0:
            raise CommandError("--difficulty-id 必须是正整数")

        known_spell_ids = sorted({
            int(spell_id)
            for spell_id in MythicDungeonAbility.objects.filter(
                enemy__dungeon__data_version=version,
                is_active=True,
            ).values_list("spell_id", flat=True)
            if int(spell_id or 0) > 0
        })
        if not known_spell_ids:
            raise CommandError(f"数据版本 {version.key} 没有可导入的怪物技能")

        validated = self._validate_snapshot(
            snapshot,
            version=version,
            expected_build=expected_build,
            locale=locale,
            difficulty_id=difficulty_id,
            known_spell_ids=known_spell_ids,
            minimum=minimum,
        )
        snapshot_hash = self._snapshot_hash(validated)
        self.stdout.write(
            f"快照校验通过: version={version.key}, build={expected_build}, "
            f"locale={locale}, difficulty_id={difficulty_id}, "
            f"captured={validated['captured_count']}/{len(known_spell_ids)}, "
            f"missing={validated['missing_count']}, "
            f"coverage={validated['coverage']:.1%}, bytes={snapshot_bytes}, "
            f"snapshot_hash={snapshot_hash}"
        )
        if options.get("dry_run"):
            self.stdout.write(self.style.SUCCESS("dry-run 完成，未写数据库"))
            return

        result = self._write_snapshot(
            version=version,
            expected_build=expected_build,
            locale=locale,
            difficulty_id=difficulty_id,
            validated=validated,
            snapshot_hash=snapshot_hash,
        )
        self.stdout.write(self.style.SUCCESS(
            f"客户端 Tooltip 导入完成: updated={result['updated']}, "
            f"linked={result['linked']}, exact={result['exact']}, "
            f"mechanic_only={result['mechanic_only']}"
        ))

    @staticmethod
    def _read_snapshot(path):
        if not path.is_file():
            raise CommandError(f"找不到 SavedVariables 文件: {path}")
        size = path.stat().st_size
        if size <= 0:
            raise CommandError("SavedVariables 文件为空")
        if size > MAX_SNAPSHOT_BYTES:
            raise CommandError(
                f"SavedVariables 文件超过 {MAX_SNAPSHOT_BYTES} 字节限制"
            )
        text = path.read_text(encoding="utf-8-sig")
        match = re.search(
            rf"(?m)^\s*{re.escape(SNAPSHOT_VARIABLE)}\s*=\s*",
            text,
        )
        if not match:
            raise CommandError(
                f"SavedVariables 中找不到 {SNAPSHOT_VARIABLE} 赋值"
            )
        try:
            snapshot = LuaValueParser(text, match.end()).parse()
        except LuaParseError as error:
            raise CommandError(f"Tooltip 快照 Lua 格式不正确: {error}") from error
        if not isinstance(snapshot, dict):
            raise CommandError("Tooltip 快照不是有效的 Lua table")
        return snapshot, size

    @staticmethod
    def _resolve_version(version_key):
        queryset = MythicDungeonDataVersion.objects.all()
        version = queryset.filter(key=version_key).first() if version_key else None
        if version_key and not version:
            raise CommandError(f"找不到 MDT 数据版本: {version_key}")
        if not version:
            version = queryset.filter(is_active=True).order_by("-imported_at").first()
        if not version:
            raise CommandError("找不到目标 MDT 数据版本")
        return version

    @staticmethod
    def _resolve_expected_build(version, configured):
        if configured:
            candidate = configured
        else:
            metadata = version.metadata if isinstance(version.metadata, dict) else {}
            spell_snapshot = metadata.get("spell_snapshot")
            spell_snapshot = (
                spell_snapshot if isinstance(spell_snapshot, dict) else {}
            )
            candidate = (
                spell_snapshot.get("snapshot_build")
                or metadata.get("snapshot_build")
                or version.game_version
                or ""
            )
        candidate = str(candidate or "").strip()
        if parse_full_build(candidate) == ("", ""):
            raise CommandError(
                f"数据版本 {version.key} 没有完整客户端 build；"
                "请显式传入 --expected-build"
            )
        return candidate

    @classmethod
    def _validate_snapshot(
        cls,
        snapshot,
        *,
        version,
        expected_build,
        locale,
        difficulty_id,
        known_spell_ids,
        minimum,
    ):
        if int(snapshot.get("schema_version") or 0) != 1:
            raise CommandError("不支持的 Tooltip 快照 schema_version")
        if not int(snapshot.get("completed_at") or 0):
            raise CommandError("Tooltip 采集尚未完成；请完成采集并 /reload 后再导入")
        if str(snapshot.get("data_version_key") or "") != version.key:
            raise CommandError(
                "Tooltip 快照 data_version_key 与目标 MDT 数据版本不一致"
            )

        client_version = str(snapshot.get("client_version") or "").strip()
        client_build = str(snapshot.get("client_build") or "").strip()
        actual_build = (
            f"{client_version}.{client_build}"
            if client_version and client_build
            else ""
        )
        if actual_build != expected_build:
            raise CommandError(
                f"客户端 build 不匹配：快照 {actual_build or '未知'}，"
                f"目标 {expected_build}"
            )
        if str(snapshot.get("expected_full_build") or "") != expected_build:
            raise CommandError("快照中的 expected_full_build 与目标 build 不一致")
        if str(snapshot.get("client_locale") or "") != locale:
            raise CommandError(
                f"客户端语言不匹配：快照 {snapshot.get('client_locale') or '未知'}，"
                f"目标 {locale}"
            )
        if int(snapshot.get("difficulty_id") or 0) != difficulty_id:
            raise CommandError(
                f"Tooltip 难度不匹配：快照 {snapshot.get('difficulty_id') or 0}，"
                f"目标 {difficulty_id}"
            )

        expected_manifest = build_manifest_core(
            data_version_key=version.key,
            full_build=expected_build,
            locale=locale,
            difficulty_id=difficulty_id,
            spell_ids=known_spell_ids,
        )
        expected_manifest_hash = manifest_hash(expected_manifest)
        if str(snapshot.get("manifest_hash") or "") != expected_manifest_hash:
            raise CommandError(
                "采集清单哈希与当前数据库不一致；请重新导出清单并采集"
            )

        raw_spells = snapshot.get("spells")
        raw_missing = snapshot.get("missing")
        if not isinstance(raw_spells, dict) or not isinstance(raw_missing, dict):
            raise CommandError("Tooltip 快照缺少 spells 或 missing 表")
        spells = {}
        invalid_ids = []
        for raw_spell_id, raw_row in raw_spells.items():
            try:
                spell_id = int(raw_spell_id)
            except (TypeError, ValueError):
                invalid_ids.append(raw_spell_id)
                continue
            if not isinstance(raw_row, dict):
                invalid_ids.append(spell_id)
                continue
            description = str(raw_row.get("description") or "").strip()
            if not is_clean_rendered_description(description):
                invalid_ids.append(spell_id)
                continue
            if locale == "zhCN" and not CJK_RE.search(description):
                invalid_ids.append(spell_id)
                continue
            spells[spell_id] = {
                "name": str(raw_row.get("name") or "").strip(),
                "description": description,
                "capture_source": str(
                    raw_row.get("capture_source") or ""
                ).strip(),
                "line_type": int(raw_row.get("line_type") or 0),
            }
        if invalid_ids:
            preview = ", ".join(str(value) for value in invalid_ids[:10])
            raise CommandError(f"快照包含无效技能说明: {preview}")

        missing_ids = set()
        for raw_spell_id in raw_missing:
            try:
                missing_ids.add(int(raw_spell_id))
            except (TypeError, ValueError) as error:
                raise CommandError(
                    f"missing 表包含无效 spell ID: {raw_spell_id}"
                ) from error

        known = set(known_spell_ids)
        captured = set(spells)
        unknown = (captured | missing_ids) - known
        if unknown:
            preview = ", ".join(str(value) for value in sorted(unknown)[:10])
            raise CommandError(f"快照包含当前数据版本之外的 spell ID: {preview}")
        unaccounted = known - captured - missing_ids
        if unaccounted:
            preview = ", ".join(str(value) for value in sorted(unaccounted)[:10])
            raise CommandError(f"快照未记录以下 spell ID 的结果: {preview}")
        if captured & missing_ids:
            preview = ", ".join(str(value) for value in sorted(captured & missing_ids)[:10])
            raise CommandError(f"快照同时把技能标记为成功和缺失: {preview}")
        if int(snapshot.get("total") or 0) != len(known):
            raise CommandError("快照 total 与当前技能总数不一致")

        coverage = len(captured) / max(1, len(known))
        if coverage < minimum:
            raise CommandError(
                f"精确 Tooltip 覆盖率 {coverage:.1%} 低于最低要求 {minimum:.1%}"
            )
        return {
            "client_version": client_version,
            "client_build": client_build,
            "client_interface_version": int(
                snapshot.get("client_interface_version") or 0
            ),
            "client_locale": locale,
            "difficulty_id": difficulty_id,
            "manifest_hash": expected_manifest_hash,
            "collector_version": str(snapshot.get("collector_version") or ""),
            "captured_at": cls._epoch_iso(snapshot.get("completed_at")),
            "spells": spells,
            "missing_ids": sorted(missing_ids),
            "captured_count": len(captured),
            "missing_count": len(missing_ids),
            "coverage": coverage,
        }

    @staticmethod
    def _epoch_iso(value):
        try:
            return datetime.fromtimestamp(
                int(value),
                tz=datetime_timezone.utc,
            ).isoformat()
        except (TypeError, ValueError, OSError, OverflowError):
            return ""

    @staticmethod
    def _snapshot_hash(validated):
        payload = {
            "client_version": validated["client_version"],
            "client_build": validated["client_build"],
            "client_locale": validated["client_locale"],
            "difficulty_id": validated["difficulty_id"],
            "manifest_hash": validated["manifest_hash"],
            "spells": {
                str(spell_id): validated["spells"][spell_id]
                for spell_id in sorted(validated["spells"])
            },
            "missing_ids": validated["missing_ids"],
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @transaction.atomic
    def _write_snapshot(
        self,
        *,
        version,
        expected_build,
        locale,
        difficulty_id,
        validated,
        snapshot_hash,
    ):
        now = timezone.now()
        spell_ids = sorted(validated["spells"])
        existing = {
            int(record.spell_id): record
            for record in MythicDungeonSpell.objects.filter(
                data_version=version,
                spell_id__in=spell_ids,
            )
        }
        source_branch = ""
        version_metadata = version.metadata if isinstance(version.metadata, dict) else {}
        spell_snapshot = version_metadata.get("spell_snapshot")
        spell_snapshot = spell_snapshot if isinstance(spell_snapshot, dict) else {}
        source_branch = str(spell_snapshot.get("source_branch") or "").strip()

        missing_records = []
        for spell_id in spell_ids:
            if spell_id not in existing:
                missing_records.append(MythicDungeonSpell(
                    data_version=version,
                    spell_id=spell_id,
                    source_branch=source_branch,
                    source_locale=locale,
                    snapshot_build=expected_build,
                    is_active=True,
                ))
        if missing_records:
            MythicDungeonSpell.objects.bulk_create(missing_records, batch_size=500)
            existing = {
                int(record.spell_id): record
                for record in MythicDungeonSpell.objects.filter(
                    data_version=version,
                    spell_id__in=spell_ids,
                )
            }

        updated = []
        for spell_id in spell_ids:
            record = existing[spell_id]
            row = validated["spells"][spell_id]
            metadata = dict(record.metadata or {})
            if description_quality(metadata) == QUALITY_MANUAL_OVERRIDE:
                continue
            metadata.update(build_description_metadata(
                source=SOURCE_WOW_CLIENT,
                quality=QUALITY_EXACT_RENDERED,
                client_version=validated["client_version"],
                client_build=validated["client_build"],
                client_interface_version=validated["client_interface_version"],
                client_locale=locale,
                difficulty_id=difficulty_id,
                tooltip_line_type=row["line_type"],
                tooltip_capture_source=row["capture_source"],
                tooltip_captured_at=validated["captured_at"],
                tooltip_imported_at=now.isoformat(),
                tooltip_manifest_hash=validated["manifest_hash"],
                tooltip_snapshot_hash=snapshot_hash,
                tooltip_collector_schema_version=1,
            ))
            if row["name"]:
                record.name_zh = row["name"]
            record.description_zh = row["description"]
            record.source_branch = record.source_branch or source_branch
            record.source_locale = locale
            record.snapshot_build = expected_build
            record.metadata = metadata
            record.is_active = True
            record.updated_at = now
            updated.append(record)
        if updated:
            MythicDungeonSpell.objects.bulk_update(
                updated,
                [
                    "name_zh",
                    "description_zh",
                    "source_branch",
                    "source_locale",
                    "snapshot_build",
                    "metadata",
                    "is_active",
                    "updated_at",
                ],
                batch_size=500,
            )

        linked = 0
        for spell_id, record in existing.items():
            linked += MythicDungeonAbility.objects.filter(
                enemy__dungeon__data_version=version,
                spell_id=spell_id,
            ).exclude(spell_record=record).update(spell_record=record)

        quality_counts = Counter()
        for metadata in MythicDungeonSpell.objects.filter(
            data_version=version,
            is_active=True,
        ).values_list("metadata", flat=True):
            quality_counts[description_quality(metadata)] += 1

        version_metadata = dict(version.metadata or {})
        spell_snapshot = dict(version_metadata.get("spell_snapshot") or {})
        spell_snapshot["client_tooltips"] = {
            "source": SOURCE_WOW_CLIENT,
            "quality": QUALITY_EXACT_RENDERED,
            "client_version": validated["client_version"],
            "client_build": validated["client_build"],
            "client_locale": locale,
            "difficulty_id": difficulty_id,
            "captured": validated["captured_count"],
            "missing": validated["missing_count"],
            "coverage": validated["coverage"],
            "manifest_hash": validated["manifest_hash"],
            "snapshot_hash": snapshot_hash,
            "captured_at": validated["captured_at"],
            "imported_at": now.isoformat(),
        }
        spell_snapshot["description_coverage"] = {
            key: value
            for key, value in sorted(quality_counts.items())
            if key
        }
        version_metadata["spell_snapshot"] = spell_snapshot
        version.metadata = version_metadata
        version.save(update_fields=["metadata", "updated_at"])
        return {
            "updated": len(updated),
            "linked": linked,
            "exact": quality_counts[QUALITY_EXACT_RENDERED],
            "mechanic_only": quality_counts["mechanic_only"],
        }
