import datetime
import json
import os
import re

from django.conf import settings
from django.db.models import Max
from django.utils import timezone

from botend.models import (
    PortalMplusRun,
    PortalMplusSeasonCutoff,
    PortalMythicstatsDpsRow,
    PortalPeakSpecRankRow,
    WowArticle,
    WowDailyReport,
    WowSkillDiffReport,
    WowWagoMonitorState,
)

try:
    from core.glm import GLMClient
except Exception:
    GLMClient = None


def _date_range(local_date):
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.datetime.combine(local_date, datetime.time.min), tz)
    end = start + datetime.timedelta(days=1)
    return start, end


def _safe_url(u):
    s = (u or "").strip()
    if not s or s in {"-", "#"}:
        return "-"
    return s


def _collapse_space(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _ensure_zh_len(text, *, min_len=100, max_len=200, pad=""):
    s = _collapse_space(text)
    if len(s) > max_len:
        return s[:max_len].rstrip()
    if len(s) >= min_len:
        return s
    tail = _collapse_space(pad)
    if not tail:
        tail = "数据来源为系统当日已采集入库内容，仅供快速浏览；如需细节请打开原链接查看全文，并以原站点信息为准。"
    need = min_len - len(s)
    if need > 0:
        s = (s + " " + tail[:need]).strip()
    if len(s) > max_len:
        s = s[:max_len].rstrip()
    return s


def _fmt_seconds(sec):
    try:
        sec = int(sec or 0)
    except Exception:
        sec = 0
    if sec <= 0:
        return "--"
    m = sec // 60
    s = sec % 60
    return f"{m}:{s:02d}"


def _load_prev_ext(report_date):
    prev = (
        WowDailyReport.objects.filter(report_date__lt=report_date)
        .order_by("-report_date", "-updated_at", "-id")
        .first()
    )
    if not prev:
        return {}
    raw = (getattr(prev, "ext_json", "") or "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _glm_summarize(*, title, desc, max_chars=160):
    if not GLMClient:
        return ""
    try:
        glm = GLMClient()
    except Exception:
        return ""
    if not getattr(glm, "client", None):
        return ""
    prompt = (
        "把下面这条魔兽世界资讯用中文概括成 100-160 字简介，不要换行，不要加标题：\n"
        + json.dumps({"title": title or "", "desc": desc or ""}, ensure_ascii=False)
    )
    out = glm.send_message(prompt, max_tokens=220, thinking_type="disabled")
    out = _collapse_space(out)
    if not out:
        return ""
    if len(out) > int(max_chars or 160):
        out = out[: int(max_chars or 160)].rstrip()
    return out


def _md_item(title, url, intro):
    return "\n".join(
        [
            f"### {_collapse_space(title) or '（无标题）'}",
            f"- 原链接：{_safe_url(url)}",
            _ensure_zh_len(intro),
            "",
        ]
    )


def _render_section(title, items):
    out = [f"## {title}", ""]
    if not items:
        out.append(_md_item("今日无新增", "-", "今日没有采集到该栏目对应的新内容或可识别变动。"))
        return "\n".join(out)
    for it in items:
        out.append(_md_item(it.get("title"), it.get("url"), it.get("intro")))
    return "\n".join(out)


def generate_wow_daily_report(*, report_date=None, use_llm=False):
    if report_date is None:
        report_date = timezone.localdate()
    today_start, today_end = _date_range(report_date)
    prev_ext = _load_prev_ext(report_date)

    news_rows = list(
        WowArticle.objects.filter(
            is_active=True,
            category="news",
            publish_time__gte=today_start,
            publish_time__lt=today_end,
        )
        .order_by("-publish_time", "-id")[:20]
    )
    news_items = []
    for a in news_rows[:10]:
        title = (a.title or "").strip()
        url = (a.url or "").strip()
        desc = (a.description or "").strip()
        intro = desc
        if use_llm and (not intro or len(_collapse_space(intro)) > 260):
            s = _glm_summarize(title=title, desc=desc)
            if s:
                intro = s
        if not intro:
            intro = f"来源：{(a.source or '').strip() or 'unknown'}；系统于今日采集入库，标题为“{title}”。若需要细节请打开原链接查看全文。"
        news_items.append({"title": title, "url": url, "intro": intro})

    nga_rows = list(
        WowArticle.objects.filter(
            is_active=True,
            source="nga",
            publish_time__gte=today_start,
            publish_time__lt=today_end,
        )
        .order_by("-reply_count", "-publish_time", "-id")[:10]
    )
    nga_items = []
    for a in nga_rows[:3]:
        title = (a.title or "").strip()
        url = (a.url or "").strip()
        reply = int(getattr(a, "reply_count", 0) or 0)
        intro = f"该帖为系统今日采集的 NGA 热议内容之一，当前记录的回复数为 {reply}。建议优先查看楼主核心观点与高赞回复，快速把握争议点、结论与实用信息。"
        nga_items.append({"title": title, "url": url, "intro": intro})

    wago_states = list(
        WowWagoMonitorState.objects.filter(
            is_active=True,
            last_event_at__gte=today_start,
            last_event_at__lt=today_end,
            last_event_status__icontains="has_class_change",
        ).order_by("-last_event_at", "-id")
    )
    wago_items = []
    for st in wago_states:
        ext_raw = (getattr(st, "ext", "") or "").strip()
        ext = {}
        if ext_raw:
            try:
                ext = json.loads(ext_raw)
            except Exception:
                ext = {}
        summary_title = (ext.get("summary_title") if isinstance(ext, dict) else "") or ""
        summary_title = (summary_title or "").strip()
        branch = (getattr(st, "branch", "") or "").strip()
        locale = (getattr(st, "locale", "") or "").strip()
        report_url = (getattr(st, "report_url", "") or "").strip()
        wago_url = (getattr(st, "wago_diff_url", "") or "").strip()
        title = summary_title or f"Wago 变更：{branch} {locale}".strip()
        intro = f"该条为系统今日监测到的 Wago DB2 差异与职业技能相关变动。点击可查看完整差异报告；若需要原始对比可使用 Wago Diff 链接进一步核对。"
        url = report_url or wago_url
        if not url:
            continue
        wago_items.append({"title": title, "url": url, "intro": intro})
        if len(wago_items) >= 6:
            break

    cutoff_latest = PortalMplusSeasonCutoff.objects.all().order_by("-updated_at", "-id").first()
    cutoff_items = []
    cutoff_snapshot = {}
    if cutoff_latest:
        season = (getattr(cutoff_latest, "season", "") or "").strip() or "unknown"
        regions = ["cn", "eu", "us"]
        rows = list(
            PortalMplusSeasonCutoff.objects.filter(season=season, region__in=regions)
            .order_by("region", "-updated_at", "-id")
        )
        by_region = {}
        for r in rows:
            key = (getattr(r, "region", "") or "").strip().lower()
            if key and key not in by_region:
                by_region[key] = r
        old = (((prev_ext.get("cutoffs") or {}) if isinstance(prev_ext, dict) else {}) or {}).get("by_region") or {}
        parts = []
        for reg in regions:
            row = by_region.get(reg)
            if not row:
                continue
            c01 = getattr(row, "cutoff_0_1", None)
            c1 = getattr(row, "cutoff_1", None)
            cutoff_snapshot[reg] = {"cutoff_0_1": c01, "cutoff_1": c1}
            old_reg = old.get(reg) if isinstance(old, dict) else None
            d01 = None
            d1 = None
            if isinstance(old_reg, dict):
                try:
                    if c01 is not None and old_reg.get("cutoff_0_1") is not None:
                        d01 = float(c01) - float(old_reg.get("cutoff_0_1"))
                except Exception:
                    d01 = None
                try:
                    if c1 is not None and old_reg.get("cutoff_1") is not None:
                        d1 = float(c1) - float(old_reg.get("cutoff_1"))
                except Exception:
                    d1 = None
            delta_txt = ""
            if d01 is not None or d1 is not None:
                delta_txt = f"（较上次日报：0.1% {('+' if (d01 or 0) >= 0 else '')}{(round(d01, 2) if d01 is not None else '--')}，1% {('+' if (d1 or 0) >= 0 else '')}{(round(d1, 2) if d1 is not None else '--')}）"
            parts.append(
                f"{reg.upper()} 0.1%：{(round(float(c01), 2) if c01 is not None else '--')}，1%：{(round(float(c1), 2) if c1 is not None else '--')}{delta_txt}"
            )
        if parts:
            intro = " | ".join(parts)
            intro = f"赛季：{season}。{intro}。分数线用于参考 0.1% 与 1% 的大秘境评分门槛，便于判断冲层进度与赛季节奏。"
            cutoff_items.append(
                {
                    "title": f"大秘境分数线变动（{season}）",
                    "url": f"https://raider.io/cn/mythic-plus/cutoffs/{season}/cn",
                    "intro": intro,
                }
            )

    run_latest = PortalMplusRun.objects.exclude(season__isnull=True).exclude(season="").order_by("-id").first()
    run_items = []
    run_snapshot = {}
    if run_latest:
        season = (getattr(run_latest, "season", "") or "").strip() or "unknown"
        region = (getattr(run_latest, "region", "") or "").strip() or "world"
        qs = PortalMplusRun.objects.filter(season=season, region=region, rank=1).exclude(dungeon_slug__isnull=True).exclude(dungeon_slug="")
        slugs = list(qs.values_list("dungeon_slug", flat=True).distinct())
        old = (((prev_ext.get("topruns") or {}) if isinstance(prev_ext, dict) else {}) or {}).get("by_dungeon") or {}
        new_records = []
        for slug in slugs:
            row = qs.filter(dungeon_slug=slug).order_by("time_seconds", "-level", "-id").first()
            if not row:
                continue
            cur = {
                "time_seconds": int(getattr(row, "time_seconds", 0) or 0),
                "level": int(getattr(row, "level", 0) or 0),
                "run_url": (getattr(row, "run_url", "") or "").strip(),
            }
            run_snapshot[slug] = cur
            old_row = old.get(slug) if isinstance(old, dict) else None
            if not isinstance(old_row, dict):
                continue
            try:
                if int(cur["time_seconds"] or 0) > 0 and int(old_row.get("time_seconds") or 0) > 0 and int(cur["time_seconds"]) < int(old_row.get("time_seconds")):
                    new_records.append((slug, old_row, cur))
            except Exception:
                continue

        if new_records:
            new_records.sort(key=lambda x: x[2].get("time_seconds") or 0)
            for slug, old_row, cur in new_records[:5]:
                title = f"TopRuns 新纪录：{slug}"
                url = cur.get("run_url") or f"https://raider.io/mythic-plus-runs/season-{season}"
                intro = (
                    f"该地下城出现新的最快限时记录：{_fmt_seconds(cur.get('time_seconds'))}（{cur.get('level')}层）。"
                    f"上次日报记录为 {_fmt_seconds(old_row.get('time_seconds'))}。建议点开原链接核对队伍构成与路线细节。"
                )
                run_items.append({"title": title, "url": url, "intro": intro})
        else:
            run_items.append(
                {
                    "title": f"TopRuns 无新最快记录（{season}）",
                    "url": "https://raider.io/mythic-plus-runs",
                    "intro": "与上一份日报快照相比，未检测到各地下城出现更快的限时成绩。若系统今日未刷新 TopRuns 数据，也可能导致变动暂未入库。",
                }
            )

    peak_latest = PortalPeakSpecRankRow.objects.filter(is_active=True).order_by("-updated_at", "-id").first()
    peak_items = []
    peak_snapshot = {}
    if peak_latest:
        season = (getattr(peak_latest, "season", "") or "").strip() or "unknown"
        region = (getattr(peak_latest, "region", "") or "").strip() or "world"
        rows = list(
            PortalPeakSpecRankRow.objects.filter(season=season, region=region, is_active=True)
            .exclude(character_name="")
            .order_by("class_slug", "spec_slug", "rank")
        )
        old = (((prev_ext.get("peak") or {}) if isinstance(prev_ext, dict) else {}) or {}).get("rows") or {}
        new_people = []
        for r in rows:
            key = f"{(r.class_slug or '').strip()}|{(r.spec_slug or '').strip()}|{int(r.rank or 0)}"
            name = (getattr(r, "character_name", "") or "").strip()
            if not key or not name:
                continue
            peak_snapshot[key] = name
            old_name = old.get(key) if isinstance(old, dict) else None
            if old_name and old_name != name:
                new_people.append((key, old_name, name, r))
        if new_people:
            for _key, old_name, name, r in new_people[:8]:
                title = f"巅峰榜新玩家：{(r.class_name or r.class_slug)}-{(r.spec_name or r.spec_slug)}"
                profile = (getattr(r, "character_path", "") or "").strip()
                url = "https://raider.io" + (profile if profile.startswith("/") else f"/{profile}") if profile else "https://raider.io"
                intro = (
                    f"该专精 Top{int(r.rank or 0)} 出现新的上榜玩家：{name}（替换 {old_name}）。"
                    f"当前记录分数为 {getattr(r, 'score', None) or '--'}，可点开链接查看角色详情与近期大秘境表现。"
                )
                peak_items.append({"title": title, "url": url, "intro": intro})
        else:
            peak_items.append(
                {
                    "title": f"巅峰榜未出现新玩家（{season}）",
                    "url": "https://raider.io",
                    "intro": "与上一份日报快照相比，未检测到各专精 Top3 名单出现新的角色名。若系统今日未刷新巅峰榜数据，也可能导致变动暂未入库。",
                }
            )

    ms_latest = PortalMythicstatsDpsRow.objects.all().order_by("-updated_at", "-id").first()
    ms_items = []
    ms_snapshot = {}
    if ms_latest:
        season = (getattr(ms_latest, "season", "") or "").strip() or "unknown"
        pid = (
            PortalMythicstatsDpsRow.objects.filter(season=season, dungeon_id=0, role="damage")
            .aggregate(Max("period_id"))
            .get("period_id__max")
        )
        period_label = ""
        if pid:
            any_row = (
                PortalMythicstatsDpsRow.objects.filter(season=season, dungeon_id=0, role="damage", period_id=pid)
                .order_by("rank")
                .first()
            )
            period_label = (getattr(any_row, "period_label", "") or "").strip() if any_row else ""
        rows = []
        if pid:
            rows = list(
                PortalMythicstatsDpsRow.objects.filter(season=season, dungeon_id=0, role="damage", period_id=pid)
                .order_by("rank")[:10]
            )
        if rows:
            top_names = [f"#{r.rank} {r.spec_name}" for r in rows[:5]]
            ms_snapshot = {"season": season, "period_id": int(pid or 0), "top10": [r.spec_slug for r in rows if r.spec_slug]}
            intro = (
                f"赛季：{season}，周期：{period_label or pid}（All dungeons）。"
                f"本期前五为：{('、'.join(top_names))}。该榜单为常规通报，用于快速把握当前环境下 DPS 专精整体梯度与排名变化。"
            )
            ms_items.append(
                {
                    "title": f"Mythicstats DPS 榜单通报（{period_label or pid}）",
                    "url": "https://mythicstats.com/dps",
                    "intro": intro,
                }
            )

    ext = {
        "generated_at": timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M:%S"),
        "cutoffs": {"by_region": cutoff_snapshot},
        "topruns": {"by_dungeon": run_snapshot},
        "peak": {"rows": peak_snapshot},
        "mythicstats": ms_snapshot,
    }

    md_parts = []
    md_parts.append(f"# 魔兽世界日报（{report_date.strftime('%Y-%m-%d')}）")
    md_parts.append("")
    md_parts.append(f"- 生成时间：{timezone.localtime(timezone.now()).strftime('%Y-%m-%d %H:%M:%S')}")
    md_parts.append("- 数据范围：仅统计系统今日已采集入库或可由快照差异判定的变动")
    md_parts.append("")
    md_parts.append(_render_section("魔兽世界新闻", news_items))
    md_parts.append(_render_section("NGA 热议追踪", nga_items))
    md_parts.append(_render_section("魔兽世界更新数据挖掘", wago_items))
    md_parts.append(_render_section("大秘境分数线变动", cutoff_items))
    md_parts.append(_render_section("大秘境 TopRuns 新最快记录", run_items))
    md_parts.append(_render_section("大秘境巅峰榜新玩家", peak_items))
    md_parts.append(_render_section("Mythicstats DPS 榜单通报", ms_items))

    md_content = "\n".join(md_parts).rstrip() + "\n"

    rel_path = f"portal/reports/wow_daily_report_{report_date.strftime('%Y-%m-%d')}.md"
    base_dir = str(getattr(settings, "BASE_DIR", "") or "")
    static_dir = os.path.join(base_dir, "static") if base_dir else os.path.join(os.getcwd(), "static")
    full_path = os.path.join(static_dir, rel_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    WowDailyReport.objects.update_or_create(
        report_date=report_date,
        defaults={
            "md_path": rel_path,
            "ext_json": json.dumps(ext, ensure_ascii=False),
        },
    )

    return {"md_path": rel_path, "full_path": full_path, "ext": ext}
