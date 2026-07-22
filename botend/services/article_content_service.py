import copy
import html
import json
import re
from typing import Any, Dict, Iterable, List
from urllib.parse import urljoin

from botend.services.wowhead_bbcode_renderer import (
    extract_wowhead_print_html_calls,
    render_wowhead_bbcode,
)

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
    if source == "wowhead":
        rendered_print_html = _materialize_wowhead_print_html(soup, base_url=base_url)
        if not rendered_print_html:
            _restore_wowhead_markup_screenshots(soup, base_url=base_url)
            _restore_wowhead_markup_item_table_cells(soup, base_url=base_url)
            _restore_wowhead_markup_spell_table_cells(soup, base_url=base_url)
            _restore_wowhead_markup_diff_marks(soup)
    for tag in soup(["script"]):
        tag.decompose()

    root = _select_article_root(soup, source=source)
    if not root:
        return []

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
    for text_node in _translatable_html_text_nodes(soup):
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


def html_block_text_nodes(html_text: str) -> List[str]:
    if not html_text or not BeautifulSoup:
        return []
    soup = BeautifulSoup(html_text or "", "html.parser")
    texts = []
    for text_node in _translatable_html_text_nodes(soup):
        text = _clean_inline_text(str(text_node))
        if text:
            texts.append(text)
    return texts


def _translatable_html_text_nodes(soup):
    skip_parents = {"script", "style", "iframe", "form", "noscript", "svg", "path", "title", "code", "pre"}
    for text_node in soup.find_all(string=True):
        parent = getattr(text_node, "parent", None)
        if not parent:
            continue
        if any(getattr(ancestor, "name", None) in skip_parents for ancestor in getattr(text_node, "parents", [])):
            continue
        text = _clean_inline_text(str(text_node))
        if not text:
            continue
        if parent.name == "a" and len(text) < 3:
            continue
        yield text_node


def _html_block(root, *, base_url: str) -> Dict[str, Any]:
    cloned = copy.copy(root)
    for tag in cloned.find_all(["script"]):
        tag.decompose()
    for tag in cloned.find_all("noscript"):
        tag.unwrap()
    _restore_empty_image_links(cloned, base_url=base_url)
    for tag in cloned.find_all(True):
        _sanitize_html_tag(tag, base_url=base_url)
    html_text = _clean_html_fragment("".join(str(child) for child in cloned.children))
    if not html_text:
        return {}
    return {"type": "html", "html": html_text}


def _is_image_url(value: str) -> bool:
    path = urljoin("", value or "").split("?", 1)[0].lower()
    return bool(re.search(r"\.(?:png|jpe?g|webp|gif)$", path))


def _restore_empty_image_links(root, *, base_url: str):
    """Turn Wowhead-rendered empty image anchors back into visible images.

    Wowhead article HTML can contain rendered ``<a href=image></a>`` anchors while the
    original BBCode ``[img]`` remains only inside a script. Since scripts are removed
    during sanitization, restore these anchors before image upload runs.
    """
    if not BeautifulSoup:
        return
    factory = BeautifulSoup("", "html.parser")
    for link in list(root.find_all("a")):
        href = (link.get("href") or "").strip()
        if not href:
            continue
        has_visible_text = bool(_clean_inline_text(link.get_text(" ", strip=True)))
        has_media = bool(link.find(["img", "picture", "video", "source"]))
        if has_visible_text or has_media:
            continue
        absolute_href = urljoin(base_url, href)
        if _is_image_url(absolute_href):
            img = factory.new_tag("img", src=absolute_href)
            alt = (link.get("title") or link.get("alt") or "").strip()
            if alt:
                img["alt"] = alt
            link.append(img)


def _wowhead_fallback_entity_metadata(soup) -> Dict[tuple, Dict[str, str]]:
    """Use rendered fallback links as stable names/URLs for BBCode entities."""
    result = {}
    entity_pattern = re.compile(r"/(item|spell|npc|object|quest|achievement)=(\d+)(?:/[^?#]*)?", re.I)
    for link in soup.find_all("a", href=True):
        match = entity_pattern.search(link.get("href") or "")
        if not match:
            continue
        name = _clean_inline_text(link.get_text(" ", strip=True))
        if not name:
            continue
        kind, entity_id = match.group(1).lower(), match.group(2)
        result.setdefault((kind, entity_id), {"name": name, "url": link.get("href")})
    for item_id, item in _wowhead_item_metadata(soup).items():
        current = result.setdefault(("item", item_id), {})
        if item.get("name"):
            current["name"] = item["name"]
        if item.get("icon"):
            current["icon"] = item["icon"]
    return result


