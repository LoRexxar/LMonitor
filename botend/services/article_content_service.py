import copy
import html
import json
import re
from typing import Any, Dict, Iterable, List
from urllib.parse import urljoin

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - runtime dependency is present in production
    BeautifulSoup = None


TEXT_BLOCK_TYPES = {"paragraph", "heading", "quote", "list_item"}
HTML_BLOCK_TYPES = {"html"}
TRANSLATABLE_BLOCK_TYPES = TEXT_BLOCK_TYPES | HTML_BLOCK_TYPES


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
        elif block_type == "html":
            text = html_block_to_plain_text(block)
            if text:
                lines.append(text)
    return "\n".join(lines)


def html_block_to_plain_text(block: Dict[str, Any]) -> str:
    html_text = (block or {}).get("html") or ""
    if not html_text or not BeautifulSoup:
        return ""
    soup = BeautifulSoup(html_text, "html.parser")
    return _clean_plain_text(soup.get_text("\n", strip=True))


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

    block = _html_block(root, base_url=base_url)
    if block:
        return [block]
    return plain_text_to_blocks(_clean_plain_text(root.get_text("\n", strip=True)))


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
        elif block_type == "html":
            result.append(html_block_translate_texts(block, translated_by_original, translated_queue[queue_index:]))
            queue_index = len(translated_queue)
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


def html_block_translate_texts(block: Dict[str, Any], translated_by_original: Dict[str, str], translated_queue: List[str]) -> Dict[str, Any]:
    new_block = dict(block or {})
    html_text = new_block.get("html") or ""
    if not html_text or not BeautifulSoup:
        return new_block
    soup = BeautifulSoup(html_text, "html.parser")
    queue_index = 0
    for text_node in soup.find_all(string=True):
        original = _clean_inline_text(str(text_node))
        if not original:
            continue
        translated = translated_by_original.get(original)
        if not translated and queue_index < len(translated_queue):
            translated = translated_queue[queue_index]
            queue_index += 1
        if translated:
            text_node.replace_with(translated)
    new_block["html"] = str(soup)
    if block.get("html"):
        new_block["original_html"] = block.get("html")
    return new_block


def _html_block(root, *, base_url: str) -> Dict[str, Any]:
    cloned = copy.copy(root)
    for tag in cloned.find_all(["script", "style", "nav", "header", "footer", "aside", "iframe", "form"]):
        tag.decompose()
    for tag in cloned.find_all("noscript"):
        tag.unwrap()
    for tag in cloned.select("ins, .advertisement, .ad, .heading-size, .heading-permalink"):
        tag.decompose()
    for tag in cloned.find_all(True):
        _sanitize_html_tag(tag, base_url=base_url)
    html_text = _clean_html_fragment("".join(str(child) for child in cloned.children))
    if not html_text:
        return {}
    return {"type": "html", "html": html_text}


def _sanitize_html_tag(tag, *, base_url: str):
    allowed_attrs = {"href", "src", "alt", "title", "class", "id", "colspan", "rowspan"}
    for attr in list(tag.attrs.keys()):
        if attr not in allowed_attrs:
            del tag.attrs[attr]
    for attr in ["href", "src"]:
        value = (tag.get(attr) or "").strip()
        if not value:
            continue
        if value.startswith(("javascript:", "data:")):
            del tag.attrs[attr]
            continue
        tag.attrs[attr] = urljoin(base_url, value)
    if tag.name == "a" and tag.get("href"):
        tag.attrs["target"] = "_blank"
        tag.attrs["rel"] = "noreferrer"


def _clean_html_fragment(html_text: str) -> str:
    html_text = re.sub(r"\n{3,}", "\n\n", html_text or "")
    return html_text.strip()


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
