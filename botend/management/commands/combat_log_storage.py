"""迁移私有原文块；事件索引始终在线。"""

import gzip
import hashlib
import json
import os
import uuid
import zipfile
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from botend.models import CombatLogReport
from botend.services.combat_log_archive import directory, read_block


def seal(report, shas, hot, cold):
    pack = cold / "archive.zip"
    manifest = {
        "version": 1,
        "sha256": report.digest,
        "size": report.size,
        "part_size": report.part_size,
        "parts": shas,
    }
    if not pack.exists():
        temporary = cold / (uuid.uuid4().hex + ".pending")
        with zipfile.ZipFile(temporary, "x", allowZip64=True) as z:
            for sha in dict.fromkeys(shas):
                z.writestr(sha + ".gz", gzip.compress(read_block(report, sha), compresslevel=3, mtime=0))
            z.writestr("manifest.json", json.dumps(manifest).encode())
        with temporary.open("rb+") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, pack)
    digest = hashlib.sha256()
    length = 0
    with zipfile.ZipFile(pack) as z:
        if json.loads(z.read("manifest.json")) != manifest:
            raise CommandError("冷归档清单与已发布报告不符。")
        for sha in shas:
            raw = gzip.decompress(z.read(sha + ".gz"))
            if hashlib.sha256(raw).hexdigest() != sha:
                raise CommandError("归档块复核失败，保留热副本。")
            length += len(raw)
            digest.update(raw)
    if digest.hexdigest() != report.digest or length != report.size:
        raise CommandError("重组文件校验失败，保留热副本。")
    for sha in dict.fromkeys(shas):
        (hot / (sha + ".gz")).unlink(missing_ok=True)


def transfer(pk, target):
    with transaction.atomic():
        report = CombatLogReport.objects.select_for_update().get(pk=pk, state="ready")
        hot, cold = directory(report), directory(report, "cold")
        shas = list(report.parts.order_by("number").values_list("digest", flat=True))
        if target == "cold":
            seal(report, shas, hot, cold)
        else:
            for sha in dict.fromkeys(shas):
                dest = hot / (sha + ".gz")
                if not dest.exists():
                    raw = read_block(report, sha)
                    temporary = hot / (uuid.uuid4().hex + ".pending")
                    with temporary.open("xb") as stream:
                        stream.write(gzip.compress(raw, compresslevel=3, mtime=0))
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, dest)
                if hashlib.sha256(gzip.decompress(dest.read_bytes())).hexdigest() != sha:
                    raise CommandError("恢复的热块校验失败。")
        report.storage_tier = target
        report.save(update_fields=["storage_tier"])


class Command(BaseCommand):
    help = "按报告或上传年龄迁移冷热归档；默认预览，--apply 执行。"

    def add_arguments(self, parser):
        parser.add_argument("--tier", choices=["hot", "cold"], required=True)
        selection = parser.add_mutually_exclusive_group(required=True)
        selection.add_argument("--report", type=uuid.UUID)
        selection.add_argument("--older-days", type=int)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        reports = CombatLogReport.objects.filter(state="ready")
        if options["report"]:
            reports = reports.filter(pk=options["report"])
        else:
            days = options["older_days"]
            if days < 0:
                raise CommandError("天数不能为负。")
            reports = reports.filter(created_at__lt=timezone.now() - timedelta(days=days)).exclude(
                storage_tier=options["tier"]
            )
        for pk in reports.order_by("created_at").values_list("pk", flat=True).iterator():
            if options["apply"]:
                transfer(pk, options["tier"])
            self.stdout.write(f"{pk} → {options['tier']} · " + ("已校验" if options["apply"] else "预览"))
