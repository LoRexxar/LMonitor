import datetime
import html
import json
import os
import re
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.utils import timezone

from botend.models import PortalMplusSeasonCutoff, PortalVideo, WowArticle, WowDailyReport
from botend.wow_daily_report.html_design import render_daily_html_with_skill

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


def _date_range(local_date: datetime.date):
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.datetime.combine(local_date, datetime.time.min), tz)
    end = start + datetime.timedelta(days=1)
    return start, end


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
        num = float(value)
        if num.is_integer():
            return str(int(num))
        return f"{num:.1f}".rstrip("0").rstrip(".")
    except Exception:
        return str(value)


def _fmt_delta(current, previous) -> str:
    if current is None or previous is None:
        return "-"
    try:
        delta = float(current) - float(previous)
    except Exception:
        return "-"
    if abs(delta) < 0.05:
        return "0"
    prefix = "+" if delta > 0 else ""
    if float(delta).is_integer():
        return f"{prefix}{int(delta)}"
    return f"{prefix}{delta:.1f}".rstrip("0").rstrip(".")


def _fmt_source_time(value) -> str:
    if not value:
        return ""
    if hasattr(value, "isoformat"):
        try:
            return timezone.localtime(value).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return value.strftime("%Y-%m-%d %H:%M")
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%a %b %d %Y %H:%M:%S"):
        try:
            source = text[:24] if fmt.startswith("%a ") else text
            return datetime.datetime.strptime(source, fmt).strftime("%Y-%m-%d %H:%M")
        except Exception:
            continue
    return text


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
        if not (getattr(glm, "client", None) or getattr(glm, "coding_client", None) or (getattr(glm, "api_key", "") and (getattr(glm, "base_url", "") or getattr(glm, "coding_base_url", "")))):
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
    if not getattr(glm, "client", None):
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
        .order_by("-publish_time", "-id")
    )
    items = [
        {
            "title": _collapse_space(a.title_cn or a.title) or "（无标题）",
            "source": a.source or "unknown",
            "category": a.category or "unknown",
            "url": a.url or "",
            "publish_time": a.publish_time,
            "body": _article_full_text(a),
            "body_html": _article_body_html(a),
        }
        for a in rows
    ]
    return {"key": "news", "title": SECTION_TITLES["news"], "items": items}


def collect_nga_section(report_date: datetime.date) -> Dict[str, Any]:
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
            "body_html": _article_body_html(a),
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


