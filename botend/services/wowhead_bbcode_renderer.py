"""Render the safe, structural subset of Wowhead ``WH.markup`` BBCode.

Wowhead article pages keep their authoritative rich-text source in
``WH.markup.printHtml(...)`` calls.  This module reads (but never executes) those
calls, parses the BBCode into a small tolerant tree, and renders semantic HTML.
Unknown wrappers keep their visible children; unknown leaf tokens remain visible
as escaped source text so content is never silently discarded.
"""

from __future__ import annotations

import html
import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse


_MAX_MARKUP_LENGTH = 2_000_000
_MAX_TOKENS = 50_000
_MAX_DEPTH = 128

_CONTAINER_TAGS = {
    "b", "bold", "i", "italic", "u", "s", "strike", "del", "ins",
    "center", "left", "right", "quote", "code", "pre", "url", "color",
    "size", "img", "table", "thead", "tbody", "tfoot", "tr", "td", "th",
    "ul", "ol", "list", "li", "h1", "h2", "h3", "h4", "h5", "h6",
}
_VOID_TAGS = {"item", "spell", "npc", "object", "quest", "achievement", "screenshot", "br", "db"}
_STRUCTURAL_CONTEXTS = {"center", "table", "thead", "tbody", "tfoot", "tr", "ul", "ol", "list"}


@dataclass
class BBNode:
    kind: str
    text: str = ""
    name: str = ""
    value: str = ""
    attrs: Dict[str, str] = field(default_factory=dict)
    flags: set = field(default_factory=set)
    children: List["BBNode"] = field(default_factory=list)
    raw: str = ""


def extract_wowhead_print_html_calls(script_text: str) -> List[Dict[str, str]]:
    """Extract string arguments from ``WH.markup.printHtml`` without evaluating JS."""
    calls = []
    source = script_text or ""
    needle = "WH.markup.printHtml"
    cursor = 0
    while True:
        start = source.find(needle, cursor)
        if start < 0:
            break
        pos = start + len(needle)
        while pos < len(source) and source[pos].isspace():
            pos += 1
        if pos >= len(source) or source[pos] != "(":
            cursor = pos
            continue
        pos += 1
        while pos < len(source) and source[pos].isspace():
            pos += 1
        markup, pos = _scan_js_string(source, pos)
        if markup is None:
            cursor = max(pos, start + len(needle))
            continue
        target_id = ""
        while pos < len(source) and source[pos].isspace():
            pos += 1
        if pos < len(source) and source[pos] == ",":
            pos += 1
            while pos < len(source) and source[pos].isspace():
                pos += 1
            target_id, next_pos = _scan_js_string(source, pos)
            if target_id is not None:
                pos = next_pos
            else:
                target_id = ""
        if len(markup) <= _MAX_MARKUP_LENGTH:
            calls.append({"markup": markup, "target_id": target_id or ""})
        cursor = max(pos, start + len(needle))
    return calls


def _scan_js_string(source: str, pos: int) -> Tuple[Optional[str], int]:
    if pos >= len(source) or source[pos] not in {'"', "'"}:
        return None, pos + 1
    quote = source[pos]
    pos += 1
    raw = []
    while pos < len(source):
        char = source[pos]
        if char == quote:
            return _decode_js_string("".join(raw), quote), pos + 1
        if char == "\\" and pos + 1 < len(source):
            raw.append(char)
            raw.append(source[pos + 1])
            pos += 2
            continue
        raw.append(char)
        pos += 1
    return None, pos


def _decode_js_string(raw: str, quote: str) -> str:
    """Decode JSON-compatible JS escapes while preserving Unicode verbatim."""
    escaped = raw
    if quote == "'":
        escaped = escaped.replace('"', '\\"').replace("\\'", "'")
    try:
        return json.loads('"{}"'.format(escaped))
    except (ValueError, TypeError):
        replacements = {r"\/": "/", r"\r": "\r", r"\n": "\n", r"\t": "\t", r"\"": '"', r"\\": "\\"}
        for old, new in replacements.items():
            escaped = escaped.replace(old, new)
        return escaped


