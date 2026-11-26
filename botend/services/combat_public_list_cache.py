"""关联来源的持久目录。只保存可展示的报告摘要，网络请求在事务外执行。"""
import json
from datetime import timedelta
from hashlib import sha256
from types import SimpleNamespace

from django.core.cache.backends.filebased import FileBasedCache
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from botend.models import CombatPublicReportEntry, CombatPublicReportPage, CombatPublicSource
from botend.services.combat_log_archive import directory
from botend.services import combat_wcl_catalog


COALESCE = timedelta(seconds=20)
LEASE = timedelta(minutes=2)
PAGE_SIZE = 20


def _cache():
    root = directory(SimpleNamespace(pk="public-report-pages"), create=False)
    return FileBasedCache(str(root), {"TIMEOUT": 86400, "OPTIONS": {"MAX_ENTRIES": 1024}})


def _key(source, page):
    identity = "\0".join((source.kind, source.region, source.realm, source.name))
    digest = sha256(identity.encode()).hexdigest()
    return f"public:v2:{source.user_id}:{source.pk}:{digest}:{page}"


def read(source, page):
    state = CombatPublicReportPage.objects.filter(source=source, page=page).first()
    if state and state.updated_at:
        return state
    # 升级后按访问迁入仍有效的旧目录，无需再次请求 WCL。
    try:
        legacy = _cache().get(_key(source, page))
    except OSError:
        legacy = None
    if legacy:
        updated, listing = legacy
        save(source, page, listing, started=updated)
        return CombatPublicReportPage.objects.filter(source=source, page=page).first()
    return None


def summaries(listing):
    reports = listing["data"]
    combat_wcl_catalog.summarize(reports)
    for r in reports:
        groups = r["groups"]
        title = r["title_label"]
        if len(groups) == 1:
            g = groups[0]
            title = g["name"] + " · " + g["difficulty"]
            if g["wipes"] and not g["dungeon"] and r["attempts"] > 1:
                title += "开荒"
        names = "、".join(g["name"] for g in groups[:3])
        yield r["code"], r["date"], dict(title=title, original=r["title"], owner=(r.get("owner") or {}).get("name"),
                                         detail=f"{r['attempts']} 场 · {names}")


def claim(source, page, force):
    now = timezone.now()
    eligible = CombatPublicReportPage.objects.filter(source=source, page=page).filter(
        Q(refresh_started=None) | Q(refresh_started__lt=now - LEASE))
    if not force:
        eligible = eligible.filter(Q(updated_at=None) | Q(updated_at__lt=now - COALESCE))
    return now if eligible.update(refresh_started=now) else None


def save(source, page, listing, *, started=None, leased=False):
    started = started or timezone.now()
    entries = list(summaries(listing))
    more = listing["has_more_pages"]
    revision = sha256(json.dumps([entries, more], default=str, sort_keys=True).encode()).hexdigest()
    with transaction.atomic():
        # 不同页可能包含同一报告；串行合并同一来源，并拒绝迟到的旧响应。
        if not CombatPublicSource.objects.filter(pk=source.pk).update(kind=F("kind")):
            return
        state, _ = CombatPublicReportPage.objects.get_or_create(source=source, page=page)
        if (leased and state.refresh_started != started) or (state.updated_at and state.updated_at > started):
            return
        newer = set(CombatPublicReportEntry.objects.filter(source=source, code__in=[e[0] for e in entries],
                    observed_at__gt=started).values_list("code", flat=True))
        CombatPublicReportEntry.objects.bulk_create([
            CombatPublicReportEntry(source=source, code=code, page=page, started_at=date, labels=labels, observed_at=started)
            for code, date, labels in entries if code not in newer
        ], update_conflicts=True, update_fields=["page","started_at","labels","observed_at"],
            **_conflict_target())
        state.codes = [e[0] for e in entries]
        state.has_more, state.revision, state.updated_at = more, revision, started
        state.save(update_fields=["codes","has_more","revision","updated_at"])


def _conflict_target():
    from django.db import connection
    return {"unique_fields":["source", "code"]} if connection.features.supports_update_conflicts_with_target else {}
