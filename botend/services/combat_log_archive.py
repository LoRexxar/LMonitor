"""本地私有压缩分片、后台索引发布和按字节偏移读取证据。"""

import csv
import gzip
import hashlib
import logging
import os
import shutil
import threading
import time
import uuid
import zipfile
from datetime import timedelta
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connections, transaction
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from botend.services import combat_log_localization as localization

from botend.models import CombatLogReport, CombatLogScheduler, CombatLogPolicy, CombatLogPurge
from botend.services.combat_log_index import PLAYER_EVENTS, build_index

logger = logging.getLogger(__name__)
PART_SIZE = 512 * 1024
LEASE = timedelta(minutes=15)


def directory(report, tier="hot", *, create=True):
    base = Path(settings.BASE_DIR)
    hot = Path(getattr(settings, "COMBAT_LOG_ROOT", base / "var" / "combat-logs")).resolve()
    cold = Path(getattr(settings, "COMBAT_LOG_COLD_ROOT", hot.parent / (hot.name + "-cold"))).resolve()
    if hot.is_relative_to(cold) or cold.is_relative_to(hot):
        raise ValueError("冷热存储必须使用独立目录。")
    root = hot if tier == "hot" else cold
    public = [
        base / "media",
        base / "static",
        getattr(settings, "STATIC_ROOT", None),
        getattr(settings, "MEDIA_ROOT", None),
        *settings.STATICFILES_DIRS,
    ]
    for item in public:
        item = item[-1] if isinstance(item, (tuple, list)) else item
        if item and root.is_relative_to(Path(item).resolve()):
            raise ValueError("日志归档目录必须位于私有存储。")
    folder = root / str(report.pk)
    if create:
        folder.mkdir(parents=True, exist_ok=True)
    return folder


def policy_for(owner):
    policies = CombatLogPolicy.objects.in_bulk(["default", str(owner.pk)])
    return policies.get(str(owner.pk)) or policies.get("default") or CombatLogPolicy(scope="default")


def purge_files(pk):
    # 路径只来自数据库 UUID；删除失败时保留持久任务，下次维护继续。
    pk = uuid.UUID(str(pk))
    report = CombatLogReport(pk=pk)
    for tier in ("hot", "cold"):
        path = directory(report, tier, create=False)
        if path.is_symlink() or path.resolve().parent != path.parent or path.name != str(pk):
            raise ValueError("归档路径异常，停止清理。")
        if path.exists():
            shutil.rmtree(path)
    CombatLogPurge.objects.filter(pk=pk).delete()


def record_purge(sender, instance, using, **kwargs):
    pk = instance.pk
    CombatLogPurge.objects.using(using).get_or_create(pk=pk)
    transaction.on_commit(lambda: purge_files(pk), using=using, robust=True)


def expire_reports():
    from botend.services import combat_public_events, combat_wcl_index
    combat_wcl_index.expire()
    combat_public_events.expire_snapshots()
    now = timezone.now()
    expired = CombatLogReport.objects.filter(expires_at__lte=now).exclude(
        state="running", updated_at__gte=now - LEASE)
    deleted = 0
    for pk in list(expired.values_list("pk", flat=True)[:500]):
        with transaction.atomic():
            report = expired.select_for_update().filter(pk=pk).first()
            if report:
                report.delete()
                deleted += 1
    for pk in CombatLogPurge.objects.values_list("pk", flat=True)[:500]:
        try:
            purge_files(pk)
        except (OSError, ValueError):
            logger.exception("回收日志目录失败 %s", pk)
    return deleted


def validate_file(name, size):
    maximum = getattr(settings, "COMBAT_LOG_MAX_BYTES", 2 * 1024**3)
    if (
        not name
        or len(name) > 240
        or not name.lower().endswith(".txt")
        or not 0 < size <= maximum
        or any(ord(char) < 32 or char in "/\\" for char in name)
    ):
        raise ValueError(f"请选择非空的 .txt 战斗日志，大小上限为 {maximum // 1024**2} MiB。")


