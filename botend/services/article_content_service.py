import html
import json
import re
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - runtime dependency is present in production
    BeautifulSoup = None


TEXT_BLOCK_TYPES = {"paragraph", "heading", "quote", "list_item"}


def dumps_blocks(blocks: Iterable[Dict[str, Any]]) -> str:
    return json.dumps(list(blocks or []), ensure_ascii=False)


def loads_blocks(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        return [b for b in raw if isinstance(b, dict)]
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [b for b in data if isinstance(b, dict)]


def blocks_to_plain_text(blocks: Iterable[Dict[str, Any]]) -> str:
    lines = []
    for block in blocks or []:
        block_type = block.get("type")
        if block_type in TEXT_BLOCK_TYPES:
            text = (block.get("text") or "").strip()
            if text:
                lines.append(text)
    return "\n".join(lines)


def article_blocks_match_reference(blocks: Iterable[Dict[str, Any]], reference_text: str = "", reference_title: str = "") -> bool:
    body = blocks_to_plain_text(blocks)
    if not body or len(body.strip()) < 20:
        return False
    if _contains_bad_article_markers(body):
        return False

    reference = "\n".join([reference_title or "", reference_text or ""]).strip()
    if not reference:
        return True
    if _contains_bad_article_markers(reference):
        return False

    body_tokens = _article_match_tokens(body)
    reference_tokens = _article_match_tokens(reference)
    if not reference_tokens:
        return True
    if not body_tokens:
        return False

    overlap = len(body_tokens & reference_tokens)
    min_required = min(8, max(3, len(reference_tokens) // 25))
    return overlap >= min_required


def _article_match_tokens(text: str) -> set:
    tokens = set()
    stop_words = {
        "with", "from", "that", "this", "have", "will", "your", "they", "them", "were", "been",
        "into", "also", "more", "some", "what", "when", "where", "which", "their", "there",
    }
    for token in re.findall(r"[A-Za-z][A-Za-z'’-]{3,}", text or ""):
        token = token.lower().strip("'’-")
        if token and token not in stop_words:
            tokens.add(token)
    return tokens


def _contains_bad_article_markers(text: str) -> bool:
    lowered = (text or "").lower()
    bad_markers = [
        "高危漏洞库", "漏洞名称", "阿里云安全专家", "avd-", "cve-",
        "直播间", "直播中", "人充电", "个人资料", "预约\n收起", "bilibili",
    ]
    return any(marker in lowered for marker in bad_markers)


def plain_text_to_blocks(text: str) -> List[Dict[str, Any]]:
    blocks = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            blocks.append({"type": "paragraph", "text": line})
    return blocks


def extract_structured_article(html_text: str, *, base_url: str = "", source: str = "") -> List[Dict[str, Any]]:
    if not html_text or not BeautifulSoup:
        return []
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "iframe", "form"]):
        tag.decompose()

    root = _select_article_root(soup, source=source)
    if not root:
        return []

    if source == "wowhead":
        _normalize_wowhead_inline_breaks(root)

    blocks = []
    for child in root.children:
        _append_node_blocks(child, blocks, base_url=base_url)

    if not blocks:
        text = _node_inline_text(root)
        blocks = plain_text_to_blocks(_clean_plain_text(text))
    return _dedupe_blocks(blocks)


def translate_blocks(blocks: Iterable[Dict[str, Any]], translated_pairs: Any) -> List[Dict[str, Any]]:
    source_blocks = [dict(b) for b in (blocks or []) if isinstance(b, dict)]
    pairs = translated_pairs
    if isinstance(translated_pairs, str):
        try:
            pairs = json.loads(translated_pairs)
        except Exception:
            pairs = []
    if not isinstance(pairs, list):
        pairs = []

    translated_by_original = {}
    translated_queue = []
    for pair in pairs:
        if isinstance(pair, dict):
            original = (pair.get("original") or "").strip()
            translated = (pair.get("translated") or "").strip()
            if original and translated:
                translated_by_original.setdefault(original, translated)
                translated_queue.append(translated)
        elif isinstance(pair, str) and pair.strip():
            translated_queue.append(pair.strip())

    queue_index = 0
    result = []
    for block in source_blocks:
        block_type = block.get("type")
        if block_type in TEXT_BLOCK_TYPES:
            original = (block.get("text") or "").strip()
            translated = translated_by_original.get(original)
            if not translated and queue_index < len(translated_queue):
                translated = translated_queue[queue_index]
                queue_index += 1
            new_block = dict(block)
            if translated:
                new_block["text"] = translated
                new_block["original"] = original
            result.append(new_block)
        else:
            result.append(block)
    return result


def _select_article_root(soup, *, source: str = ""):
    selectors = []
    if source == "wowhead":
        selectors.extend([
            "#blog-post .text",
            "#news-post .text",
            ".news-post .text",
            ".blog-post .text",
            "div.news-post-content",
            "div.news-post-text",
        ])
    selectors.extend([
        ".article-content",
        ".post-content",
        "div.content-body",
        ".topic-body.crawler-post .post",
        ".crawler-post .post",
        "article",
        "main",
        ".text",
    ])
    best = None
    best_len = 0
    for selector in selectors:
        for el in soup.select(selector):
            text_len = len(el.get_text(" ", strip=True) or "")
            if text_len > best_len:
                best = el
                best_len = text_len
    return best


def _append_node_blocks(node, blocks: List[Dict[str, Any]], *, base_url: str):
    name = getattr(node, "name", None)
    if not name:
        text = str(node).strip()
        if text:
            _append_text_block(blocks, "paragraph", text)
        return

    if name in {"h1", "h2", "h3", "h4"}:
        _append_text_block(blocks, "heading", _node_inline_text(node), level=int(name[1]))
        return
    if name == "p":
        _append_text_block(blocks, "paragraph", _node_inline_text(node))
        return
    if name == "blockquote":
        _append_text_block(blocks, "quote", _node_inline_text(node))
        return
    if name in {"ul", "ol"}:
        _append_list_blocks(node, blocks, base_url=base_url)
        return
    if name == "img":
        block = _image_block(node, base_url=base_url)
        if block:
            blocks.append(block)
        return
    if name in {"figure"}:
        img = node.find("img")
        block = _image_block(img, base_url=base_url) if img else None
        if block:
            caption = node.find("figcaption")
            if caption:
                block["caption"] = _node_inline_text(caption)
            blocks.append(block)
        return

    if name in {"div", "section"}:
        _append_container_blocks(node, blocks, base_url=base_url)
        return

    direct_text = _node_inline_text(node)
    if direct_text:
        _append_text_block(blocks, "paragraph", direct_text)


def _append_container_blocks(node, blocks: List[Dict[str, Any]], *, base_url: str):
    inline_parts = []
    for child in node.children:
        child_name = getattr(child, "name", None)
        if not child_name:
            text = str(child).strip()
            if text:
                inline_parts.append(text)
            continue
        if child_name in {"br"}:
            _flush_inline_parts(inline_parts, blocks)
            continue
        if child_name in _BLOCK_TAGS:
            _flush_inline_parts(inline_parts, blocks)
            _append_node_blocks(child, blocks, base_url=base_url)
            continue
        text = _node_inline_text(child)
        if text:
            inline_parts.append(text)
    _flush_inline_parts(inline_parts, blocks)


def _append_list_blocks(node, blocks: List[Dict[str, Any]], *, base_url: str):
    ordered = getattr(node, "name", None) == "ol"
    for li in node.find_all("li", recursive=False):
        inline_parts = []
        child_lists = []
        for child in li.children:
            child_name = getattr(child, "name", None)
            if child_name in {"ul", "ol"}:
                child_lists.append(child)
                continue
            if child_name in {"br"}:
                continue
            text = _node_inline_text(child) if child_name else str(child).strip()
            if text:
                inline_parts.append(text)

        text = _clean_inline_text(" ".join(inline_parts))
        if text:
            _append_text_block(blocks, "list_item", text, ordered=ordered)
        for child_list in child_lists:
            _append_list_blocks(child_list, blocks, base_url=base_url)


def _flush_inline_parts(inline_parts: List[str], blocks: List[Dict[str, Any]]):
    text = _clean_inline_text(" ".join([part for part in inline_parts if part]))
    inline_parts.clear()
    _append_text_block(blocks, "paragraph", text)


_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
    "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p",
    "pre", "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}


def _node_inline_text(node) -> str:
    text = node.get_text(" ", strip=True) if hasattr(node, "get_text") else str(node)
    return _clean_inline_text(text)


def _clean_inline_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:!?%)\]}>])", r"\1", text)
    text = re.sub(r"([([{<])\s+", r"\1", text)
    text = re.sub(r"(\d+)\s*/\s*(\d+)", r"\1/\2", text)
    text = re.sub(r"\s+([.。])", r"\1", text)
    return text.strip()


