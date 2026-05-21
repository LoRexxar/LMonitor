import datetime
import json
import os
import re

from django.conf import settings
from django.db.models import Max, Q
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


def _sanitize_title(s):
    t = _collapse_space(s)
    if not t:
        return ""
    t = re.sub(r"^[\"“”']+|[\"“”']+$", "", t).strip()
    t = re.sub(r"^系统.*?(?:标题为|标题：)\s*", "", t).strip()
    return _collapse_space(t)


_SUMMARY_BAN_WORDS = ("入库", "采集", "数据库")


def _has_ban_word(s):
    t = str(s or "")
    return any(w in t for w in _SUMMARY_BAN_WORDS)


def _sanitize_summary(s):
    t = _collapse_space(s)
    if not t:
        return ""
    t = re.sub(r"^[\"“”']+|[\"“”']+$", "", t).strip()
    t = re.sub(r"^系统.*?(?:标题为|标题：)\s*", "", t).strip()
    parts = re.split(r"[。！？；;]\s*", t)
    parts = [p.strip() for p in parts if p and not _has_ban_word(p)]
    t = "。".join(parts).strip("。").strip()
    return _collapse_space(t)


def _normalize_body(s):
    t = str(s or "").replace("\r\n", "\n").replace("\r", "\n")
    t = t.strip()
    if not t:
        return ""
    lines = [ln.rstrip() for ln in t.split("\n")]
    out = []
    blank = 0
    for ln in lines:
        if not ln.strip():
            blank += 1
            if blank <= 1:
                out.append("")
            continue
        blank = 0
        out.append(ln)
    return "\n".join(out).strip()


def _ensure_zh_len(text, *, min_len=100, max_len=200, pad=""):
    s = _collapse_space(text)
    if len(s) > max_len:
        return s[:max_len].rstrip()
    if len(s) >= min_len:
        return s
    tail = _collapse_space(pad)
    if not tail:
        tail = "更多细节可在链接中查看队伍配置、路线与关键节点处理。"
    need = min_len - len(s)
    if need > 0:
        if s and s[-1] not in "。！？；;.!?":
            s = s + "。"
        s = (s + tail[:need]).strip()
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
        "你是一名日报编辑。请严格基于给定信息生成 100-160 字中文摘要："
        "不要出现“系统/入库/数据库/采集”等无效措辞；不要虚构未提供的事实；不要换行；不要加标题。\n"
        + json.dumps({"title": title or "", "desc": desc or ""}, ensure_ascii=False)
    )
    out = glm.send_message(prompt, max_tokens=220, thinking_type="disabled")
    out = _collapse_space(out)
    out = _sanitize_summary(out)
    if not out:
        return ""
    if len(out) > int(max_chars or 160):
        out = out[: int(max_chars or 160)].rstrip()
    return out