def _materialize_wowhead_print_html(soup, *, base_url: str) -> bool:
    """Render authoritative Wowhead BBCode into its target nodes.

    The browser version calls ``WH.markup.printHtml(markup, target_id, ...)``.
    We parse the same source without executing JavaScript, render it to safe HTML,
    and replace the adjacent noscript fallback only after a text-completeness check.
    """
    if not BeautifulSoup:
        return False
    entities = _wowhead_fallback_entity_metadata(soup)
    screenshot_extensions = _wowhead_screenshot_extensions_by_id(soup)
    rendered_any = False
    article_root = _select_article_root(soup, source="wowhead")
    for script in list(soup.find_all("script")):
        script_text = script.string or script.get_text("", strip=False) or ""
        calls = extract_wowhead_print_html_calls(script_text)
        if not calls:
            continue
        fallback = script.find_next_sibling("noscript")
        script_rendered = False
        for call in calls:
            rendered = render_wowhead_bbcode(
                call.get("markup") or "",
                base_url=base_url,
                entities=entities,
                screenshot_extensions=screenshot_extensions,
            )
            if not rendered:
                continue
            fragment = BeautifulSoup(rendered, "html.parser")
            target = soup.find(id=call.get("target_id")) if call.get("target_id") else None
            if target is not None and article_root is not None and target not in article_root.descendants:
                target = None
            if target is not None:
                target.clear()
                for child in list(fragment.contents):
                    target.append(child.extract())
            elif fallback is not None and fallback.parent is not None:
                for child in list(fragment.contents):
                    fallback.insert_before(child.extract())
            elif script.parent is not None and article_root is not None and script in article_root.descendants:
                for child in list(fragment.contents):
                    script.insert_before(child.extract())
            elif article_root is not None:
                for child in list(fragment.contents):
                    article_root.append(child.extract())
            else:
                continue
            script_rendered = True
            rendered_any = True
        if script_rendered and fallback is not None and fallback.parent is not None:
            fallback.decompose()
    return rendered_any


def _restore_wowhead_markup_screenshots(soup, *, base_url: str):
    """Restore Wowhead ``[screenshot id=...]`` BBCode into visible images.

    Some Wowhead articles keep screenshots only in ``WH.markup.printHtml`` markup.
    The rendered ``.news-post-content`` fallback may contain only text, so removing
    scripts without first materializing screenshots makes Portal articles lose all
    images.  Build the canonical Wowhead screenshot URL and insert the images near
    matching text positions before scripts are stripped.
    """
    if not BeautifulSoup:
        return
    screenshot_extensions = _wowhead_screenshot_extensions_by_id(soup)
    screenshots = []
    seen = set()
    for script in soup.find_all("script"):
        text = script.string or script.get_text("", strip=False) or ""
        if "WH.markup.printHtml" not in text or "[screenshot" not in text:
            continue
        decoded = _decode_wowhead_markup_string(text)
        for match in re.finditer(r"\[screenshot\b([^\]]*)\](?:\[/screenshot\])?", decoded, re.I):
            attrs = match.group(1) or ""
            id_match = re.search(r"\bid\s*=\s*([0-9]+)", attrs, re.I)
            if not id_match:
                continue
            screenshot_id = id_match.group(1)
            if screenshot_id in seen:
                continue
            seen.add(screenshot_id)
            alt_match = re.search(r"\balt\s*=\s*\"(.*?)\"", attrs, re.I | re.S)
            width_match = re.search(r"\bwidth\s*=\s*([0-9]+)", attrs, re.I)
            screenshots.append({
                "id": screenshot_id,
                "alt": _decode_wowhead_markup_string(alt_match.group(1) if alt_match else ""),
                "width": width_match.group(1) if width_match else "",
                "ext": screenshot_extensions.get(screenshot_id, "png"),
            })
    if not screenshots:
        return

    root = _select_article_root(soup, source="wowhead") or soup
    factory = BeautifulSoup("", "html.parser")
    first_text_node = None
    for text_node in root.find_all(string=True):
        parent = getattr(text_node, "parent", None)
        if any(getattr(ancestor, "name", None) in {"script", "style"} for ancestor in getattr(text_node, "parents", [])):
            continue
        if _clean_inline_text(str(text_node)):
            first_text_node = text_node
            break

    previous = None
    for item in screenshots:
        image_url = "https://wow.zamimg.com/uploads/screenshots/normal/{}.{}".format(item["id"], item.get("ext") or "png")
        link = factory.new_tag("a", href=image_url)
        link["class"] = "article-image-link"
        img = factory.new_tag("img", src=image_url)
        if item.get("alt"):
            img["alt"] = item["alt"]
            link["title"] = item["alt"]
        if item.get("width"):
            img["width"] = item["width"]
        link.append(img)
        wrapper = factory.new_tag("div")
        wrapper["class"] = "wowhead-screenshot"
        wrapper.append(link)
        if previous is not None:
            previous.insert_after(wrapper)
        elif first_text_node is not None:
            first_text_node.insert_after(wrapper)
        else:
            root.append(wrapper)
        previous = wrapper


