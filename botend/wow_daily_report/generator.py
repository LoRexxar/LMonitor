import datetime
import html
import json
import os
import re
import tempfile
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.utils import timezone
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from django.utils.safestring import SafeData
from botend.wow_daily_report.site_sections import collect_site_sections, day_window
from botend.wow_daily_report.edition import SHANGHAI, edition_for_report_date

from botend.models import PortalMplusSeasonCutoff, PortalVideo, WowArticle, WowDailyReport

try:
    from core.glm import GLMClient
except Exception:  # pragma: no cover - optional runtime dependency
    GLMClient = None

try:
    from utils.log import logger
except Exception:  # pragma: no cover
    logger = None


_LLM_RUN_ERRORS: List[Dict[str, str]] = []


REGION_LABELS = {
    "cn": "国服",
    "eu": "欧服",
    "us": "美服",
    "kr": "韩服",
    "tw": "台服",
    "world": "全球",
}


SECTION_TITLES = {
    "news": "魔兽世界当天新闻",
    "nga": "NGA 热议",
    "videos": "当前更新的 WoW 视频列表",
    "cutoffs": "大秘境分数线汇总",
}


def _llm_note(kind: str, err: Any) -> None:
    text = _collapse_space(err)[:500]
    if not text:
        return
    _LLM_RUN_ERRORS.append({"type": str(kind or "llm"), "error": text})
    if logger:
        try:
            logger.warning(f"[WowDailyReport][LLM] {kind}: {text}")
        except Exception:
            pass