def parse_wowhead_bbcode(markup: str) -> BBNode:
    root = BBNode(kind="root")
    stack = [root]
    token_count = 0
    cursor = 0
    source = (markup or "")[:_MAX_MARKUP_LENGTH]
    for match in re.finditer(r"\[([^\]\r\n]{1,2048})\]", source):
        if token_count >= _MAX_TOKENS:
            break
        if match.start() > cursor:
            stack[-1].children.append(BBNode(kind="text", text=source[cursor:match.start()]))
        raw = match.group(0)
        body = match.group(1).strip()
        cursor = match.end()
        token_count += 1
        if body.startswith("/"):
            name = body[1:].strip().lower()
            closed = False
            for index in range(len(stack) - 1, 0, -1):
                if stack[index].name == name:
                    del stack[index:]
                    closed = True
                    break
            if not closed and name not in _VOID_TAGS:
                stack[-1].children.append(BBNode(kind="text", text=raw))
            continue
        node = _parse_open_tag(body, raw)
        if node is None:
            stack[-1].children.append(BBNode(kind="text", text=raw))
            continue
        stack[-1].children.append(node)
        # All non-void tags may be wrappers.  The renderer decides whether the
        # wrapper itself is supported; unknown wrappers still retain children.
        if node.name not in _VOID_TAGS and len(stack) < _MAX_DEPTH:
            stack.append(node)
    if cursor < len(source):
        stack[-1].children.append(BBNode(kind="text", text=source[cursor:]))
    return root


def _parse_open_tag(body: str, raw: str) -> Optional[BBNode]:
    match = re.match(r"^([A-Za-z][\w-]*)(?:=([^\s]+))?(?:\s+(.*))?$", body, re.S)
    if not match:
        return None
    name = match.group(1).lower()
    value = _strip_quotes(match.group(2) or "")
    attrs = {}
    flags = set()
    tail = match.group(3) or ""
    if tail:
        try:
            parts = shlex.split(tail, posix=True)
        except ValueError:
            parts = tail.split()
        for part in parts[:64]:
            if "=" in part:
                key, attr_value = part.split("=", 1)
                key = key.lower()
                if re.match(r"^[a-z][\w-]*$", key) and key not in attrs:
                    attrs[key] = _strip_quotes(attr_value)[:4096]
            elif re.match(r"^[A-Za-z][\w-]*$", part):
                flags.add(part.lower())
    return BBNode(kind="tag", name=name, value=value, attrs=attrs, flags=flags, raw=raw)


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def render_wowhead_bbcode(
    markup: str,
    *,
    base_url: str = "https://www.wowhead.com/",
    entities: Optional[Dict[Tuple[str, str], Dict[str, str]]] = None,
    screenshot_extensions: Optional[Dict[str, str]] = None,
) -> str:
    root = parse_wowhead_bbcode(markup)
    context = {
        "base_url": base_url,
        "entities": entities or {},
        "screenshot_extensions": screenshot_extensions or {},
    }
    return "".join(_render_node(child, context, []) for child in root.children)


def _render_node(node: BBNode, context: dict, ancestors: List[str]) -> str:
    if node.kind == "text":
        if ancestors and ancestors[-1] in _STRUCTURAL_CONTEXTS and not node.text.strip():
            return ""
        escaped = html.escape(node.text, quote=False)
        return re.sub(r"\r\n|\r|\n", "<br/>", escaped)

    name = node.name
    children = "".join(_render_node(child, context, ancestors + [name]) for child in node.children)
    simple = {
        "b": "strong", "bold": "strong", "i": "em", "italic": "em", "u": "u",
        "s": "s", "strike": "s", "del": "del", "ins": "ins", "code": "code",
        "pre": "pre", "table": "table", "thead": "thead", "tbody": "tbody",
        "tfoot": "tfoot", "tr": "tr", "ul": "ul", "ol": "ol", "li": "li",
        "h1": "h1", "h2": "h2", "h3": "h3", "h4": "h4", "h5": "h5", "h6": "h6",
    }
    if name in simple:
        tag = simple[name]
        return "<{0}>{1}</{0}>".format(tag, children)
    if name == "list":
        tag = "ol" if node.value in {"1", "decimal"} else "ul"
        return "<{0}>{1}</{0}>".format(tag, children)
    if name in {"center", "left", "right"}:
        return '<div class="wh-{0}" style="text-align: {0}">{1}</div>'.format(name, children)
    if name == "quote":
        return '<blockquote class="wh-quote">{}</blockquote>'.format(children)
    if name in {"td", "th"}:
        return _render_table_cell(node, children)
    if name == "url":
        href = node.value or node.attrs.get("url", "") or _plain_children(node)
        safe = _safe_url(href, context["base_url"])
        return '<a href="{}">{}</a>'.format(html.escape(safe, quote=True), children) if safe else children
    if name == "br":
        return "<br/>"
    if name == "img":
        src = _safe_url(_plain_children(node), context["base_url"])
        return '<img src="{}"/>'.format(html.escape(src, quote=True)) if src else ""
    if name == "screenshot":
        return _render_screenshot(node, context)
    if name in {"item", "spell", "npc", "object", "quest", "achievement"}:
        return _render_entity(node, context, ancestors)
    if name == "db":
        return children
    if name in {"color", "size"}:
        # Preserve semantic content without trusting arbitrary source CSS values.
        return children
    if children:
        return children
    return '<span class="wh-unsupported-token">{}</span>'.format(html.escape(node.raw, quote=False))


