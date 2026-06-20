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

    blocks = []
    for child in root.children:
        _append_node_blocks(child, blocks, base_url=base_url)

    if not blocks:
        text = root.get_text(separator="\n", strip=True)
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
        _append_text_block(blocks, "heading", node.get_text(" ", strip=True), level=int(name[1]))
        return
    if name == "p":
        _append_text_block(blocks, "paragraph", node.get_text(" ", strip=True))
        return
    if name == "blockquote":
        _append_text_block(blocks, "quote", node.get_text(" ", strip=True))
        return
    if name in {"ul", "ol"}:
        ordered = name == "ol"
        for li in node.find_all("li", recursive=False):
            _append_text_block(blocks, "list_item", li.get_text(" ", strip=True), ordered=ordered)
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
                block["caption"] = caption.get_text(" ", strip=True)
            blocks.append(block)
        return

    direct_text = _clean_plain_text(node.get_text("\n", strip=True))
    if name in {"div", "section"}:
        for child in node.children:
            _append_node_blocks(child, blocks, base_url=base_url)
        return
    if direct_text:
        _append_text_block(blocks, "paragraph", direct_text)


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