def _glm_summarize_payload(*, payload, min_chars=100, max_chars=200):
    if not GLMClient:
        return ""
    try:
        glm = GLMClient()
    except Exception:
        return ""
    if not getattr(glm, "client", None):
        return ""
    prompt = (
        "你是一名日报编辑。请严格基于给定信息生成 100-200 字中文摘要："
        "输出需要有分析视角，优先说明“发生了什么/变化幅度/可能原因或影响/玩家可关注点”；"
        "不要出现“系统/入库/数据库/采集”等无效措辞；不要虚构未提供的事实；不要换行；不要加标题。\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    out = glm.send_message(prompt, max_tokens=260, thinking_type="disabled")
    out = _collapse_space(out)
    out = _sanitize_summary(out)
    if not out:
        return ""
    if _has_ban_word(out):
        return ""
    if len(out) > int(max_chars or 200):
        out = out[: int(max_chars or 200)].rstrip()
    if len(out) < int(min_chars or 100):
        out = _ensure_zh_len(out, min_len=int(min_chars or 100), max_len=int(max_chars or 200))
    return out


def _md_item(title, url, intro, *, ensure_len=True):
    title = _sanitize_title(title) or "（无标题）"
    intro = _sanitize_summary(intro) or ""
    if ensure_len:
        intro = _ensure_zh_len(intro)
    return "\n".join(
        [
            f"### {title}",
            f"- 链接：{_safe_url(url)}",
            intro,
            "",
        ]
    )


def _md_news_item(title, url, body):
    title = _sanitize_title(title) or "（无标题）"
    body = _normalize_body(body)
    if not body:
        body = "（正文为空或抓取失败）"
    return "\n".join(
        [
            f"### {title}",
            f"- 链接：{_safe_url(url)}",
            "",
            body,
            "",
        ]
    )


def _render_news_section(title, items):
    out = [f"## {title}", ""]
    if not items:
        out.append("今日无新增。")
        out.append("")
        return "\n".join(out)
    for it in items:
        out.append(_md_news_item(it.get("title"), it.get("url"), it.get("body")))
    return "\n".join(out)


def _render_cutoff_section(*, title, items, snapshot):
    out = [f"## {title}", ""]
    if not items or not snapshot:
        out.append("今日无新增。")
        out.append("")
        return "\n".join(out)

    url = (items[0].get("url") if items else "") or "-"
    out.append(f"- 链接：{_safe_url(url)}")
    out.append("")
    out.append("| 区域 | 0.1% | Δ0.1% | 1% | Δ1% |")
    out.append("| --- | ---: | ---: | ---: | ---: |")
    for reg in ("cn", "eu", "us"):
        cur = (snapshot or {}).get(reg) if isinstance(snapshot, dict) else None
        if not isinstance(cur, dict):
            continue
        c01 = cur.get("cutoff_0_1")
        c1 = cur.get("cutoff_1")
        d01 = cur.get("delta_0_1")
        d1 = cur.get("delta_1")
        def _fmt_num(x):
            try:
                return f"{float(x):.2f}"
            except Exception:
                return "--"
        def _fmt_delta(x):
            try:
                v = float(x)
                sign = "+" if v >= 0 else ""
                return f"{sign}{v:.2f}"
            except Exception:
                return "--"
        out.append(f"| {reg.upper()} | {_fmt_num(c01)} | {_fmt_delta(d01)} | {_fmt_num(c1)} | {_fmt_delta(d1)} |")
    out.append("")
    return "\n".join(out)


def _md_mythicstats_item(*, title, url, season, period, top5, summary):
    title = _sanitize_title(title) or "（无标题）"
    season = _sanitize_title(season)
    period = _sanitize_title(period)
    top5 = top5 or []
    summary = _sanitize_summary(summary)
    if not summary:
        summary = (
            "从前五专精可以快速判断当前周期的强势梯队与环境倾向。"
            "建议结合自身队伍构成、词缀与目标副本选择，关注这些专精在关键窗口的爆发与稳定性差异。"
        )
    summary = _ensure_zh_len(summary, pad="更多细节可在链接中查看样本与统计口径。")
    out = [
        f"### {title}",
        f"- 链接：{_safe_url(url)}",
    ]
    if season or period:
        out.append(f"- 赛季/周期：{season or '--'} / {period or '--'}")
    if top5:
        out.append("- 前五：")
        for i, name in enumerate(top5[:5], start=1):
            out.append(f"  - #{i} {name}")
    out.append(summary)
    out.append("")
    return "\n".join(out)


def _render_mythicstats_section(*, title, items):
    out = [f"## {title}", ""]
    if not items:
        out.append("今日无新增。")
        out.append("")
        return "\n".join(out)
    for it in items:
        out.append(
            _md_mythicstats_item(
                title=it.get("title"),
                url=it.get("url"),
                season=it.get("season"),
                period=it.get("period"),
                top5=it.get("top5"),
                summary=it.get("summary"),
            )
        )
    return "\n".join(out)

def _topruns_record_fallback_intro(*, slug, old_row, cur, url):
    try:
        new_sec = int(cur.get("time_seconds") or 0)
    except Exception:
        new_sec = 0
    try:
        old_sec = int((old_row or {}).get("time_seconds") or 0)
    except Exception:
        old_sec = 0
    delta = old_sec - new_sec if (new_sec > 0 and old_sec > 0) else 0
    delta_txt = f"（提升 {delta} 秒）" if delta > 0 else ""
    base = (
        f"{slug} 出现新的最快限时记录：{_fmt_seconds(new_sec)}（{int(cur.get('level') or 0)}层），"
        f"对比上次日报 { _fmt_seconds(old_sec) }{delta_txt}。"
        "这类提升通常来自路线微调、爆发窗口安排更集中、关键怪处理更干净或更少的失误。"
        "建议重点对照队伍构成、词缀与拉怪节奏，提炼可复用的路线节点与控场细节。"
    )
    return _ensure_zh_len(base, pad="更多细节可在链接中查看队伍配置、路线与关键节点处理。")


def _peak_new_player_fallback_intro(*, class_name, spec_name, rank, new_player, old_player, score):
    score_txt = str(score) if score is not None else "--"
    base = (
        f"{class_name}-{spec_name} 的巅峰榜 Top{int(rank or 0)} 出现新上榜：{new_player}，替换 {old_player}。"
        f"当前分数 {score_txt}。"
        "这类变动通常意味着该专精在当前词缀/版本节奏下的路线与队伍配置有新的高分解法被验证，或是冲榜玩家在关键地下城完成了更稳的高层成绩。"
        "建议重点关注其近期钥石选择、队伍构成与路线节点处理，判断是否有可复用的拉怪节奏与控场细节。"
    )
    return _ensure_zh_len(base, pad="更多细节可在链接中查看角色近期大秘境记录与队伍构成。")


def _render_section(title, items):
    out = [f"## {title}", ""]
    if not items:
        out.append("今日无新增。")
        out.append("")
        return "\n".join(out)
    for it in items:
        out.append(_md_item(it.get("title"), it.get("url"), it.get("intro")))
    return "\n".join(out)


def generate_wow_daily_report(*, report_date=None, use_llm=True):
    if report_date is None:
        report_date = timezone.localdate()
    today_start, today_end = _date_range(report_date)
    prev_ext = _load_prev_ext(report_date)

    news_rows = list(
        WowArticle.objects.filter(is_active=True, category="news")
        .filter(publish_time__gte=today_start, publish_time__lt=today_end)
        .order_by("-publish_time", "-id")[:20]
    )
    news_items = []
    for a in news_rows[:10]:
        title = (a.title or "").strip()
        url = (a.url or "").strip()
        body = (a.description or "").strip()
        news_items.append({"title": title, "url": url, "body": body})

    nga_rows = list(
        WowArticle.objects.filter(is_active=True, source="nga")
        .filter(publish_time__gte=today_start, publish_time__lt=today_end)
        .order_by("-reply_count", "-publish_time", "-id")[:10]
    )
    nga_items = []
    for a in nga_rows[:3]:
        title = (a.title or "").strip()
        url = (a.url or "").strip()
        reply = int(getattr(a, "reply_count", 0) or 0)
        desc = _sanitize_summary((a.description or "").strip())
        intro = ""
        if use_llm:
            intro = _glm_summarize_payload(
                payload={
                    "type": "nga_hot",
                    "title": title,
                    "reply_count": reply,
                    "description": desc,
                    "url": url,
                }
            )
        if not intro:
            intro = desc or f"该帖为当日热议贴之一，当前可见回复数 {reply}。建议先定位楼主主张与结论，再浏览高赞回复提炼共识与争议点，最后对照自身职业/玩法选择可执行建议。"
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
        intro = ""
        if use_llm:
            intro = _glm_summarize_payload(
                payload={
                    "type": "wago_diff",
                    "title": title,
                    "branch": branch,
                    "locale": locale,
                    "report_url": report_url,
                    "wago_diff_url": wago_url,
                }
            )
        if not intro:
            intro = "该条为职业技能/数据表差异变动汇总，建议点开报告查看变更表与受影响职业概览，并结合补丁/热修语境判断实际影响。"
        url = report_url or wago_url
        if not url:
            continue
        wago_items.append({"title": title, "url": url, "intro": intro})
        if len(wago_items) >= 6:
            break

    cutoff_latest = PortalMplusSeasonCutoff.objects.all().order_by("-updated_at", "-id").first()
    cutoff_items = []
    cutoff_snapshot = {}
    cutoff_table_snapshot = {}
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
            cutoff_table_snapshot[reg] = {
                "cutoff_0_1": c01,
                "cutoff_1": c1,
                "delta_0_1": d01,
                "delta_1": d1,
            }
            delta_txt = ""
            if d01 is not None or d1 is not None:
                delta_txt = f"（较上次日报：0.1% {('+' if (d01 or 0) >= 0 else '')}{(round(d01, 2) if d01 is not None else '--')}，1% {('+' if (d1 or 0) >= 0 else '')}{(round(d1, 2) if d1 is not None else '--')}）"
            parts.append(
                f"{reg.upper()} 0.1%：{(round(float(c01), 2) if c01 is not None else '--')}，1%：{(round(float(c1), 2) if c1 is not None else '--')}{delta_txt}"
            )
        if parts:
            intro_base = " | ".join(parts)
            intro = ""
            if use_llm:
                intro = _glm_summarize_payload(
                    payload={
                        "type": "mplus_cutoff",
                        "season": season,
                        "regions": intro_base,
                    }
                )
            if not intro:
                intro = f"赛季：{season}。{intro_base}。用于快速判断 0.1%/1% 门槛是否上升或回落，辅助规划冲分节奏。"
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
                if use_llm:
                    s = _glm_summarize_payload(
                        payload={
                            "type": "topruns_record",
                            "dungeon_slug": slug,
                            "new_time": _fmt_seconds(cur.get("time_seconds")),
                            "new_level": cur.get("level"),
                            "old_time": _fmt_seconds(old_row.get("time_seconds")),
                            "url": url,
                        }
                    )
                    if s:
                        intro = s
                if not intro:
                    intro = _topruns_record_fallback_intro(slug=slug, old_row=old_row, cur=cur, url=url)
                run_items.append({"title": title, "url": url, "intro": intro})
        else:
            intro = (
                "与上一份日报快照相比，未检测到更快的限时成绩出现。"
                "若你在冲榜，可以继续关注各地下城的路线优化、阵容搭配与关键怪处理。"
                "多数排名提升来自更稳定的拉怪节奏、更少的死亡与更高效的爆发窗口利用；也建议对照本周词缀与热门路线的细节变化来做微调。"
            )
            if use_llm:
                s = _glm_summarize_payload(payload={"type": "topruns_no_change", "season": season})
                if s:
                    intro = s
            run_items.append({"title": f"TopRuns 无新最快记录（{season}）", "url": "https://raider.io/mythic-plus-runs", "intro": intro})

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
        old_by_spec = {}
        if isinstance(old, dict):
            for k, v in old.items():
                try:
                    parts = str(k).split("|")
                    if len(parts) != 3:
                        continue
                    cslug = (parts[0] or "").strip()
                    sslug = (parts[1] or "").strip()
                    rk = int(parts[2] or 0)
                except Exception:
                    continue
                if not cslug or not sslug or rk not in (1, 2, 3):
                    continue
                spec_key = f"{cslug}|{sslug}"
                old_by_spec.setdefault(spec_key, {})[rk] = (str(v or "")).strip()

        new_by_spec = {}
        candidates = []
        for r in rows:
            try:
                rk = int(getattr(r, "rank", 0) or 0)
            except Exception:
                rk = 0
            if rk not in (1, 2, 3):
                continue
            cslug = (r.class_slug or "").strip()
            sslug = (r.spec_slug or "").strip()
            name = (getattr(r, "character_name", "") or "").strip()
            if not cslug or not sslug or not name:
                continue
            key = f"{cslug}|{sslug}|{rk}"
            peak_snapshot[key] = name
            spec_key = f"{cslug}|{sslug}"
            new_by_spec.setdefault(spec_key, {})[rk] = name
            old_name = None
            try:
                old_name = old.get(key) if isinstance(old, dict) else None
            except Exception:
                old_name = None
            if old_name and str(old_name).strip() != name:
                candidates.append((spec_key, str(old_name).strip(), name, r, rk))

        swap_only_specs = set()
        for spec_key, new_map in new_by_spec.items():
            old_map = old_by_spec.get(spec_key) or {}
            old_set = {n for n in (old_map.values() or []) if (n or "").strip()}
            new_set = {n for n in (new_map.values() or []) if (n or "").strip()}
            if old_set and old_set == new_set:
                swap_only_specs.add(spec_key)

        new_people = []
        for spec_key, old_name, name, r, rk in candidates:
            if spec_key in swap_only_specs:
                continue
            old_set = {n for n in ((old_by_spec.get(spec_key) or {}).values() or []) if (n or "").strip()}
            if not old_set:
                continue
            if name in old_set:
                continue
            new_people.append((spec_key, old_name, name, r, rk))

        if new_people:
            for _spec_key, old_name, name, r, rk in new_people[:8]:
                class_name = (r.class_name or r.class_slug)
                spec_name = (r.spec_name or r.spec_slug)
                title = f"巅峰榜新玩家：{class_name}-{spec_name}"
                profile = (getattr(r, "character_path", "") or "").strip()
                url = "https://raider.io" + (profile if profile.startswith("/") else f"/{profile}") if profile else "https://raider.io"
                score = getattr(r, "score", None)
                intro = _peak_new_player_fallback_intro(
                    class_name=class_name,
                    spec_name=spec_name,
                    rank=rk,
                    new_player=name,
                    old_player=old_name,
                    score=score,
                )
                if use_llm:
                    old_map = old_by_spec.get(_spec_key) or {}
                    new_map = new_by_spec.get(_spec_key) or {}
                    old_top3 = [old_map.get(i) for i in (1, 2, 3)]
                    new_top3 = [new_map.get(i) for i in (1, 2, 3)]
                    s = _glm_summarize_payload(
                        payload={
                            "type": "peak_new_player",
                            "season": season,
                            "region": region,
                            "class": class_name,
                            "spec": spec_name,
                            "rank": int(rk or 0),
                            "new_player": name,
                            "old_player": old_name,
                            "score": score,
                            "old_top3": old_top3,
                            "new_top3": new_top3,
                            "url": url,
                        }
                    )
                    if s:
                        intro = s
                peak_items.append({"title": title, "url": url, "intro": intro})
        else:
            intro = (
                "与上一份日报快照相比，未检测到各专精 Top3 名单出现新的角色名。"
                "如果你在追榜，建议重点对照当前分数、钥石路线与队伍配置的变化，观察是否出现适配本周词缀的主流打法迁移。"
            )
            if use_llm:
                s = _glm_summarize_payload(payload={"type": "peak_no_change", "season": season})
                if s:
                    intro = s
            peak_items.append({"title": f"巅峰榜未出现新玩家（{season}）", "url": "https://raider.io", "intro": intro})

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
            top_names = [str(getattr(r, "spec_name", "") or "").strip() for r in rows[:5] if (getattr(r, "spec_name", "") or "").strip()]
            ms_snapshot = {"season": season, "period_id": int(pid or 0), "top10": [r.spec_slug for r in rows if r.spec_slug]}
            summary = ""
            if use_llm:
                summary = _glm_summarize_payload(
                    payload={
                        "type": "mythicstats_dps",
                        "season": season,
                        "period": period_label or pid,
                        "top5": [f"#{i+1} {n}" for i, n in enumerate(top_names[:5])],
                    }
                )
            ms_items.append(
                {
                    "title": f"Mythicstats DPS 榜单通报（{period_label or pid}）",
                    "url": "https://mythicstats.com/dps",
                    "season": season,
                    "period": str(period_label or pid),
                    "top5": top_names[:5],
                    "summary": summary,
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
    md_parts.append(f"- 更新时间：{timezone.localtime(timezone.now()).strftime('%Y-%m-%d %H:%M:%S')}")
    md_parts.append("")
    md_parts.append(_render_news_section("魔兽世界新闻", news_items))
    md_parts.append(_render_section("NGA 热议追踪", nga_items))
    md_parts.append(_render_section("魔兽世界更新数据挖掘", wago_items))
    md_parts.append(_render_cutoff_section(title="大秘境分数线变动", items=cutoff_items, snapshot=cutoff_table_snapshot))
    md_parts.append(_render_section("大秘境 TopRuns 新最快记录", run_items))
    md_parts.append(_render_section("大秘境巅峰榜新玩家", peak_items))
    md_parts.append(_render_mythicstats_section(title="Mythicstats DPS 榜单通报", items=ms_items))

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