def _wowhead_screenshot_extensions_by_id(soup) -> Dict[str, str]:
    """Infer Wowhead screenshot file extensions from Gatherer imageType data."""
    result = {}
    image_type_to_ext = {"2": "jpg", "3": "png"}
    for script in soup.find_all("script"):
        text = script.string or script.get_text("", strip=False) or ""
        if "WH.Gatherer.addData(91" not in text or "imageType" not in text:
            continue
        for match in re.finditer(r'"(\d+)"\s*:\s*\{[^{}]*?"imageType"\s*:\s*(\d+)', text):
            ext = image_type_to_ext.get(match.group(2))
            if ext:
                result[match.group(1)] = ext
    return result


def _wowhead_item_metadata(soup) -> Dict[str, Dict[str, str]]:
    """Read the item names/icons embedded in Wowhead Gatherer data."""
    result = {}
    for script in soup.find_all("script"):
        text = script.string or script.get_text("", strip=False) or ""
        if "WH.Gatherer.addData(3" not in text:
            continue
        pattern = r'"(\d+)"\s*:\s*\{[^{}]*?"name_enus"\s*:\s*"((?:\\.|[^"\\])*)"[^{}]*?"icon"\s*:\s*"((?:\\.|[^"\\])*)"'
        for match in re.finditer(pattern, text, re.S):
            result[match.group(1)] = {
                "name": _decode_wowhead_markup_string(match.group(2)),
                "icon": _decode_wowhead_markup_string(match.group(3)),
            }
    return result