def _collapse_space(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _strip_html(value: Any) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return html.unescape(_collapse_space(text))


def _truncate(value: Any, max_chars: int = 500) -> str:
    text = _collapse_space(value)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


def _safe_html(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


_ALLOWED_BODY_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "caption", "code", "del", "div", "em", "figcaption", "figure",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "ins", "li", "ol", "p", "pre", "s", "small",
    "span", "strong", "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
}
_ALLOWED_BODY_ATTRS = {"href", "src", "alt", "title", "class", "style", "colspan", "rowspan", "width", "height", "target", "rel"}


def _sanitize_body_html(value: Any) -> str:
    text = str(value or "")
    if not text.strip():
        return ""
    text = _bbcode_to_html(text)
    text = re.sub(r"<\s*(script|iframe|object|embed|form|input|button)[^>]*>[\s\S]*?<\s*/\s*\1\s*>", "", text, flags=re.I)
    text = re.sub(r"<\s*/?\s*(script|iframe|object|embed|form|input|button)[^>]*>", "", text, flags=re.I)

    def clean_tag(match):
        slash, tag, attrs = match.group(1), (match.group(2) or "").lower(), match.group(3) or ""
        if tag not in _ALLOWED_BODY_TAGS:
            return ""
        if slash:
            return f"</{tag}>"
        cleaned_attrs = []
        for attr_match in re.finditer(r"([:\w-]+)(?:\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s\"'=<>`]+))?", attrs):
            name = (attr_match.group(1) or "").lower()
            raw_value = attr_match.group(2) or ""
            if name.startswith("on") or name not in _ALLOWED_BODY_ATTRS:
                continue
            value = raw_value.strip().strip('"\'')
            if name in {"href", "src"} and re.match(r"\s*(javascript:|data:(?!image/))", value, flags=re.I):
                continue
            if name == "target" and value not in {"_blank", "_self"}:
                continue
            if name == "rel" and not value:
                continue
            cleaned_attrs.append(f'{name}="{_safe_html(value)}"')
        attr_text = (" " + " ".join(cleaned_attrs)) if cleaned_attrs else ""
        return f"<{tag}{attr_text}>"

    return re.sub(r"<\s*(/?)\s*([a-zA-Z0-9]+)([^>]*)>", clean_tag, text)


def _bbcode_to_html(value: Any) -> str:
    text = str(value or "")

    def replace_img(match):
        url = html.unescape((match.group(1) or "").strip())
        if not re.match(r"https?://", url, flags=re.I):
            return ""
        safe_url = _safe_html(url)
        return f'<figure><img src="{safe_url}" alt="NGA 图片"></figure>'

    return re.sub(r"\[img\]\s*([^\[]+?)\s*\[/img\]", replace_img, text, flags=re.I)


def _date_range(local_date: datetime.date):
    return day_window(local_date)


def _fmt_dt(dt) -> str:
    if not dt:
        return ""
    try:
        return timezone.localtime(dt).strftime("%H:%M")
    except Exception:
        return str(dt)


def _fmt_num(value) -> str:
    if value is None or value == "":
        return "-"
    try:
        num = Decimal(str(value))
        if not num.is_finite():
            return "-"
        text = f"{num:.2f}"
        return "0.00" if text == "-0.00" else text
    except (InvalidOperation, TypeError, ValueError):
        return "-"


def _fmt_delta(current, previous) -> str:
    if current is None or previous is None:
        return "-"
    try:
        delta = Decimal(str(current)) - Decimal(str(previous))
    except (InvalidOperation, TypeError, ValueError):
        return "-"
    text = _fmt_num(delta)
    if text in {"-", "0.00"}:
        return text
    return ("+" if delta > 0 else "") + text


def _source_datetime(value):
    if isinstance(value, datetime.datetime):
        return value if timezone.is_aware(value) else None
    text = str(value or '').strip()
    try:
        parsed = parse_datetime(text)
        if parsed is not None and timezone.is_aware(parsed):
            return parsed
        match = re.fullmatch(r'([A-Za-z]{3} [A-Za-z]{3} \d{1,2} \d{4} \d{2}:\d{2}:\d{2}) GMT([+-]\d{4})(?: \([^\r\n]*\))?', text)
        if match:
            return datetime.datetime.strptime(' '.join(match.groups()), '%a %b %d %Y %H:%M:%S %z')
    except ValueError:
        pass
    return None


def _fmt_source_time(value: Any) -> str:
    parsed = _source_datetime(value)
    if parsed is not None:
        return timezone.localtime(parsed).strftime('%Y-%m-%d %H:%M')
    return str(value or '').strip()


def _looks_like_json(value: Any) -> bool:
    text = str(value or "").strip()
    return (text.startswith("[") and text.endswith("]")) or (text.startswith("{") and text.endswith("}"))


def _extract_text_from_blocks(raw: Any) -> str:
    if not raw:
        return ""
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return ""
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return ""
    parts: List[str] = []
    for block in payload:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type") or ""
        if block_type not in ("html", "text", "paragraph"):
            continue
        text = _strip_html(block.get("html") or block.get("text") or "")
        if text:
            parts.append(text)
    return _collapse_space(" ".join(parts))


def _extract_html_from_blocks(raw: Any) -> str:
    if not raw:
        return ""
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return ""
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return ""
    parts: List[str] = []
    for block in payload:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type") or ""
        if block_type == "html" and block.get("html"):
            parts.append(str(block.get("html") or ""))
        elif block_type in ("text", "paragraph") and (block.get("text") or block.get("html")):
            parts.append(f"<p>{_safe_html(block.get('text') or block.get('html') or '')}</p>")
    return "\n".join([p for p in parts if p.strip()])


def _plain_text_to_html(text: str) -> str:
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
    if not paragraphs:
        return ""
    html_parts = []
    for paragraph in paragraphs:
        html_parts.append(f"<p>{_safe_html(paragraph).replace(chr(10), '<br>')}</p>")
    return "\n".join(html_parts)


def _article_body_html(article: WowArticle) -> str:
    for raw in (
        getattr(article, "content_blocks_cn", "") or "",
        getattr(article, "content_blocks", "") or "",
    ):
        html_body = _extract_html_from_blocks(raw)
        if html_body.strip():
            return html_body
    for raw in (getattr(article, "content_cn", "") or "", getattr(article, "content", "") or "", getattr(article, "description", "") or ""):
        if raw and not _looks_like_json(raw):
            text = str(raw or "").strip()
            if _strip_html(text):
                return _plain_text_to_html(text)
    return ""


def _article_text(article: WowArticle, max_chars: int = 800) -> str:
    candidates = [
        getattr(article, "description", "") or "",
        _extract_text_from_blocks(getattr(article, "content_blocks_cn", "") or ""),
        _extract_text_from_blocks(getattr(article, "content_blocks", "") or ""),
        getattr(article, "content_cn", "") or "",
        getattr(article, "content", "") or "",
    ]
    parts: List[str] = []
    for raw in candidates:
        if not raw or _looks_like_json(raw):
            continue
        text = _strip_html(raw)
        if text and text not in parts:
            parts.append(text)
    return _truncate(" ".join(parts), max_chars)


def _article_full_text(article: WowArticle) -> str:
    candidates = [
        _extract_text_from_blocks(getattr(article, "content_blocks_cn", "") or ""),
        _extract_text_from_blocks(getattr(article, "content_blocks", "") or ""),
        getattr(article, "content_cn", "") or "",
        getattr(article, "content", "") or "",
        getattr(article, "description", "") or "",
    ]
    for raw in candidates:
        if not raw or _looks_like_json(raw):
            continue
        text = _strip_html(raw)
        if text:
            return text
    return ""


def _fallback_item_summary(item: Dict[str, Any]) -> str:
    text = _collapse_space(item.get("body") or _strip_html(item.get("body_html") or ""))
    if not text:
        return "暂无可用正文，建议打开原文链接查看。"
    return _truncate(text, 180)


def _summarize_news_item(item: Dict[str, Any], *, use_llm: bool) -> Dict[str, Any]:
    fallback = _fallback_item_summary(item)
    if not use_llm:
        return {"text": fallback, "llm_ok": False, "error": "llm_disabled"}
    if not GLMClient:
        return {"text": fallback, "llm_ok": False, "error": "GLMClient 不可用"}
    source_text = _truncate(_strip_html(item.get("body_html") or item.get("body") or ""), 1600)
    if not source_text:
        return {"text": fallback, "llm_ok": False, "error": "empty_body"}
    try:
        glm = GLMClient()
        if not glm.available:
            return {"text": fallback, "llm_ok": False, "error": "GLM client 未初始化"}
        prompt = (
            "你是魔兽世界日报编辑。请只基于下面这条新闻原文，写一段 120 字以内中文总结。"
            "要求：讲清楚这条新闻具体发生了什么；不要编造；不要输出标题、项目符号或换行。\n"
            + json.dumps({
                "title": item.get("title") or "",
                "source": item.get("source") or "",
                "url": item.get("url") or "",
                "body": source_text,
            }, ensure_ascii=False)
        )
        out = glm.send_message(prompt, max_tokens=180, thinking_type="disabled")
        text = _collapse_space(out)
        if not text:
            return {"text": fallback, "llm_ok": False, "error": getattr(glm, "last_error", "") or "empty"}
        return {"text": _truncate(text, 160), "llm_ok": True, "error": ""}
    except Exception as exc:
        return {"text": fallback, "llm_ok": False, "error": str(exc)}


def _section_fallback_summary(section_key: str, items: List[Dict[str, Any]]) -> str:
    title = SECTION_TITLES.get(section_key, "日报模块")
    if not items:
        if section_key == "videos":
            return "今天暂时没有新的 WoW 视频更新。"
        return f"今天暂无新的{title}内容。"
    if section_key == "news":
        names = "、".join([i.get("title", "") for i in items[:3] if i.get("title")])
        return f"今天共有 {len(items)} 条魔兽世界新闻，重点包括{names}。"
    if section_key == "nga":
        names = "、".join([i.get("title", "") for i in items[:2] if i.get("title")])
        return f"今天 NGA 回复量最高的讨论集中在{names}，适合优先查看楼主观点和高回复分歧。"
    if section_key == "videos":
        names = "、".join([i.get("title", "") for i in items[:3] if i.get("title")])
        return f"今天更新了 {len(items)} 个 WoW 视频，包含{names}。"
    if section_key == "cutoffs":
        changes = [f"{i.get('region_label')} 0.1% {_fmt_delta(i.get('cutoff_0_1'), i.get('cutoff_0_1_prev'))}" for i in items]
        return "大秘境分数线今日更新，" + "，".join(changes[:3]) + "。"
    return f"今天共有 {len(items)} 条{title}内容。"


def _summarize_section(section_key: str, title: str, payload: Dict[str, Any], fallback: str, use_llm: bool) -> Dict[str, Any]:
    if not use_llm:
        return {"text": fallback, "llm_ok": False, "error": "llm_disabled"}
    if not GLMClient:
        _llm_note(section_key, "GLMClient 不可用")
        return {"text": fallback, "llm_ok": False, "error": "GLMClient 不可用"}
    try:
        glm = GLMClient()
    except Exception as exc:
        _llm_note(section_key, exc)
        return {"text": fallback, "llm_ok": False, "error": str(exc)}
    if not glm.available:
        _llm_note(section_key, "GLM client 未初始化")
        return {"text": fallback, "llm_ok": False, "error": "GLM client 未初始化"}

    prompt = (
        "你是魔兽世界日报编辑。请只基于下面这个模块的原始材料，写一段 200 字以内的中文摘要。"
        "要求：简单易懂，第一句话讲清楚这个模块发生了什么；不要虚构，不要提数据库/采集/系统；"
        "不要输出标题、项目符号或换行。日报稍后会在摘要后展示原文/列表/表格，所以你只负责概括。\n"
        + json.dumps({"section": title, "payload": payload}, ensure_ascii=False)
    )
    try:
        out = glm.send_message(prompt, max_tokens=260, thinking_type="disabled")
    except Exception as exc:
        _llm_note(section_key, exc)
        return {"text": fallback, "llm_ok": False, "error": str(exc)}
    text = _collapse_space(out)
    if not text:
        err = getattr(glm, "last_error", "") or "empty"
        _llm_note(section_key, err)
        return {"text": fallback, "llm_ok": False, "error": str(err)}
    if len(text) > 200:
        text = text[:200].rstrip()
    return {"text": text, "llm_ok": True, "error": ""}


NEWS_SECTION_SOURCES = ("wowhead", "blizzard_tracker")


def collect_news_section(report_date: datetime.date) -> Dict[str, Any]:
    from botend.services.blizzard_forum import article_channel
    start, end = _date_range(report_date)
    rows = (
        WowArticle.objects.filter(
            is_active=True,
            publish_time__gte=start,
            publish_time__lt=end,
            source__in=NEWS_SECTION_SOURCES,
        )
        .exclude(category="nga")
        .exclude(category="video")
        .select_related("blizzard_topic__category")
        .order_by("-publish_time", "-id")
    )
    items = [
        {
            "title": _collapse_space(a.title_cn or a.title) or "（无标题）",
            "source": a.source or "unknown",
            "category": a.category or "unknown",
            "url": a.url or "",
            "portal_url": reverse("portal_article", kwargs={"article_id": a.pk}),
            "publish_time": a.publish_time,
            "body": _article_full_text(a),
            "body_html": _article_body_html(a),
            **article_channel(a),
        }
        for a in rows
    ]
    return {"key": "news", "title": SECTION_TITLES["news"], "items": items}


def collect_nga_section(report_date: datetime.date) -> Dict[str, Any]:
    from botend.services.nga_browse_service import render_main_post
    start, end = _date_range(report_date)
    rows = (
        WowArticle.objects.filter(is_active=True, source="nga", publish_time__gte=start, publish_time__lt=end)
        .order_by("-reply_count", "-publish_time", "-id")[:2]
    )
    items = [
        {
            "title": _collapse_space(a.title_cn or a.title) or "（无标题）",
            "reply_count": int(a.reply_count or 0),
            "url": a.url or "",
            "publish_time": a.publish_time,
            "body": _article_text(a, max_chars=600),
            "body_html": render_main_post(a.content_cn or a.content) if (a.content_cn or a.content) else _article_body_html(a),
        }
        for a in rows
    ]
    return {"key": "nga", "title": SECTION_TITLES["nga"], "items": items}


def collect_video_section(report_date: datetime.date) -> Dict[str, Any]:
    start, end = _date_range(report_date)
    rows = (
        PortalVideo.objects.filter(is_active=True, tag="wow", published_at__gte=start, published_at__lt=end)
        .order_by("-published_at", "-id")
    )
    items = [
        {
            "title": _collapse_space(v.title) or "（无标题）",
            "url": v.url or "",
            "bvid": v.bvid or "",
            "cover_url": v.cover_url or "",
            "author_name": v.author_name or "",
            "author_url": v.author_url or "",
            "published_at": v.published_at,
        }
        for v in rows
    ]
    return {"key": "videos", "title": SECTION_TITLES["videos"], "items": items}


def _latest_cutoff_season() -> str:
    row = PortalMplusSeasonCutoff.objects.order_by("-updated_at", "-id").first()
    return getattr(row, "season", "") or "unknown"


def collect_cutoff_section(report_date: datetime.date, previous_ext: Optional[Dict[str, Any]] = None,
                           published_ext: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    frozen_by_region = (((published_ext or {}).get("cutoffs") or {}).get("by_region") or {})
    # Start with the edition's facts, not today's mutable central rows.  Even an
    # absent region, empty central table or season rollover cannot erase them.
    current_by_region = {region: {**snapshot, "snapshot_note": '沿用本日已发布快照；' + (
                             '时间为北京时间' if snapshot.get('source_timezone') == 'Asia/Shanghai'
                             else '原记录未保存来源时区')}
                         for region, snapshot in frozen_by_region.items()
                         if region in REGION_LABELS and isinstance(snapshot, dict)}
    frozen_seasons = {snapshot.get("season") for snapshot in current_by_region.values()}
    season = (next(iter(frozen_seasons)) if len(frozen_seasons) == 1 else "unknown"
              ) if current_by_region else _latest_cutoff_season()
    rows = (PortalMplusSeasonCutoff.objects.filter(season=season).order_by("region")
            if season and season != "unknown" else PortalMplusSeasonCutoff.objects.none())
    previous_by_region = (((previous_ext or {}).get("cutoffs") or {}).get("by_region") or {})
    _, report_end = _date_range(report_date)
    for row in rows:
        if row.region in current_by_region or row.region not in REGION_LABELS:
            continue
        source_stamp = _source_datetime(row.source_updated_at)
        if source_stamp and source_stamp >= report_end:
            # Only unfrozen regions in this edition's season may be filled, and
            # never with tomorrow's central values.
            continue
        if row.cutoff_0_1 is None and row.cutoff_1 is None:
            continue
        current_by_region[row.region] = {
            "season": row.season,
            "cutoff_0_1": row.cutoff_0_1,
            "cutoff_1": row.cutoff_1,
            "source_updated_at": _fmt_source_time(row.source_updated_at),
            "source_timezone": "Asia/Shanghai" if source_stamp else "",
        }
    items = []
    for region, item in sorted(current_by_region.items()):
        item.update(region=region, region_label=REGION_LABELS[region])
        prior = previous_by_region.get(region) or {}
        if not item.get("season") or prior.get("season") != item.get("season"):
            prior = {}
        # Compare only the same region/season in yesterday's published edition.
        # Central *_prev fields are collector latches, not daily-report facts.
        for field in ("cutoff_0_1", "cutoff_1"):
            item[field + "_prev"] = prior.get(field)
            item[field + "_delta"] = _fmt_delta(item.get(field), prior.get(field))
        items.append(item)
    return {"key": "cutoffs", "title": SECTION_TITLES["cutoffs"], "season": season, "items": items,
        "notice": f"冲分参考快照，见各区来源更新时间；变化较昨日已发布快照（{report_date - datetime.timedelta(days=1):%Y-%m-%d}），采样时点可能不同，不代表精确自然日涨幅；无可比昨日快照时显示“-”。"}


def _load_previous_ext(report_date: datetime.date) -> Dict[str, Any]:
    row = WowDailyReport.objects.filter(report_date=report_date - datetime.timedelta(days=1)).first()
    if not row or not row.ext_json:
        return {}
    try:
        return json.loads(row.ext_json)
    except Exception:
        return {}


def _section_payload(section: Dict[str, Any]) -> Dict[str, Any]:
    items = []
    for item in section.get("items") or []:
        clean = {}
        for key, value in item.items():
            if key == "body_html":
                clean[key] = str(value) if section.get('key') == 'nga' and isinstance(value, SafeData) else _sanitize_body_html(value)
            elif hasattr(value, "isoformat"):
                clean[key] = value.isoformat()
            else:
                clean[key] = value
        items.append(clean)
    return {**section, "items": items}


def _render_news_items(items: List[Dict[str, Any]], *, summary_only=False) -> str:
    if not items:
        return '<p class="empty">今日暂无相关新闻。</p>'
    out = ['<div class="article-list">']
    for item in items:
        out.append('<article class="daily-card news-card">')
        out.append(f'<h3 class="news-title"><a href="{_safe_html(item.get("url"))}" target="_blank" rel="noopener">{_safe_html(item.get("title"))}</a></h3>')
        channel = (f'<span class="bluepost-channel-badge" data-game-version="{_safe_html(item.get("game_version"))}">[{_safe_html(item.get("game_version_label"))}]</span>'
                   if item.get('source') == 'blizzard_tracker' and item.get('game_version_label') else '')
        out.append(
            f'<div class="meta"><span>来源：{_safe_html(item.get("source"))}</span>{channel}<span>时间：{_safe_html(_fmt_dt(item.get("publish_time")))}</span></div>'
        )
        out.append(f'<a class="original-link" href="{_safe_html(item.get("url"))}" target="_blank" rel="noopener">原文链接</a>')
        if item.get("portal_url"):
            out.append(f'<a class="original-link" href="{_safe_html(item["portal_url"])}">站内阅读</a>')
        summary_label = "AI 总结" if item.get("summary_llm_ok") else "内容摘要"
        summary = item.get("summary") or _fallback_item_summary(item)
        if summary_only:
            # Weekly news is one readable paragraph, not a hidden full article.
            out.append(f'<p class="item-summary"><strong>{summary_label}：</strong>{_safe_html(_collapse_space(summary))}</p>')
        else:
            out.append(f'<div class="item-summary"><strong>{summary_label}：</strong>{_safe_html(summary)}</div>')
            out.append('<div class="original-label">新闻原文</div>')
            body_html = _sanitize_body_html(item.get("body_html") or "")
            if body_html:
                out.append(f'<div class="body-html">{body_html}</div>')
            else:
                body = item.get("body") or "（暂无正文片段）"
                out.append(f'<p class="body-text">{_safe_html(body)}</p>')
        out.append('</article>')
    out.append('</div>')
    return "\n".join(out)


def _render_nga_items(items: List[Dict[str, Any]]) -> str:
    from botend.services.nga_browse_service import render_main_post
    if not items:
        return '<p class="empty">今日暂无 NGA 热议记录。</p>'
    out = ['<div class="article-list">']
    for item in items:
        out.append('<article class="daily-card nga-card">')
        out.append(f'<h3><a href="{_safe_html(item.get("url"))}" target="_blank" rel="noopener">{_safe_html(item.get("title"))}</a></h3>')
        out.append(
            f'<div class="meta"><span>回复 {_safe_html(item.get("reply_count"))}</span><span>{_safe_html(_fmt_dt(item.get("publish_time")))}</span></div>'
        )
        # Preserve validated SafeData from the public reader; do not re-escape URLs.
        body_html = item.get("body_html") or ""
        if not isinstance(body_html, SafeData):
            body_html = render_main_post(body_html)
        if body_html:
            from bs4 import BeautifulSoup
            fragment = BeautifulSoup(body_html, 'html.parser')
            for image in fragment.find_all('img'):
                image.attrs.setdefault('alt', 'NGA 图片')
            out.append(f'<div class="body-html">{fragment}</div>')
        else:
            out.append(f'<p class="body-text">{_safe_html(item.get("body") or "（暂无正文片段）")}</p>')
        out.append('</article>')
    out.append('</div>')
    return "\n".join(out)


def _render_video_items(items: List[Dict[str, Any]]) -> str:
    if not items:
        return '<p class="empty">今日暂无新视频。</p>'
    out = ['<div class="video-grid">']
    for item in items:
        out.append('<article class="daily-card video-card">')
        cover = item.get("cover_url") or ""
        if cover:
            out.append(f'<a href="{_safe_html(item.get("url"))}" target="_blank" rel="noopener"><img src="{_safe_html(cover)}" alt="{_safe_html(item.get("title"))}"></a>')
        out.append(f'<h3><a href="{_safe_html(item.get("url"))}" target="_blank" rel="noopener">{_safe_html(item.get("title"))}</a></h3>')
        author_url = item.get("author_url") or ""
        author = _safe_html(item.get("author_name") or "未知 UP")
        if author_url:
            author_html = f'<a href="{_safe_html(author_url)}" target="_blank" rel="noopener">{author}</a>'
        else:
            author_html = author
        out.append(f'<div class="meta"><span>{author_html}</span><span>{_safe_html(_fmt_dt(item.get("published_at")))}</span></div>')
        out.append('</article>')
    out.append('</div>')
    return "\n".join(out)


def _render_cutoff_items(items: List[Dict[str, Any]]) -> str:
    if not items:
        return '<p class="empty">今日暂无大秘境分数线数据。</p>'
    rows = [
        "<table>",
        "<thead><tr><th>区域</th><th>0.1%</th><th>昨日已发布 0.1%</th><th>较昨日已发布快照</th><th>1%</th><th>昨日已发布 1%</th><th>较昨日已发布快照</th><th>数据更新时间</th></tr></thead>",
        "<tbody>",
    ]
    for item in items:
        rows.append(
            "<tr>"
            f"<td>{_safe_html(item.get('region_label'))}</td>"
            f"<td>{_safe_html(_fmt_num(item.get('cutoff_0_1')))}</td>"
            f"<td>{_safe_html(_fmt_num(item.get('cutoff_0_1_prev')))}</td>"
            f"<td class=\"delta\">{_safe_html(item.get('cutoff_0_1_delta'))}</td>"
            f"<td>{_safe_html(_fmt_num(item.get('cutoff_1')))}</td>"
            f"<td>{_safe_html(_fmt_num(item.get('cutoff_1_prev')))}</td>"
            f"<td class=\"delta\">{_safe_html(item.get('cutoff_1_delta'))}</td>"
            f"<td>{_safe_html(item.get('source_updated_at'))}</td>"
            "</tr>"
        )
    rows.append("</tbody></table>")
    return "\n".join(rows)


def _render_rio_items(items):
    """A small, deterministic projection; the central source owns all facts."""
    if items and all(isinstance(item.get("weekly"), dict) for item in items):
        from botend.wow_daily_report.rio_weekly_presentation import render_weekly_rio_items
        return render_weekly_rio_items(items)
    from botend.wow_daily_report.presentation import _source_href

    def count(value, signed=False):
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            return '—'
        number = Decimal(str(value))
        if not number.is_finite() or number != number.to_integral_value():
            return '—'
        integer = int(number)
        return ('+' if signed and integer > 0 else '') + f'{integer:,}'

    def link(text, url):
        href = _source_href(url)
        return (f'<a href="{_safe_html(href)}" target="_blank" rel="noopener noreferrer">'
                f'{_safe_html(text)} ↗</a>') if href else ''

    cards = ['<div class="rio-report-grid">']
    for item in items:
        cards.append(f'<article class="rio-report-card" data-region="{_safe_html(item.get("region"))}">'
                     f'<header class="rio-report-heading"><h3>{_safe_html(item.get("region_label"))}</h3>'
                     f'<span>{_safe_html(item.get("season_name"))}</span></header>'
                     f'<p class="rio-report-date">数据日期 <time>{_safe_html(item.get("data_date"))}</time>'
                     + (' <span class="rio-report-status">保留快照</span>' if item.get('stale') else '') + '</p>'
                     f'<p class="rio-report-population"><strong>{count(item.get("population"))}</strong><span>参与角色</span></p>'
                     '<dl class="rio-report-metrics">')
        for label, key, day_key in [('较前一日', 'day_delta', 'previous_date'), ('较7日前', 'week_delta', 'week_date')]:
            value = item.get(key)
            direction = 'positive' if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else (
                'negative' if isinstance(value, (int, float)) and not isinstance(value, bool) and value < 0 else 'neutral')
            cards.append(f'<div><dt>{label}<span>{_safe_html(item.get(day_key))}</span></dt>'
                         f'<dd data-direction="{direction}">{count(value, True)}</dd></div>')
        cards.append('</dl>')
        if item.get('day_delta') is None or item.get('week_delta') is None:
            cards.append('<p class="rio-report-missing">“—”表示对应日期无可比数据。</p>')
        times = []
        if item.get('fetched_at'):
            times.append('采集：' + _fmt_source_time(item['fetched_at']))
        if item.get('upstream_at'):
            times.append('来源更新：' + _fmt_source_time(item['upstream_at']))
        if times:
            cards.append(f'<p class="rio-report-source">{_safe_html(" · ".join(times))}（北京时间）</p>')
        cards.append('<nav class="rio-report-links" aria-label="数据观察详情">'
                     + link('参与趋势', item.get('url')) + link('副本热力图', item.get('heatmap_url'))
                     + '</nav></article>')
    return ''.join(cards) + '</div>'


def _render_site_items(items):
    from urllib.parse import urlsplit
    from botend.wow_daily_report.presentation import _source_href

    def link(text, url):
        href = _source_href(url)
        return (f'<a href="{_safe_html(href)}" rel="noopener noreferrer" target="_blank">'
                f'{_safe_html(text)}</a>') if href else _safe_html(text)

    def icon(url, css_class):
        href = _source_href(url)
        if not href or urlsplit(href).scheme != 'https':
            return ''
        return (f'<img class="{css_class}" src="{_safe_html(href)}" alt="" '
                'width="20" height="20" loading="lazy" decoding="async">')

    rows = ['<div class="article-list site-context-list">']
    kinds = {'portal_event': 'event', 'wow_today_snapshot': 'today'}
    for item in items:
        kind = kinds.get((item.get('reference') or {}).get('kind'), 'update')
        facts = item.get('facts', [])
        status = ''
        if kind == 'event' and facts:
            status = f'<span class="context-status">{_safe_html(facts[0])}</span>'
            facts = facts[1:]
        heading = link(item.get('title'), item.get('url'))
        rows.append(f'<article class="daily-card site-context-card context-{kind}" data-source-kind="{kind}">'
                    f'<div class="context-heading">{icon(item.get("icon_url"), "context-icon")}'
                    f'<h3>{heading}</h3>{status}</div>')
        meta = [str(item.get(key) or '').strip() for key in ('source', 'time')]
        meta = [value for value in meta if value]
        if meta:
            rows.append(f'<p class="meta">{_safe_html(" · ".join(meta))}</p>')
        lines = item.get('lines')
        # Rich lines are a parallel presentation of these exact central facts,
        # never a replacement with independently shortened content.
        if not isinstance(lines, list) or [line.get('text') for line in lines] != facts:
            lines = [{'text': fact} for fact in facts]
        rows.append('<ul class="context-facts">')
        for line in lines:
            rows.append('<li>' + icon(line.get('icon_url'), 'context-fact-icon')
                        + f'<span>{link(line.get("text"), line.get("url"))}</span></li>')
        rows.append('</ul></article>')
    return '\n'.join(rows + ['</div>'])


def render_daily_html(report_date: datetime.date, sections: List[Dict[str, Any]], *, edition=None) -> str:
    from botend.wow_daily_report.presentation import render_daily_document
    if edition is None:
        edition = edition_for_report_date(report_date)
    return render_daily_document(report_date, sections, {
        'news': lambda items: _render_news_items(items, summary_only=edition['kind'] == 'wednesday_weekly'),
        'nga': _render_nga_items,
        'videos': _render_video_items, 'cutoffs': _render_cutoff_items,
        'today': _render_site_items, 'events': _render_site_items, 'updates': _render_site_items,
        'rio': _render_rio_items,
    }, generated_at=timezone.localtime(timezone.now(), timezone=SHANGHAI).strftime('%Y-%m-%d %H:%M:%S'),
        edition=edition)


def _static_report_path(report_date: datetime.date):
    rel_path = f"portal/reports/wow_daily_report_{report_date.strftime('%Y-%m-%d')}.html"
    base_dir = str(getattr(settings, "BASE_DIR", "") or "")
    static_dir = os.path.join(base_dir, "static") if base_dir else os.path.join(os.getcwd(), "static")
    full_path = os.path.join(static_dir, rel_path)
    return rel_path, full_path


def generate_wow_daily_report(*, report_date=None, use_llm=True):
    _LLM_RUN_ERRORS.clear()
    if report_date is None:
        report_date = timezone.localdate()
    if isinstance(report_date, str):
        report_date = datetime.date.fromisoformat(report_date)

    edition = edition_for_report_date(report_date)
    previous_ext = _load_previous_ext(report_date)
    published = WowDailyReport.objects.filter(report_date=report_date).only("ext_json").first()
    try:
        published_ext = json.loads(published.ext_json) if published else {}
    except (TypeError, ValueError):
        published_ext = {}
    sections = [
        collect_news_section(report_date),
        collect_nga_section(report_date),
        collect_cutoff_section(report_date, previous_ext=previous_ext, published_ext=published_ext),
    ]
    video_section = collect_video_section(report_date)
    if video_section.get("items"):
        sections.insert(2, video_section)

    from botend.wow_daily_report.rio_section import collect_rio_section
    rio_section = collect_rio_section(report_date, published_ext=published_ext,
                                      weekly=edition["kind"] == "wednesday_weekly")
    if rio_section.get('items'):
        sections.append(rio_section)
    available = {section["key"]: section for section in sections + collect_site_sections(report_date)}
    sections = [available[key] for key in ("rio", "news", "events", "updates", "nga", "videos", "today", "cutoffs") if key in available]

    ext_sections: Dict[str, Any] = {}
    cutoff_snapshot: Dict[str, Any] = {}
    for section in sections:
        key = section["key"]
        if key == "news":
            for item in section.get("items") or []:
                item_summary = _summarize_news_item(item, use_llm=use_llm)
                item["summary"] = item_summary["text"]
                item["summary_llm_ok"] = bool(item_summary.get("llm_ok"))
                item["summary_error"] = item_summary.get("error") or ""
        fallback = _section_fallback_summary(key, section.get("items") or [])
        payload = _section_payload(section)
        if key in ("rio", "cutoffs", "today", "events", "updates") or not section.get("items"):
            summary_result = {"text": "", "llm_ok": False, "error": "summary_disabled"}
        else:
            summary_result = _summarize_section(key, section["title"], payload, fallback, use_llm=use_llm)
        section["summary"] = summary_result["text"]
        ext_section = {
            "title": section["title"],
            "count": len(section.get("items") or []),
            "summary_llm_ok": bool(summary_result.get("llm_ok")),
            "summary_error": summary_result.get("error") or "",
        }
        if key == "news":
            source_counts: Dict[str, int] = {}
            for item in section.get("items") or []:
                source = str(item.get("source") or "unknown")
                source_counts[source] = source_counts.get(source, 0) + 1
            ext_section["source_counts"] = source_counts
        for field in ("notice", "source_time", "source_date", "snapshot_id"):
            if field in section:
                ext_section[field] = section[field]
        references = [item["reference"] for item in section.get("items", []) if item.get("reference")]
        if references:
            ext_section["references"] = references
        ext_sections[key] = ext_section
        if key == "cutoffs":
            for item in section.get("items") or []:
                cutoff_snapshot[item["region"]] = {
                    "season": item.get("season"),
                    "cutoff_0_1": item.get("cutoff_0_1"),
                    "cutoff_1": item.get("cutoff_1"),
                    "source_updated_at": item.get("source_updated_at"),
                    "source_timezone": item.get("source_timezone", ""),
                }

    # AI may summarize prose, never delete facts or freely redesign the document.
    html_content = render_daily_html(report_date, sections, edition=edition)
    design_meta = {"renderer": "daily_editorial_v2", "error": ""}
    rel_path, full_path = _static_report_path(report_date)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                dir=os.path.dirname(full_path), prefix=".daily-", suffix=".tmp", delete=False) as f:
            temporary_path = f.name
            f.write(html_content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, full_path)
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)

    ext = {
        "format": "html",
        "edition": edition,
        "generated_at": timezone.localtime(timezone.now(), timezone=SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
        "llm_enabled": bool(use_llm),
        "llm_errors": list(_LLM_RUN_ERRORS),
        "html_renderer": design_meta.get("renderer") or "fallback_static",
        **({"html_design_error": design_meta.get("error")} if design_meta.get("error") and design_meta.get("renderer") == "fallback_static" else {}),
        "sections": ext_sections,
        "cutoffs": {"by_region": cutoff_snapshot},
    }
    WowDailyReport.objects.update_or_create(
        report_date=report_date,
        defaults={"md_path": rel_path, "ext_json": json.dumps(ext, ensure_ascii=False)},
    )
    return {"md_path": rel_path, "full_path": full_path, "ext": ext}