def _normalize_wowhead_inline_breaks(root):
    for tag in root.select("ins, .advertisement, .ad, .heading-size, .heading-permalink"):
        tag.decompose()


def _append_text_block(blocks: List[Dict[str, Any]], block_type: str, text: str, **extra):
    text = _clean_plain_text(html.unescape(text or ""))
    if not text:
        return
    block = {"type": block_type, "text": text}
    block.update(extra)
    blocks.append(block)


def _image_block(node, *, base_url: str) -> Optional[Dict[str, Any]]:
    if not node:
        return None
    src = (node.get("src") or node.get("data-src") or node.get("data-original") or "").strip()
    if not src:
        srcset = (node.get("srcset") or "").split(",")
        if srcset:
            src = srcset[0].strip().split(" ")[0]
    if not src or src.startswith("data:"):
        return None
    return {
        "type": "image",
        "url": urljoin(base_url, src),
        "alt": (node.get("alt") or "").strip(),
    }


def _clean_plain_text(text: str) -> str:
    text = re.sub(r"\r", "", text or "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join([line for line in lines if line]).strip()


def _dedupe_blocks(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = []
    previous_key = None
    for block in blocks:
        if block.get("type") in TEXT_BLOCK_TYPES:
            key = (block.get("type"), block.get("text"))
        else:
            key = (block.get("type"), block.get("url"))
        if key == previous_key:
            continue
        previous_key = key
        result.append(block)
    return result
