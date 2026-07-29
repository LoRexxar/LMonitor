"""导出供 WoW PTR AddOn 使用的大秘境技能采集清单。"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from botend.models import MythicDungeonAbility, MythicDungeonDataVersion
from botend.mythic_planner.spell_tooltips import (
    build_manifest_core,
    manifest_hash,
    parse_full_build,
)


DEFAULT_OUTPUT = (
    Path("tools")
    / "LMonitorMythicTooltipCollector"
    / "LMonitorMythicTooltipManifest.lua"
)


class Command(BaseCommand):
    help = "导出精确 PTR 客户端 Tooltip 采集所需的 spell ID 清单"

    def add_arguments(self, parser):
        parser.add_argument(
            "--version-key",
            default="",
            help="MDT 数据版本；默认使用当前生效版本",
        )
        parser.add_argument(
            "--build",
            default="",
            help="目标完整 build，例如 12.1.0.68914；默认读取版本技能快照",
        )
        parser.add_argument("--locale", default="zhCN", help="目标客户端语言")
        parser.add_argument(
            "--difficulty-id",
            type=int,
            default=8,
            help="Tooltip 难度 ID；史诗钥石为 8",
        )
        parser.add_argument(
            "--output",
            default=str(DEFAULT_OUTPUT),
            help="输出的 AddOn Lua 清单路径",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="只校验并打印统计，不写文件",
        )

    def handle(self, *args, **options):
        version = self._resolve_version(str(options.get("version_key") or "").strip())
        full_build = self._resolve_build(
            version,
            str(options.get("build") or "").strip(),
        )
        client_version, client_build = parse_full_build(full_build)
        if not client_version or not client_build:
            raise CommandError(f"build 格式不正确: {full_build}")

        locale = str(options.get("locale") or "").strip()
        if not re.fullmatch(r"[a-z]{2}[A-Z]{2}", locale):
            raise CommandError(f"locale 格式不正确: {locale}")
        difficulty_id = int(options.get("difficulty_id") or 0)
        if difficulty_id <= 0:
            raise CommandError("--difficulty-id 必须是正整数")

        spell_ids = sorted({
            int(spell_id)
            for spell_id in MythicDungeonAbility.objects.filter(
                enemy__dungeon__data_version=version,
                is_active=True,
            ).values_list("spell_id", flat=True)
            if int(spell_id or 0) > 0
        })
        if not spell_ids:
            raise CommandError(f"数据版本 {version.key} 没有可采集的怪物技能")

        manifest_core = build_manifest_core(
            data_version_key=version.key,
            full_build=full_build,
            locale=locale,
            difficulty_id=difficulty_id,
            spell_ids=spell_ids,
        )
        digest = manifest_hash(manifest_core)
        generated_at = timezone.now().isoformat()
        content = self._render_lua(
            manifest_core,
            manifest_hash=digest,
            generated_at=generated_at,
        )

        output = Path(str(options.get("output") or DEFAULT_OUTPUT))
        if not output.is_absolute():
            output = Path(settings.BASE_DIR) / output
        self.stdout.write(
            f"数据版本: {version.key}, build={full_build}, locale={locale}, "
            f"difficulty_id={difficulty_id}, spells={len(spell_ids)}, "
            f"manifest_hash={digest}"
        )
        if options.get("dry_run"):
            self.stdout.write(self.style.SUCCESS("dry-run 完成，未写采集清单"))
            return

        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8", newline="\n")
        self.stdout.write(self.style.SUCCESS(f"采集清单已写入: {output}"))

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
    def _resolve_build(version, configured):
        if configured:
            return configured
        metadata = version.metadata if isinstance(version.metadata, dict) else {}
        snapshot = metadata.get("spell_snapshot")
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        for candidate in (
            snapshot.get("snapshot_build"),
            metadata.get("snapshot_build"),
            version.game_version,
        ):
            candidate = str(candidate or "").strip()
            if parse_full_build(candidate) != ("", ""):
                return candidate
        raise CommandError(
            f"数据版本 {version.key} 没有完整客户端 build；请显式传入 --build"
        )

    @staticmethod
    def _render_lua(manifest, *, manifest_hash, generated_at):
        lines = [
            "-- 此文件由 export_mythic_tooltip_manifest 自动生成，请勿手工修改。",
            "LMonitorMythicTooltipManifest = {",
            f"    schema_version = {int(manifest['schema_version'])},",
            f"    data_version_key = {_lua_quote(manifest['data_version_key'])},",
            f"    expected_full_build = {_lua_quote(manifest['expected_full_build'])},",
            f"    expected_client_version = {_lua_quote(manifest['expected_client_version'])},",
            f"    expected_client_build = {_lua_quote(manifest['expected_client_build'])},",
            f"    locale = {_lua_quote(manifest['locale'])},",
            f"    difficulty_id = {int(manifest['difficulty_id'])},",
            f"    manifest_hash = {_lua_quote(manifest_hash)},",
            f"    generated_at = {_lua_quote(generated_at)},",
            "    spell_ids = {",
        ]
        lines.extend(
            f"        {int(spell_id)},"
            for spell_id in manifest["spell_ids"]
        )
        lines.extend([
            "    },",
            "}",
            "",
        ])
        return "\n".join(lines)


def _lua_quote(value):
    text = str(value or "")
    text = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return f'"{text}"'