def _restore_wowhead_markup_item_table_cells(soup, *, base_url: str):
    """Restore item cards that Wowhead's noscript table leaves as empty cells."""
    if not BeautifulSoup:
        return
    item_cells = []
    centered_tables = False
    for script in soup.find_all("script"):
        text = script.string or script.get_text("", strip=False) or ""
        if "WH.markup.printHtml" not in text or "[item=" not in text:
            continue
        decoded = _decode_wowhead_markup_string(text)
        centered_tables = centered_tables or bool(re.search(r"\[center\]\s*\[table\]", decoded, re.I))
        for cell_match in re.finditer(r"\[td([^\]]*)\](.*?)\[/td\]", decoded, re.I | re.S):
            cell_attrs, cell_markup = cell_match.groups()
            item_match = re.search(r"\[item=(\d+)(?:\s+[^\]]*)?\]", cell_markup, re.I)
            if item_match:
                item_cells.append({
                    "id": item_match.group(1),
                    "attrs": cell_attrs or "",
                    "centered": bool(re.search(r"\[center\]", cell_markup, re.I)),
                })
    if not item_cells:
        return

    metadata = _wowhead_item_metadata(soup)
    factory = BeautifulSoup("", "html.parser")
    cell_index = 0
    restored_tables = []
    for cell in soup.find_all("td"):
        if cell_index >= len(item_cells):
            break
        if _clean_inline_text(cell.get_text(" ", strip=True)) or cell.find(["a", "img", "span"]):
            continue
        item_cell = item_cells[cell_index]
        item_id = item_cell["id"]
        cell_index += 1
        colspan_match = re.search(r"\bcolspan\s*=\s*[\"']?(\d+)", item_cell["attrs"], re.I)
        if colspan_match:
            cell["colspan"] = colspan_match.group(1)
        valign_match = re.search(r"\bvalign\s*=\s*[\"']?([\w-]+)", item_cell["attrs"], re.I)
        if valign_match:
            existing_style = str(cell.get("style") or "").strip().rstrip(";")
            vertical_style = "vertical-align: {}".format(valign_match.group(1))
            cell["style"] = "; ".join(part for part in (existing_style, vertical_style) if part)
        item = metadata.get(item_id) or {}
        name = item.get("name") or "Item {}".format(item_id)
        link = factory.new_tag("a", href=urljoin(base_url, "/item={}".format(item_id)))
        link["class"] = "wowhead-item-card"
        icon = item.get("icon")
        if icon:
            image = factory.new_tag("img", src="https://wow.zamimg.com/images/wow/icons/large/{}.jpg".format(icon))
            image["alt"] = name
            link.append(image)
        label = factory.new_tag("span")
        label.string = name
        link.append(label)
        if item_cell["centered"]:
            centered = factory.new_tag("div")
            centered["class"] = "wh-center"
            centered["style"] = "text-align: center"
            centered.append(link)
            cell.append(centered)
        else:
            cell.append(link)
        table = cell.find_parent("table")
        if table is not None and table not in restored_tables:
            restored_tables.append(table)

    # Wowhead's noscript fallback emits source line breaks as direct children of
    # table/tr. Those nodes are invalid HTML and browsers foster-parent them
    # before the table, producing large blank gaps. Remove only these structural
    # breaks from tables whose missing item cells were restored above.
    for table in restored_tables:
        for direct_break in list(table.find_all("br", recursive=False)):
            direct_break.decompose()
        for row in table.find_all("tr"):
            for direct_break in list(row.find_all("br", recursive=False)):
                direct_break.decompose()

    if centered_tables:
        for table in restored_tables:
            if table.parent and table.parent.get("class") and "wh-center" in table.parent.get("class", []):
                continue
            centered = factory.new_tag("div")
            centered["class"] = "wh-center"
            centered["style"] = "text-align: center"
            table.wrap(centered)


def _restore_wowhead_markup_spell_table_cells(soup, *, base_url: str):
    """Restore Wowhead BBCode spell tokens that noscript renders as empty table cells."""
    if not BeautifulSoup:
        return
    spell_cells = []
    for script in soup.find_all("script"):
        text = script.string or script.get_text("", strip=False) or ""
        if "WH.markup.printHtml" not in text or "[spell=" not in text:
            continue
        for cell_markup in re.findall(r"\[td[^\]]*\](.*?)\[\\?/td\]", text, re.I | re.S):
            spell_match = re.search(r"\[spell=(\d+)([^\]]*)\]", cell_markup, re.I)
            if not spell_match:
                continue
            spell_id = spell_match.group(1)
            attrs = _decode_wowhead_markup_string(spell_match.group(2) or "")
            name_match = re.search(r"\btempname=\"(.*?)\"", attrs, re.I)
            name = _decode_wowhead_markup_string(name_match.group(1) if name_match else "") or "Spell {}".format(spell_id)
            spell_cells.append((spell_id, name))
    if not spell_cells:
        return

    factory = BeautifulSoup("", "html.parser")
    cell_index = 0
    for cell in soup.find_all("td"):
        if cell_index >= len(spell_cells):
            break
        if _clean_inline_text(cell.get_text(" ", strip=True)) or cell.find(["a", "img", "span"]):
            continue
        spell_id, name = spell_cells[cell_index]
        cell_index += 1
        link = factory.new_tag("a", href=urljoin(base_url, "/spell={}".format(spell_id)))
        link.string = name
        cell.append(link)