def create_batch(owner, items):
    """同一批次统一预留名额；直到替换日志解析成功才回收旧归档。"""
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=owner.pk)
        policy = policy_for(owner)
        reports = CombatLogReport.objects.filter(owner=owner)
        now = timezone.now()
        matching = reports.filter(upload_key__in=[key for _, _, key in items if key])
        matching.filter(expires_at__lte=now).update(upload_key=None)
        existing = {r.upload_key: r for r in matching}
        new = []
        for name, size, key in items:
            validate_file(name, size)
            if key not in existing:
                new.append((name, size, key))
            elif (existing[key].filename, existing[key].size) != (name, size):
                raise ValueError("续传记录与所选场次不一致。")
        if not new:
            return [existing[k] for _, _, k in items]
        if len(items) > policy.max_reports:
            raise ValueError(f"本批场次数超过账号上限（{policy.max_reports} 份）。")
        live = reports.filter(retire_after__isnull=True, expires_at__gt=now)
        excess = max(0, live.count() + len(new) - policy.max_reports)
        victims = []
        if excess:
            if not policy.recycle_oldest:
                raise ValueError(f"已达到账号的 {policy.max_reports} 份上限，请先移除日志或减少场次数。")
            victims = list(live.filter(state__in=["ready", "failed"], replacements__isnull=True)
                           .exclude(pk__in=[r.pk for r in existing.values()]).order_by("created_at", "pk")[:excess])
            if len(victims) < excess:
                raise ValueError("可替换的旧日志不足，请先完成或移除待上传日志。")
        check_quota(owner, sum(size for _, size, _ in new))
        expiry = timezone.now() + timedelta(days=policy.retention_days)
        for i, (name, size, key) in enumerate(new):
            report = reports.create(filename=name, size=size, owner=owner, part_size=PART_SIZE,
                                    upload_key=key, expires_at=expiry)
            existing[key] = report
            for victim in victims[i::len(new)]:
                victim.retire_after = report
                victim.save(update_fields=["retire_after"])
        return [existing[k] for _, _, k in items]


def create(owner, name, size):
    return create_batch(owner, [(name, size, None)])[0]


def part_count(report):
    return (report.size + report.part_size - 1) // report.part_size


def account_byte_quota(owner):
    reports = CombatLogReport.objects.filter(owner=owner)
    reserved = reports.aggregate(total=Sum(Coalesce("reserved_bytes", "size")))["total"] or 0
    return reserved, getattr(settings, "COMBAT_LOG_ACCOUNT_BYTES", 10 * 1024**3)


def check_quota(owner, addition):
    reserved, limit = account_byte_quota(owner)
    if reserved + addition > limit:
        raise ValueError("账号日志归档配额不足。")


def pause_upload(owner, pk, *, before=None):
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=owner.pk)
        report = CombatLogReport.objects.select_for_update().get(pk=pk, owner=owner, expires_at__gt=timezone.now())
        if report.state != "uploading" or (before and report.updated_at >= before):
            return
        report.reserved_bytes = sum(
            min(report.part_size, report.size - n * report.part_size)
            for n in report.parts.values_list("number", flat=True)
        )
        report.state = "paused"
        report.save(update_fields=["state", "reserved_bytes", "updated_at"])


def put_part(owner, pk, number, data):
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=owner.pk)
        report = CombatLogReport.objects.select_for_update().get(owner=owner, pk=pk, expires_at__gt=timezone.now())
        if number >= part_count(report) or len(data) != min(
            report.part_size, report.size - number * report.part_size
        ):
            raise ValueError("分片长度或序号不符。")
        digest = hashlib.sha256(data).hexdigest()
        previous = report.parts.filter(number=number).first()
        if previous:
            if previous.digest != digest:
                raise ValueError("已接收分片与当前文件不同，请重新选择原文件。")
            return
        if report.state == "paused":
            check_quota(owner, report.size - report.reserved_bytes)
            report.state = "uploading"
            report.reserved_bytes = None
        if report.state != "uploading":
            raise ValueError("此报告已结束上传。")
        root = directory(report)
        if shutil.disk_usage(root).free < len(data) + getattr(
            settings, "COMBAT_LOG_MIN_FREE_BYTES", 256 * 1024**2
        ):
            raise ValueError("服务器可用空间不足。")
        packed = gzip.compress(data, compresslevel=3, mtime=0)
        target = root / (digest + ".gz")
        temp = root / (uuid.uuid4().hex + ".part")
        with temp.open("xb") as stream:
            stream.write(packed)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
        report.parts.create(number=number, digest=digest)
        report.save(update_fields=["state", "reserved_bytes", "updated_at"])


_index_lock = threading.Lock()
_index_thread = None
_index_wakeup = False


def launch(pk=None, attempt=None):
    """提交后在当前服务进程内消费队列；空队列立即退出。"""
    global _index_thread, _index_wakeup
    with _index_lock:
        if _index_thread is not None and _index_thread.is_alive():
            _index_wakeup = True
            return
        _index_wakeup = False
        _index_thread = threading.Thread(target=_run_indexes, args=(pk, attempt),
                                         name="combat-log-index", daemon=True)
        _index_thread.start()