def collect_cutoff_section(report_date: datetime.date, previous_ext: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    season = _latest_cutoff_season()
    rows = list(
        PortalMplusSeasonCutoff.objects.filter(season=season).order_by("region")
        if season != "unknown"
        else PortalMplusSeasonCutoff.objects.none()
    )
    previous_by_region = (((previous_ext or {}).get("cutoffs") or {}).get("by_region") or {})
    items = []
    for row in rows:
        prev_snapshot = previous_by_region.get(row.region) or {}
        cutoff_0_1_prev = row.cutoff_0_1_prev
        cutoff_1_prev = row.cutoff_1_prev
        if cutoff_0_1_prev is None:
            cutoff_0_1_prev = prev_snapshot.get("cutoff_0_1")
        if cutoff_1_prev is None:
            cutoff_1_prev = prev_snapshot.get("cutoff_1")
        items.append(
            {
                "season": row.season,
                "region": row.region,
                "region_label": REGION_LABELS.get(row.region, row.region.upper()),
                "cutoff_0_1": row.cutoff_0_1,
                "cutoff_0_1_prev": cutoff_0_1_prev,
                "cutoff_0_1_delta": _fmt_delta(row.cutoff_0_1, cutoff_0_1_prev),
                "cutoff_1": row.cutoff_1,
                "cutoff_1_prev": cutoff_1_prev,
                "cutoff_1_delta": _fmt_delta(row.cutoff_1, cutoff_1_prev),
                "source_updated_at": _fmt_source_time(row.source_updated_at),
            }
        )
    return {"key": "cutoffs", "title": SECTION_TITLES["cutoffs"], "season": season, "items": items}


def _load_previous_ext(report_date: datetime.date) -> Dict[str, Any]:
    row = WowDailyReport.objects.filter(report_date__lt=report_date).order_by("-report_date").first()
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
                clean[key] = _sanitize_body_html(value)
            elif hasattr(value, "isoformat"):
                clean[key] = value.isoformat()
            else:
                clean[key] = value
        items.append(clean)
    return {"key": section.get("key"), "title": section.get("title"), "items": items}


def _render_news_items(items: List[Dict[str, Any]]) -> str:
    if not items:
        return '<p class="empty">今日暂无相关新闻。</p>'
    out = ['<div class="article-list">']
    for item in items:
        out.append('<article class="daily-card news-card">')
        out.append(f'<h3 class="news-title"><a href="{_safe_html(item.get("url"))}" target="_blank" rel="noopener">{_safe_html(item.get("title"))}</a></h3>')
        out.append(
            f'<div class="meta"><span>来源：{_safe_html(item.get("source"))}</span><span>时间：{_safe_html(_fmt_dt(item.get("publish_time")))}</span></div>'
        )
        out.append(f'<a class="original-link" href="{_safe_html(item.get("url"))}" target="_blank" rel="noopener">原文链接</a>')
        out.append(f'<div class="item-summary"><strong>AI 总结：</strong>{_safe_html(item.get("summary") or _fallback_item_summary(item))}</div>')
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
    if not items:
        return '<p class="empty">今日暂无 NGA 热议记录。</p>'
    out = ['<div class="article-list">']
    for item in items:
        out.append('<article class="daily-card nga-card">')
        out.append(f'<h3><a href="{_safe_html(item.get("url"))}" target="_blank" rel="noopener">{_safe_html(item.get("title"))}</a></h3>')
        out.append(
            f'<div class="meta"><span>回复 {_safe_html(item.get("reply_count"))}</span><span>{_safe_html(_fmt_dt(item.get("publish_time")))}</span></div>'
        )
        body_html = _sanitize_body_html(item.get("body_html") or "")
        if body_html:
            out.append(f'<div class="body-html">{body_html}</div>')
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
        "<thead><tr><th>区域</th><th>0.1%</th><th>上次 0.1%</th><th>变化</th><th>1%</th><th>上次 1%</th><th>变化</th><th>数据更新时间</th></tr></thead>",
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


def render_daily_html(report_date: datetime.date, sections: List[Dict[str, Any]]) -> str:
    generated_at = timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M:%S")
    section_html = []
    body_renderers = {
        "news": _render_news_items,
        "nga": _render_nga_items,
        "videos": _render_video_items,
        "cutoffs": _render_cutoff_items,
    }
    for section in sections:
        key = section["key"]
        renderer = body_renderers[key]
        summary_html = f'<p class="section-summary">{_safe_html(section.get("summary") or "")}</p>' if section.get("summary") else ""
        section_html.append(
            "\n".join(
                [
                    f'<section class="daily-section daily-{_safe_html(key)}">',
                    f'<h2>{_safe_html(section["title"])}</h2>',
                    summary_html,
                    '<div class="section-body">',
                    renderer(section.get("items") or []),
                    "</div>",
                    "</section>",
                ]
            )
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>魔兽世界日报 - {_safe_html(report_date.strftime('%Y-%m-%d'))}</title>
  <style>
    :root {{ color-scheme: dark; --bg:#0b1020; --card:#151b2f; --muted:#9aa7bd; --text:#e7ecf6; --accent:#f7b955; --line:rgba(255,255,255,.1); }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:linear-gradient(180deg,#0b1020,#111827); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; line-height:1.7; }}
    a {{ color:#8fc7ff; text-decoration:none; }}
    a:hover {{ text-decoration:underline; }}
    .daily-report {{ max-width:1180px; margin:0 auto; padding:28px 20px 48px; }}
    .daily-hero {{ padding:26px 28px; border:1px solid var(--line); border-radius:18px; background:rgba(21,27,47,.86); box-shadow:0 20px 50px rgba(0,0,0,.22); }}
    .daily-hero h1 {{ margin:0 0 6px; font-size:30px; }}
    .daily-hero p,.meta {{ color:var(--muted); margin:0; }}
    .daily-section {{ margin-top:22px; padding:22px; border:1px solid var(--line); border-radius:18px; background:rgba(21,27,47,.72); }}
    .daily-section h2 {{ margin:0 0 10px; color:var(--accent); font-size:22px; }}
    .section-summary {{ margin:0 0 18px; padding:12px 14px; border-left:4px solid var(--accent); background:rgba(247,185,85,.08); border-radius:10px; }}
    .article-list {{ display:grid; grid-template-columns:1fr; gap:14px; }}
    .daily-card {{ border:1px solid var(--line); background:rgba(255,255,255,.04); border-radius:14px; padding:16px; }}
    .daily-card h3 {{ margin:0 0 8px; font-size:18px; }}
    .meta {{ display:flex; gap:12px; flex-wrap:wrap; font-size:13px; margin-bottom:8px; }}
    .original-link {{ display:inline-flex; align-items:center; gap:6px; margin:2px 0 8px; padding:5px 10px; border:1px solid rgba(143,199,255,.35); border-radius:999px; background:rgba(143,199,255,.08); font-size:13px; font-weight:700; }}
    .item-summary {{ margin:10px 0; padding:10px 12px; border:1px solid rgba(247,185,85,.22); background:rgba(247,185,85,.08); border-radius:10px; color:#f4e5c0; }}
    .original-label {{ margin:12px 0 6px; color:var(--muted); font-size:13px; font-weight:700; letter-spacing:.04em; }}
    .body-text, .body-html {{ margin:8px 0 0; color:#d7deea; }}
    .body-html {{ overflow-x:auto; }}
    .body-html p {{ margin:8px 0; }}
    .body-html ul,.body-html ol {{ margin:8px 0 8px 22px; padding:0; }}
    .body-html blockquote {{ margin:10px 0; padding:8px 12px; border-left:3px solid var(--accent); background:rgba(247,185,85,.07); }}
    .body-html img {{ max-width:100%; height:auto; border-radius:10px; }}
    .body-html table {{ margin:10px 0; min-width:520px; }}
    .video-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:14px; }}
    .video-card img {{ width:100%; aspect-ratio:16/9; object-fit:cover; border-radius:12px; margin-bottom:10px; background:#000; }}
    table {{ width:100%; border-collapse:collapse; overflow:hidden; border-radius:12px; }}
    th,td {{ border-bottom:1px solid var(--line); padding:10px 12px; text-align:left; }}
    th {{ color:#fff; background:rgba(255,255,255,.07); }}
    .delta {{ font-weight:700; color:#f7d48a; }}
    .empty {{ color:var(--muted); margin:0; }}
  </style>
</head>
<body>
  <main class="daily-report">
    <header class="daily-hero">
      <h1>魔兽世界日报</h1>
      <p>{_safe_html(report_date.strftime('%Y-%m-%d'))} · 生成时间 {_safe_html(generated_at)}</p>
    </header>
    {' '.join(section_html)}
  </main>
</body>
</html>
"""


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

    previous_ext = _load_previous_ext(report_date)
    sections = [
        collect_news_section(report_date),
        collect_nga_section(report_date),
        collect_cutoff_section(report_date, previous_ext=previous_ext),
    ]
    video_section = collect_video_section(report_date)
    if video_section.get("items"):
        sections.insert(2, video_section)

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
        if key == "cutoffs":
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
        ext_sections[key] = ext_section
        if key == "cutoffs":
            for item in section.get("items") or []:
                cutoff_snapshot[item["region"]] = {
                    "season": item.get("season"),
                    "cutoff_0_1": item.get("cutoff_0_1"),
                    "cutoff_1": item.get("cutoff_1"),
                    "source_updated_at": item.get("source_updated_at"),
                }

    design_sections = []
    for section in sections:
        design_section = dict(section)
        design_section["items"] = (_section_payload(section).get("items") or [])
        design_sections.append(design_section)
    html_content, design_meta = render_daily_html_with_skill(report_date, design_sections, use_llm=use_llm)
    if not html_content:
        html_content = render_daily_html(report_date, sections)
    rel_path, full_path = _static_report_path(report_date)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    ext = {
        "format": "html",
        "generated_at": timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M:%S"),
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
