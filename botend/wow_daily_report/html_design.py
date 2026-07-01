import json
import re
from typing import Any, Dict, List, Optional, Tuple

try:
    from core.glm import GLMClient
except Exception:  # pragma: no cover - optional runtime dependency
    GLMClient = None


class HtmlDesignError(Exception):
    pass


def extract_html(streamed: Any) -> str:
    text = str(streamed or "")
    if not text.strip():
        return ""
    fence = re.search(r"```(?:html|HTML)?\s*([\s\S]*?)```", text)
    if fence:
        inner = fence.group(1).strip()
        if inner.startswith("<"):
            return inner
    doctype = re.search(r"<!DOCTYPE\s+html", text, flags=re.I)
    if doctype:
        end = text.lower().rfind("</html>")
        if end >= 0:
            return text[doctype.start() : end + len("</html>")]
        return text[doctype.start() :].strip()
    html_start = re.search(r"<html[\s>]", text, flags=re.I)
    if html_start:
        end = text.lower().rfind("</html>")
        if end >= 0:
            return text[html_start.start() : end + len("</html>")]
        return text[html_start.start() :].strip()
    stripped = text.strip()
    if stripped.startswith("<") and "</" in stripped:
        return stripped
    return ""


def validate_html_document(value: Any) -> Tuple[bool, str]:
    html = str(value or "").strip()
    if not html:
        return False, "empty html"
    lower = html.lower()
    if "<html" not in lower or "</html>" not in lower:
        return False, "missing html document wrapper"
    if "<body" not in lower and "<main" not in lower:
        return False, "missing body/main content"
    return True, ""


def _json_safe(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def build_daily_report_payload(report_date, sections: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "title": "魔兽世界日报",
        "report_date": report_date.isoformat() if hasattr(report_date, "isoformat") else str(report_date),
        "sections": _json_safe(sections),
    }


def build_daily_report_design_prompt(payload: Dict[str, Any]) -> str:
    return """你是 LMonitor 的 HTML 视觉设计师，不是日报编辑。请把下面的结构化日报内容渲染成一份自包含单文件 HTML。

【硬性边界】
1. 只能做格式美化和排版，不要改写事实，不要总结，不要新增或删除条目。
2. 输入里的新闻正文使用 body_html 字段；这些是已经清洗过的文章 HTML 片段。必须尽量原样保留内部格式，例如 strong、em、ul/ol/li、table、a、img、blockquote、code、pre、ins、del。
3. 如果同时存在 body_text 和 body_html，以 body_html 为正文展示依据；body_text 只可用于摘要/无 HTML 兜底。
4. 每条新闻必须逐条展示：新闻标题、来源、链接、AI 总结（summary 字段）、新闻原文（body_html 字段，若没有则用 body 字段）。不得只展示模块摘要，也不得把多条新闻合并成一段。
5. 所有 title、url、source、publish_time、reply_count、cover_url、cutoff 数值必须来自输入，不得编造。
6. 输出必须是完整 HTML 文档：以 <!DOCTYPE html> 开头，包含 <html>、<head>、<body>，以 </html> 结束。
7. 输出纯 HTML，不要 markdown 围栏，不要解释文字。
8. CSS 写在 <style> 内；可以使用系统字体和内联 SVG/CSS，不要依赖不稳定外部图片。已有 cover_url/img src 可以保留。
9. 页面风格：暗色魔兽情报板；顶部紧凑横向指标条；只渲染输入 JSON 中实际存在的 sections；新闻/NGA/视频/大秘境分数线等模块清晰分区；如果没有 videos section，不要生成视频模块或“暂无视频”占位；大秘境分数线 cutoffs 板块只展示表格和数据，不要生成或展示 AI 总结/概括段；表格可横向滚动；移动端可读。
10. 不使用 scroll snap、滚轮劫持、右侧圆点导航。

【结构化日报 JSON】
""" + json.dumps(payload, ensure_ascii=False, indent=2)


def render_daily_html_with_skill(report_date, sections: List[Dict[str, Any]], *, use_llm: bool = True) -> Tuple[Optional[str], Dict[str, str]]:
    if not use_llm:
        return None, {"renderer": "fallback_static", "error": "llm_disabled"}
    if GLMClient is None:
        return None, {"renderer": "fallback_static", "error": "GLMClient unavailable"}
    payload = build_daily_report_payload(report_date, sections)
    prompt = build_daily_report_design_prompt(payload)
    try:
        glm = GLMClient()
        if not (getattr(glm, "client", None) or getattr(glm, "coding_client", None) or (getattr(glm, "api_key", "") and (getattr(glm, "base_url", "") or getattr(glm, "coding_base_url", "")))):
            return None, {"renderer": "fallback_static", "error": "GLM client not initialized and HTTP fallback not configured"}
        raw = glm.send_message(prompt, max_tokens=12000, thinking_type="disabled")
        if not raw:
            return None, {"renderer": "fallback_static", "error": getattr(glm, "last_error", "") or "empty model response"}
        html = extract_html(raw)
        ok, err = validate_html_document(html)
        if not ok:
            detail = getattr(glm, "last_error", "") or err
            return None, {"renderer": "fallback_static", "error": detail}
        return html, {"renderer": "ai_html_skill", "error": ""}
    except Exception as exc:
        return None, {"renderer": "fallback_static", "error": str(exc)[:500]}