def _run_indexes(pk, attempt):
    global _index_thread, _index_wakeup
    try:
        close_old_connections()
        if pk is not None:
            collect(pk, attempt)
        while True:
            while drain():
                pass
            with _index_lock:
                if not _index_wakeup:
                    _index_thread = None
                    return
                _index_wakeup = False
    except Exception:
        logger.exception("日志索引队列中断，待现有维护任务恢复")
    finally:
        with _index_lock:
            if _index_thread is threading.current_thread():
                _index_thread = None
        connections.close_all()


def maintain_archives():
    """随 LMonitor 既有维护周期清理到期日志，并接手因服务重启遗留的解析。"""
    deleted = expire_reports()
    reports = CombatLogReport.objects
    idle_days = getattr(settings, "COMBAT_LOG_PAUSE_IDLE_DAYS", None)
    if idle_days and idle_days >= 1:
        before = timezone.now() - timedelta(days=idle_days)
        idle = reports.filter(state="uploading", updated_at__lt=before).select_related("owner")
        for report in idle.order_by("updated_at")[:500]:
            pause_upload(report.owner, report.pk, before=before)
    if reports.filter(state="queued").exists() or reports.filter(
            state="running", updated_at__lt=timezone.now() - LEASE).exists():
        launch()
    return deleted


def enqueue(owner, pk, *, rebuild=False):
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=owner.pk)
        report = CombatLogReport.objects.select_for_update().get(owner=owner, pk=pk, expires_at__gt=timezone.now())
        if rebuild and report.retire_after_id:
            raise ValueError("此日志已预定替换，暂时无法重建。")
        if report.state == "ready" and not rebuild:
            return
        if report.state in ("queued", "running") and report.updated_at >= timezone.now() - LEASE:
            return
        if report.parts.count() != part_count(report):
            raise ValueError("仍有分片未上传。")
        if not report.upload_key and (
            CombatLogReport.objects.filter(
                owner=owner, state__in=["queued", "running"], updated_at__gte=timezone.now() - LEASE
            )
            .exclude(pk=pk)
            .exists()
        ):
            raise ValueError("当前账号已有日志正在解析，请稍后重试。")
        report.attempt = uuid.uuid4()
        report.state = "queued"
        report.message = ""
        report.recoveries = 0
        report.save(update_fields=["attempt", "state", "message", "updated_at", "recoveries"])
        transaction.on_commit(lambda: launch(pk, report.attempt), robust=True)


def index_path(report):
    if hasattr(report, "wcl_index"):
        return report.wcl_index
    return directory(report) / f"{report.published or report.attempt}.sqlite3"


def read_block(report, sha):
    other = "cold" if report.storage_tier == "hot" else "hot"
    for tier in (report.storage_tier, other, report.storage_tier):
        try:
            folder = directory(report, tier, create=False)
            if tier == "cold" and (folder / "archive.zip").is_file():
                with zipfile.ZipFile(folder / "archive.zip") as bundle:
                    packed = bundle.read(sha + ".gz")
            else:
                packed = (folder / (sha + ".gz")).read_bytes()
        except FileNotFoundError:
            continue
        data = gzip.decompress(packed)
        if hashlib.sha256(data).hexdigest() != sha:
            raise ValueError("归档块校验失败。")
        return data
    raise ValueError("归档缺少原文块。")


def archived_chunks(report):
    size = 0
    from contextlib import ExitStack

    with ExitStack() as stack:
        pack = directory(report, "cold", create=False) / "archive.zip"
        bundle = (
            stack.enter_context(zipfile.ZipFile(pack))
            if report.storage_tier == "cold" and pack.is_file()
            else None
        )
        for sha in report.parts.order_by("number").values_list("digest", flat=True).iterator():
            if bundle:
                data = gzip.decompress(bundle.read(sha + ".gz"))
                if hashlib.sha256(data).hexdigest() != sha:
                    raise ValueError("冷归档校验失败。")
            else:
                data = read_block(report, sha)
            size += len(data)
            yield data
    if size != report.size:
        raise ValueError("原文件长度校验失败。")