def _restore_wowhead_markup_diff_marks(soup):
    """Restore Wowhead [del]/[ins] diff markers from markup scripts onto fallback text.

    Wowhead's fallback HTML often contains only flattened text like ``Value: -5-1``
    while the script markup records ``Value: [del]-5[/del][ins]-1[/ins]``. Match
    those changed value runs by text and replace only the affected text node with
    ``<del>``/``<ins>`` markup before scripts are removed.
    """
    if not BeautifulSoup:
        return
    segments = []
    for script in soup.find_all("script"):
        text = script.string or script.get_text("", strip=False) or ""
        if "WH.markup.printHtml" not in text or ("[del]" not in text and "[ins]" not in text):
            continue
        decoded = _decode_wowhead_markup_string(text)
        for match in re.finditer(r"([^\r\n\[]*?)(?:\[del\]([\s\S]*?)\[/del\])(?:\[ins\]([\s\S]*?)\[/ins\])?", decoded, re.I):
            old = _clean_inline_text(match.group(2) or "")
            new = _clean_inline_text(match.group(3) or "")
            prefix = _clean_inline_text(match.group(1) or "")
            if not old:
                continue
            # Keep a short label such as "Value:" or "Duration:" to avoid
            # matching unrelated occurrences of the same numeric value.
            label_match = re.search(r"([A-Za-z #/%()]+:\s*)$", prefix)
            label = _clean_inline_text(label_match.group(1) if label_match else "")
            plain = _clean_inline_text("{} {} {}".format(label, old, new))
            if len(plain.replace(" ", "")) < 2:
                continue
            segments.append({"label": label, "old": old, "new": new, "plain": plain})
    if not segments:
        return

    for seg in segments:
        wanted = seg["plain"].replace(" ", "")
        for text_node in list(soup.find_all(string=True)):
            parent = getattr(text_node, "parent", None)
            if not parent or parent.name in {"script", "style", "del", "ins"}:
                continue
            current = _clean_inline_text(str(text_node))
            if not current or wanted not in current.replace(" ", ""):
                continue
            restored = _build_diff_marked_fragment(str(text_node), seg)
            if not restored:
                continue
            fragment = BeautifulSoup(restored, "html.parser")
            text_node.replace_with(*list(fragment.contents))
            break


def _build_diff_marked_fragment(text: str, seg: Dict[str, str]) -> str:
    label = re.escape(seg.get("label") or "")
    old = re.escape(seg.get("old") or "")
    new = re.escape(seg.get("new") or "")
    if not old:
        return ""
    if seg.get("new"):
        pattern = r"({}\s*)({})(\s*)({})".format(label, old, new) if label else r"({})(\s*)({})".format(old, new)
    else:
        pattern = r"({}\s*)({})".format(label, old) if label else r"({})".format(old)
    match = re.search(pattern, text)
    if not match:
        return ""
    if seg.get("new"):
        if label:
            repl = "{}<del>{}</del>{}<ins>{}</ins>".format(html.escape(match.group(1)), html.escape(match.group(2)), html.escape(match.group(3)), html.escape(match.group(4)))
        else:
            repl = "<del>{}</del>{}<ins>{}</ins>".format(html.escape(match.group(1)), html.escape(match.group(2)), html.escape(match.group(3)))
    else:
        if label:
            repl = "{}<del>{}</del>".format(html.escape(match.group(1)), html.escape(match.group(2)))
        else:
            repl = "<del>{}</del>".format(html.escape(match.group(1)))
    return html.escape(text[:match.start()]) + repl + html.escape(text[match.end():])


def _decode_wowhead_markup_string(value: str) -> str:
    value = value or ""
    value = value.replace('\\"', '"').replace("\\/", "/")
    try:
        value = bytes(value, "utf-8").decode("unicode_escape")
    except Exception:
        pass
    return html.unescape(value).strip()


