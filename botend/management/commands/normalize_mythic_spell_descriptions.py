"""基于现有 DB2 原始文本重建大秘境技能机制说明。"""

from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from botend.models import MythicDungeonDataVersion, MythicDungeonSpell
from botend.mythic_planner.spell_tooltips import (
    QUALITY_EXACT_RENDERED,
    QUALITY_MANUAL_OVERRIDE,
    QUALITY_MECHANIC_ONLY,
    SOURCE_WAGO_DB2,
    build_description_metadata,
    description_quality,
    metadata_client_full_build,
    parse_full_build,
    should_preserve_description,
)
from botend.wow.spell_text import SpellTextResolver


class Command(BaseCommand):
    help = (
        "不重新下载 DB2，仅使用技能 metadata 中保留的原始文本，"
        "将旧的 x 占位说明重建为不含伪造数值的机制说明"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--version-key",
            default="",
            help="MDT 数据版本 key；留空时使用最近导入的启用版本",
        )
        parser.add_argument(
            "--expected-build",
            default="",
            help="要求数据版本属于该完整 build，例如 12.1.0.68914",
        )
        parser.add_argument(
            "--branch",
            default="",
            help="DB2 分支；留空时从版本元数据读取，仍为空则使用 wowt",
        )
        parser.add_argument("--locale", default="zhCN")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        version = self._resolve_version(options["version_key"])
        expected_build = str(options["expected_build"] or "").strip()
        snapshot = self._spell_snapshot(version)
        snapshot_build = str(snapshot.get("snapshot_build") or "").strip()
        if expected_build:
            if parse_full_build(expected_build) == ("", ""):
                raise CommandError(
                    "--expected-build 必须是完整 build，例如 12.1.0.68914"
                )
            if snapshot_build and snapshot_build != expected_build:
                raise CommandError(
                    f"数据版本 build 不匹配: 当前 {snapshot_build}，"
                    f"要求 {expected_build}"
                )
        target_build = expected_build or snapshot_build
        branch = (
            str(options["branch"] or "").strip()
            or str(snapshot.get("source_branch") or "").strip()
            or "wowt"
        )
        locale = str(options["locale"] or "").strip()
        if locale != "zhCN":
            raise CommandError("当前机制说明重建仅支持 --locale zhCN")

        records = list(
            MythicDungeonSpell.objects.filter(
                data_version=version,
                is_active=True,
            ).order_by("spell_id")
        )
        resolver = SpellTextResolver(branch=branch, locale=locale)
        now = timezone.now()
        updated = []
        counts = Counter()

        for record in records:
            metadata = dict(record.metadata or {})
            existing_quality = description_quality(metadata)
            if existing_quality == QUALITY_MANUAL_OVERRIDE:
                counts["preserved_manual"] += 1
                continue

            preserve = bool(
                record.description_zh
                and should_preserve_description(
                    metadata,
                    QUALITY_MECHANIC_ONLY,
                )
            )
            if (
                preserve
                and existing_quality == QUALITY_EXACT_RENDERED
                and target_build
                and metadata_client_full_build(metadata)
                and metadata_client_full_build(metadata) != target_build
            ):
                preserve = False
            if preserve:
                counts[f"preserved_{existing_quality or 'higher_quality'}"] += 1
                continue

            raw_description = str(
                metadata.get("raw_description_zh") or ""
            )
            raw_aura = str(
                metadata.get("raw_aura_description_zh") or ""
            )
            description = (
                resolver.resolve_mechanic(raw_description, record.spell_id)
                if raw_description
                else ""
            )
            aura_description = (
                resolver.resolve_mechanic(raw_aura, record.spell_id)
                if raw_aura
                else ""
            )
            if not description and not aura_description:
                counts["missing_raw_text"] += 1
                continue

            metadata.update(build_description_metadata(
                source=SOURCE_WAGO_DB2,
                quality=QUALITY_MECHANIC_ONLY,
                normalized_at=now.isoformat(),
                normalized_from_build=record.snapshot_build or target_build,
            ))
            changed = (
                record.description_zh != description
                or record.aura_description_zh != aura_description
                or record.metadata != metadata
            )
            record.description_zh = description
            record.aura_description_zh = aura_description
            record.metadata = metadata
            if changed:
                record.updated_at = now
                updated.append(record)
                counts["updated"] += 1
            else:
                counts["unchanged"] += 1

        projected_quality = Counter(
            description_quality(record.metadata)
            for record in records
            if record.description_zh or record.aura_description_zh
        )
        if not options["dry_run"]:
            with transaction.atomic():
                if updated:
                    MythicDungeonSpell.objects.bulk_update(
                        updated,
                        [
                            "description_zh",
                            "aura_description_zh",
                            "metadata",
                            "updated_at",
                        ],
                        batch_size=500,
                    )
                version_metadata = dict(version.metadata or {})
                spell_snapshot = dict(
                    version_metadata.get("spell_snapshot") or {}
                )
                spell_snapshot["description_coverage"] = {
                    key: value
                    for key, value in sorted(projected_quality.items())
                    if key
                }
                spell_snapshot["mechanic_descriptions_normalized_at"] = (
                    now.isoformat()
                )
                version_metadata["spell_snapshot"] = spell_snapshot
                version.metadata = version_metadata
                version.save(update_fields=["metadata", "updated_at"])

        mode = "dry-run" if options["dry_run"] else "已写入"
        self.stdout.write(
            self.style.SUCCESS(
                f"{mode}: version={version.key}, build={target_build or '-'}, "
                f"updated={counts['updated']}, "
                f"missing_raw={counts['missing_raw_text']}, "
                f"preserved_exact={counts['preserved_exact_rendered']}, "
                f"preserved_external={counts['preserved_rendered_external']}, "
                f"preserved_manual={counts['preserved_manual']}"
            )
        )

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
    def _spell_snapshot(version):
        metadata = version.metadata if isinstance(version.metadata, dict) else {}
        snapshot = metadata.get("spell_snapshot")
        return snapshot if isinstance(snapshot, dict) else {}