def settle_upload_quota(report):
    """在首次发布的事务内，持有账号锁后按当时的配额结算替换。"""
    owned = CombatLogReport.objects.filter(owner_id=report.owner_id)
    available = owned.filter(Q(retire_after__isnull=True) | Q(retire_after=report), expires_at__gt=timezone.now())
    policy = policy_for(report.owner)
    excess = available.count() - policy.max_reports
    if excess > 0:
        if not policy.recycle_oldest:
            raise ValueError("账号已满且当前设置为拒绝新上传，请释放名额后重试。")
        candidates = available.exclude(pk=report.pk).filter(state__in=["ready", "failed"], replacements__isnull=True)
        victims = list(candidates.order_by("created_at", "pk").values_list("pk", flat=True)[:excess])
        if len(victims) < excess:
            raise ValueError("可回收的旧日志不足，请释放名额后重试。")
        owned.filter(pk__in=victims).delete()
    owned.filter(retire_after=report).update(retire_after=None)


def collect(pk, attempt):
    task = CombatLogReport.objects.filter(pk=pk, attempt=attempt)
    with transaction.atomic():
        CombatLogScheduler.objects.select_for_update().get_or_create(pk=1)
        running = CombatLogReport.objects.filter(state="running", updated_at__gte=timezone.now() - LEASE)
        if running.count() >= getattr(settings, "COMBAT_LOG_WORKERS", 2):
            return False
        if running.filter(owner_id__in=task.values("owner_id")).exists():
            return False
        if not task.filter(state="queued").update(state="running", updated_at=timezone.now()):
            return False
    try:
        report = task.get()
        digest = hashlib.sha256()

        def verified_chunks():
            heartbeat = time.monotonic()
            for data in archived_chunks(report):
                digest.update(data)
                if time.monotonic() - heartbeat >= 10:
                    if not task.filter(state="running").update(updated_at=timezone.now()):
                        raise ValueError("任务已由新的解析进程接管。")
                    heartbeat = time.monotonic()
                yield data

        result = build_index(directory(report) / f"{attempt}.sqlite3", verified_chunks(),
                             headers_first=bool(report.upload_key and report.upload_key.version == 8))
        with transaction.atomic():
            get_user_model().objects.select_for_update().get(pk=report.owner_id)
            if report.expires_at <= timezone.now():
                raise ValueError("本次上传已到期。")
            published = task.filter(state="running").update(summary=result, digest=digest.hexdigest(),
                published=attempt, state="ready", updated_at=timezone.now())
            if published and report.published is None:
                settle_upload_quota(report)
    except Exception as exc:
        logger.exception("解析战斗日志失败 %s", pk)
        task.filter(state="running").update(
            state="failed",
            updated_at=timezone.now(),
            message=str(exc)[:240]
            if isinstance(exc, ValueError)
            else "归档或索引读取失败，请检查空间后重试。",
        )
    return True


def drain():
    now = timezone.now()
    stale = CombatLogReport.objects.filter(state="running", updated_at__lt=now - LEASE)
    for pk, attempt, retries in stale.values_list("pk", "attempt", "recoveries"):
        current = stale.filter(pk=pk, attempt=attempt)
        if retries >= 2:
            current.update(state="failed", message="解析进程反复中断，可手动重试。", updated_at=now)
        else:
            current.update(state="queued", attempt=uuid.uuid4(), recoveries=retries + 1, updated_at=now)
    for pk, attempt in (
        CombatLogReport.objects.filter(state="queued").order_by("updated_at").values_list("pk", "attempt")
    ):
        if collect(pk, attempt):
            return True
    return False


