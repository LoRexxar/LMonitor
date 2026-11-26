"""Read-only display projection. Never fetch images or rewrite source documents."""
from functools import lru_cache
from html import escape, unescape
from html.parser import HTMLParser
import re

from botend.services.site_image_assets import published_image_url as display_image_url


# Match every attribute, not just src: quoted alt/title can contain markup or
# strings like `src="..."`. Only use offsets in parser-confirmed start tags.
_ATTR = re.compile(r'''\s+([^\s=/>'"]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>'"`]+)))?''')
_MEDIA = re.compile(r'<(?:img|source|video)\b', re.I)
_CANDIDATE = re.compile(r'[\s,]*([^\s,]\S*)')


def _map_srcset(value, resolve):
    # A URL token may contain commas (including data: URLs); only its trailing
    # commas or a comma after the descriptor end the candidate. Keep spacing.
    pos = last = 0
    parts = []
    while match := _CANDIDATE.match(value, pos):
        token = match[1]
        url = token.rstrip(',')
        mapped = resolve(url)
        if mapped != url:
            start = match.start(1)
            parts.extend((value[last:start], mapped))
            last = start + len(url)
        pos = match.end()
        if token == url:
            comma = value.find(',', pos)
            if comma < 0:
                break
            pos = comma + 1
    return ''.join([*parts, value[last:]]) if parts else value


class _ImageSlots(HTMLParser):
    CDATA_CONTENT_ELEMENTS = (*HTMLParser.CDATA_CONTENT_ELEMENTS, 'textarea', 'title', 'xmp', 'iframe')

    def __init__(self, text):
        super().__init__(convert_charrefs=False)
        self.lines = [0, *(m.end() for m in re.finditer('\n', text))]
        self.slots = []
        self.picture = False
        self.feed(text)
        self.close()

    def handle_endtag(self, tag):
        if tag == 'picture':
            self.picture = False

    def handle_starttag(self, tag, attrs):
        if tag == 'picture':
            self.picture = True
        if tag not in ('img', 'video') and not (tag == 'source' and self.picture):
            return
        fields = ('poster',) if tag == 'video' else ('src', 'srcset', 'data-src', 'data-srcset')
        raw = self.get_starttag_text() or ''
        line, column = self.getpos()
        offset = self.lines[line - 1] + column
        pos = 1 + len(tag)
        for name, _ in attrs:
            m = _ATTR.match(raw, pos)
            if not m or m[1].lower() != name:
                break  # malformed tag; do not guess where later attributes start
            pos = m.end()
            if name not in fields or m.lastindex not in (2, 3, 4):
                continue
            a, b = m.span(m.lastindex)
            self.slots.append((offset + a, offset + b, name.endswith('srcset'), m.lastindex == 4))


def _image_slots(text):
    try:
        return _ImageSlots(text).slots
    except (ValueError, AssertionError):
        return ()  # tolerate malformed legacy markup without regenerating it


# Only immutable public guide HTML uses this cache, never authenticated headers.
# Cache positions, not mapped URLs: a manifest update applies immediately.
_guide_slots = lru_cache(maxsize=12)(_image_slots)


def display_image_html(text, *, public_guide=False):
    if not isinstance(text, str) or not _MEDIA.search(text):
        return text
    resolve = lru_cache(maxsize=256)(display_image_url)
    parts, last = [], 0
    slots = (_guide_slots if public_guide and len(text) <= 1_200_000 else _image_slots)(text)
    def replace_url(value):
        url = unescape(value)
        mapped = resolve(url)
        return escape(mapped, quote=True) if mapped != url else value

    for a, b, srcset, unquoted in slots:
        raw = text[a:b]
        if srcset:
            mapped = _map_srcset(raw, replace_url)
        else:
            mapped = replace_url(raw)
        if mapped != raw:
            parts.extend((text[last:a], '"' + mapped + '"' if unquoted else mapped))
            last = b
    return ''.join([*parts, text[last:]]) if parts else text


def display_article_blocks(blocks):
    result = []
    for block in blocks:
        if block.get('type') == 'html':
            if isinstance(block.get('html'), str):
                block = {**block, 'html': display_image_html(block['html'])}
        elif block.get('type') == 'image' and 'url' in block:
            mapped = display_image_url(block['url'])
            if mapped != block['url']:
                block = {'source_url': block['url'], **block, 'url': mapped}
        result.append(block)
    return result