def _clean_discourse_lightbox_meta(root):
    """Normalize Discourse lightbox widgets to clean clickable article images."""
    for lightbox in root.select(".lightbox-wrapper"):
        img = lightbox.find("img")
        if not img:
            lightbox.decompose()
            continue
        link = lightbox.find("a", class_="lightbox") or lightbox.find("a")
        if link and link.get("href"):
            tag_factory = BeautifulSoup("", "html.parser")
            clean_link = tag_factory.new_tag("a", href=link.get("href"), title=link.get("title") or img.get("alt") or "")
            clean_link["class"] = "article-image-link"
            clean_link.append(img.extract())
            lightbox.replace_with(clean_link)
        else:
            lightbox.replace_with(img.extract())

    for image_grid in root.select(".d-image-grid"):
        for paragraph in list(image_grid.find_all("p", recursive=False)):
            if paragraph.find(["img", "a", "div"]) and not _clean_inline_text(paragraph.get_text(" ", strip=True)):
                paragraph.unwrap()
        if not image_grid.find("img"):
            image_grid.decompose()


def _sanitize_html_tag(tag, *, base_url: str):
    """Remove only executable behavior; preserve article markup and metadata."""

    url_attrs = {"href", "src", "poster", "data-source-src"}
    for attr in list(tag.attrs.keys()):
        attr_lower = attr.lower()
        value = tag.get(attr)
        if attr_lower.startswith("on"):
            del tag.attrs[attr]
            continue
        if attr_lower in {"srcdoc"}:
            del tag.attrs[attr]
            continue
        if attr_lower in url_attrs:
            raw_value = " ".join(value) if isinstance(value, list) else str(value or "")
            normalized = raw_value.strip()
            if not normalized:
                continue
            lowered = normalized.lower().lstrip()
            if lowered.startswith(("javascript:", "vbscript:")):
                del tag.attrs[attr]
                continue
            if lowered.startswith("data:") and not lowered.startswith(("data:image/", "data:video/", "data:audio/")):
                del tag.attrs[attr]
                continue
            # Keep data-source-src as source metadata for the image upload stage;
            # it may intentionally be relative and should not become the visible
            # URL until the uploader rewrites it.
            if attr_lower != "data-source-src":
                tag.attrs[attr] = urljoin(base_url, normalized)


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
    for tag in root.select(".advertisement, .ad, .heading-size, .heading-permalink"):
        tag.decompose()

    block_tags = {
        "address", "article", "aside", "blockquote", "div", "dl", "fieldset", "figcaption", "figure",
        "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main",
        "nav", "ol", "p", "pre", "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
    }

    noisy_boundary_tags = block_tags - {"table", "tbody", "tfoot", "thead", "tr", "td", "th"}

    # Wowhead uses breaks around lists and ordinary block wrappers as spacing
    # noise, but breaks around tables can be intentional article layout.
    for tag in root.find_all(["li", "td", "th", "blockquote", "div"]):
        if tag is root:
            continue
        _trim_edge_breaks(tag)

    changed = True
    while changed:
        changed = False
        for br in list(root.find_all("br")):
            prev_sig = _significant_sibling(br, previous=True)
            next_sig = _significant_sibling(br, previous=False)
            if (
                prev_sig is not None
                and getattr(prev_sig, "name", None) == "br"
                and getattr(br, "parent", None) is not root
            ):
                br.decompose()
                changed = True
                continue
            if prev_sig is not None and getattr(prev_sig, "name", None) in noisy_boundary_tags:
                br.decompose()
                changed = True
                continue
            if next_sig is not None and getattr(next_sig, "name", None) in noisy_boundary_tags:
                br.decompose()
                changed = True
                continue


def _trim_edge_breaks(tag):
    while True:
        first = _significant_child(tag, first=True)
        if first is None or getattr(first, "name", None) != "br":
            break
        first.decompose()
    while True:
        last = _significant_child(tag, first=False)
        if last is None or getattr(last, "name", None) != "br":
            break
        last.decompose()


def _significant_child(tag, *, first: bool):
    children = list(getattr(tag, "children", []) or [])
    if not first:
        children = list(reversed(children))
    for child in children:
        if getattr(child, "name", None) is None and not str(child).strip():
            continue
        return child
    return None


def _significant_sibling(node, *, previous: bool):
    sibling = node.previous_sibling if previous else node.next_sibling
    while sibling is not None:
        if getattr(sibling, "name", None) is None and not str(sibling).strip():
            sibling = sibling.previous_sibling if previous else sibling.next_sibling
            continue
        return sibling
    return None


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