def read_report(report, fight_id, death_line):
    """原文只解压证据所在的分片；同一个窗口内复用已解压块。"""
    with localization.database(report,index_path(report)) as db:
        fights = [
            dict(row)
            for row in db.execute("""
            SELECT f.*, (SELECT count(*) FROM event WHERE kind='UNIT_DIED'
                AND line BETWEEN f.start_line AND f.end_line) AS deaths,
                (SELECT ms FROM event WHERE kind='UNIT_DIED' AND line BETWEEN f.start_line AND f.end_line
                 ORDER BY line LIMIT 1) AS first_death,
                (SELECT count(*) FROM interrupt WHERE line BETWEEN f.start_line AND f.end_line) AS kicks
            FROM fight f ORDER BY id
        """)
        ]
        observed = {entry["id"]: entry["observed_end_ms"] for entry in report.summary.get("activity_index", [])
                    if entry.get("observed_end_ms") is not None}
        for fight in fights:
            fight["seconds"] = None
            fight["first_death_seconds"] = None
            if fight["official_ms"] is not None:
                fight["official_seconds"] = fight["official_ms"] / 1000
            if fight["end_ms"] is not None and fight["start_ms"] is not None:
                fight["seconds"] = max(0, fight["end_ms"] - fight["start_ms"]) // 1000
            elif fight["id"] in observed and fight["start_ms"] is not None:
                fight["observed_seconds"] = max(0, observed[fight["id"]] - fight["start_ms"]) // 1000
            if fight["first_death"] is not None:
                fight["first_death_seconds"] = (fight["first_death"] - fight["start_ms"]) / 1000
        result = {"fights": fights, "legacy_partial_key": report.summary.get("boundary_revision", 0) < 2
                  and any(f["kind"] == "钥匙" and f["start_ms"] is not None and f["end_ms"] is None for f in fights)}
        if fight_id is None:
            return result
        fight = next((f for f in fights if f["id"] == fight_id), None)
        if not fight:
            raise LookupError
        comparable = ("session", "kind", "game_id", "level", "parent")
        result["comparisons"] = [f for f in fights if all(f[k] == fight[k] for k in comparable)]
        deaths = [
            dict(r)
            for r in db.execute(
                """
            SELECT line,target,ms FROM event WHERE kind='UNIT_DIED' AND line BETWEEN ? AND ? ORDER BY line
        """,
                (fight["start_line"], fight["end_line"]),
            )
        ]
        for death in deaths:
            death["seconds"] = (death["ms"] - fight["start_ms"]) / 1000
        result.update(selected=fight, deaths=deaths)
        target = "i.target"
        join = ""
        if report.summary.get("index_version",0)>=3:
            join = "LEFT JOIN fact f ON f.line=i.line LEFT JOIN actor a ON a.id=f.dst"
            target = "COALESCE(a.name,i.target)"
        result["interrupts"] = [
            dict(row)
            for row in db.execute(
                f"""
            SELECT i.spell_id, cn_spell(i.spell_id,i.spell) AS spell, i.actor, {target} AS target,
                   count(*) AS total, min(i.line) AS first_line
            FROM interrupt i {join} WHERE i.line BETWEEN ? AND ?
            GROUP BY i.spell_id,cn_spell(i.spell_id,i.spell),i.actor,{target} ORDER BY total DESC,first_line
        """,
                (fight["start_line"], fight["end_line"]),
            )
        ]
        if death_line is None:
            return result
        if not any(d["line"] == death_line for d in deaths):
            raise LookupError
        dead = db.execute("SELECT * FROM event WHERE line=?", (death_line,)).fetchone()
        rows = db.execute(
            """
            SELECT * FROM event WHERE dst=? AND ms BETWEEN ? AND ? AND line BETWEEN ? AND ?
            ORDER BY ms DESC,line DESC LIMIT 101
        """,
            (dead["dst"], dead["ms"] - 10000, dead["ms"], fight["start_line"], death_line),
        ).fetchall()
        result["limited"] = len(rows) > 100

        @lru_cache(maxsize=12)
        def block(number):
            part = report.parts.get(number=number)
            return read_block(report, part.digest)

        evidence = []
        legacy_ids = []
        for row in reversed(rows[:100]):
            pos, within = divmod(row["offset"], report.part_size)
            length = min(row["length"], 4096)
            raw = block(pos)[within : within + length]
            if len(raw) < length:
                raw += block(pos + 1)[: length - len(raw)]
            raw_text = raw.decode("utf-8", "replace").rstrip()
            fields = next(csv.reader([raw_text.partition("  ")[2]]), [])
            source = fields[2] if len(fields) > 2 and fields[2] != "nil" else ""
            sid = fields[9] if row["skill"] and len(fields)>9 else None
            if report.summary.get("index_version",0)<3:
                legacy_ids.append((localization.number(sid),localization.npc_id(row["src"])))
            skill,source = db.execute("SELECT cn_spell(?,?),cn_actor(?,?)",
                    (sid,row["skill"],row["src"],source)).fetchone()
            evidence.append(
                dict(
                    line=row["line"],
                    seconds=(row["ms"] - dead["ms"]) / 1000,
                    kind=row["kind"],
                    label=PLAYER_EVENTS[row["kind"]],
                    skill=skill,
                    source=source,
                    raw=raw_text,
                    clipped=row["length"] > length,
                )
            )
        if legacy_ids:
            spell_names = localization.spell_names({sid: r["skill"] for (sid,_),r in zip(legacy_ids,evidence) if sid})
            npc_names = localization.npc_names(nid for _,nid in legacy_ids)
            for (sid,nid),r in zip(legacy_ids,evidence):
                r["skill"] = spell_names.get(sid,r["skill"])
                r["source"] = npc_names.get(nid,r["source"])
        result["last_damage"] = next((item for item in reversed(evidence) if "伤害" in item["label"]), None)
        result.update(evidence=evidence, dead=dead["target"])
        return result