def _render_table_cell(node: BBNode, children: str) -> str:
    attributes = []
    for key in ("colspan", "rowspan"):
        value = node.attrs.get(key, "")
        if value.isdigit() and 1 <= int(value) <= 100:
            attributes.append('{}="{}"'.format(key, value))
    valign = node.attrs.get("valign", "").lower()
    if valign in {"top", "middle", "bottom", "baseline"}:
        attributes.append('style="vertical-align: {}"'.format(valign))
    suffix = " " + " ".join(attributes) if attributes else ""
    return "<{0}{1}>{2}</{0}>".format(node.name, suffix, children)


def _render_entity(node: BBNode, context: dict, ancestors: List[str]) -> str:
    entity_id = node.value.strip()
    if not entity_id.isdigit():
        return html.escape(node.raw, quote=False)
    kind = node.name
    metadata = context["entities"].get((kind, entity_id), {})
    name = metadata.get("name") or node.attrs.get("tempname") or "{} {}".format(kind.title(), entity_id)
    default_path = "/{}={}".format(kind, entity_id)
    href = _safe_url(metadata.get("url") or default_path, context["base_url"])
    is_card = "tooltip" in node.flags and "td" in ancestors
    classes = "wowhead-{}-card".format(kind) if is_card else "wowhead-entity-link"
    parts = []
    icon = metadata.get("icon") or ""
    if icon and is_card:
        if "://" not in icon:
            icon = "https://wow.zamimg.com/images/wow/icons/large/{}.jpg".format(icon)
        safe_icon = _safe_url(icon, context["base_url"])
        if safe_icon:
            parts.append('<img alt="{}" src="{}"/>'.format(html.escape(name, quote=True), html.escape(safe_icon, quote=True)))
    parts.append("<span>{}</span>".format(html.escape(name, quote=False)))
    return '<a class="{cls}" data-wh-entity="{kind}" data-wh-id="{id}" href="{href}">{body}</a>'.format(
        cls=classes, kind=kind, id=entity_id, href=html.escape(href, quote=True), body="".join(parts)
    )


def _render_screenshot(node: BBNode, context: dict) -> str:
    screenshot_id = (node.attrs.get("id") or node.value).strip()
    if not screenshot_id.isdigit():
        return html.escape(node.raw, quote=False)
    ext = context["screenshot_extensions"].get(screenshot_id, "png")
    if ext not in {"png", "jpg", "jpeg", "webp"}:
        ext = "png"
    src = "https://wow.zamimg.com/uploads/screenshots/normal/{}.{}".format(screenshot_id, ext)
    alt = node.attrs.get("alt", "")
    width = node.attrs.get("width", "")
    width_attr = ' width="{}"'.format(width) if width.isdigit() and int(width) <= 4096 else ""
    return '<div class="wowhead-screenshot"><a class="article-image-link" href="{src}"><img alt="{alt}" src="{src}"{width}/></a></div>'.format(
        src=html.escape(src, quote=True), alt=html.escape(alt, quote=True), width=width_attr
    )


def _plain_children(node: BBNode) -> str:
    result = []
    for child in node.children:
        if child.kind == "text":
            result.append(child.text)
        else:
            result.append(_plain_children(child))
    return "".join(result).strip()


def _safe_url(value: str, base_url: str) -> str:
    normalized = html.unescape((value or "").strip())
    if not normalized:
        return ""
    absolute = urljoin(base_url, normalized)
    scheme = urlparse(absolute).scheme.lower()
    return absolute if scheme in {"http", "https"} else ""
