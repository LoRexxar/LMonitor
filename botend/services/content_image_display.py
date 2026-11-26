"""Read-only display projection. Never fetch images or rewrite source documents."""
from functools import lru_cache
from html import escape, unescape
from html.parser import HTMLParser
import re

from botend.services.site_image_assets import exact_receipt_url, published_image_url as display_image_url


# Match every attribute, not just src: quoted alt/title can contain markup or
# strings like `src="..."`. Only use offsets in parser-confirmed start tags.
_ATTR = re.compile(r'''\s+([^\s=/>'"]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>'"`]+)))?''')
_MEDIA = re.compile(r'<(?:img|image|source|video|link|input)\b', re.I)
_CANDIDATE = re.compile(r'[\s,]*([^\s,]\S*)')


def _map_srcset(value, resolve):
    # Rebuild candidates rather than leaving a descriptor (e.g. "2x") in
    # URL position after an unpublished URL is dropped.
    candidates = []
    pos = 0
    while match := _CANDIDATE.match(value, pos):
        token = match[1]
        url = token.rstrip(',')
        pos = match.end()
        descriptor = ''
        if token == url:
            comma = value.find(',', pos)
            descriptor = value[pos:comma if comma >= 0 else len(value)].strip()
            pos = len(value) if comma < 0 else comma + 1
        mapped = resolve(url)
        if mapped and (not descriptor or re.fullmatch(r'(?:[1-9]\d*w|\d+(?:\.\d+)?x)', descriptor)):
            candidates.append(f'{mapped} {descriptor}' if descriptor else mapped)
    return ', '.join(candidates)


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
        if tag == 'img' or (tag == 'source' and self.picture):
            fields = ('src', 'srcset', 'data-src', 'data-srcset', 'data-original', 'data-lazy-src',
                      'data-lazy-srcset', 'data-source-src', 'data-fallback', 'data-original-src')
        elif tag == 'video':
            fields = ('poster',)
        elif tag == 'image':  # SVG <image>, not <a href>.
            fields = ('href', 'xlink:href')
        elif tag == 'input' and (dict(attrs).get('type') or '').lower() == 'image':
            fields = ('src',)
        elif tag == 'link':
            params = dict(attrs)
            rel = (params.get('rel') or '').lower().split()
            if not ({'icon', 'apple-touch-icon', 'mask-icon'} & set(rel) or
                    ('preload' in rel and (params.get('as') or '').lower() == 'image')):
                return
            fields = ('href', 'imagesrcset')
        else:
            return
        raw = self.get_starttag_text() or ''
        line, column = self.getpos()
        offset = self.lines[line - 1] + column
        pos = 1 + len(tag)
        first = len(self.slots)
        for name, _ in attrs:
            m = _ATTR.match(raw, pos)
            if not m or m[1].lower() != name:
                del self.slots[first:]
                self.slots.append((offset, offset + len(raw), None, None, None))
                break  # Drop malformed media tag, not the rest of the document.
            pos = m.end()
            if name not in fields or m.lastindex not in (2, 3, 4):
                continue
            a, b = m.span(m.lastindex)
            self.slots.append((offset + a, offset + b, name.endswith('srcset'), m.lastindex == 4, offset + m.start()))


def _image_slots(text):
    try:
        return _ImageSlots(text).slots
    except (ValueError, AssertionError):
        return None  # Unknown parse state: never return unprojected media.


# Only immutable public guide HTML uses this cache, never authenticated headers.
# Cache positions, not mapped URLs: a manifest update applies immediately.
_guide_slots = lru_cache(maxsize=12)(_image_slots)


def display_image_html(text, *, public_guide=False):
    if not isinstance(text, str) or not _MEDIA.search(text):
        return text
    resolve = lru_cache(maxsize=256)(display_image_url)
    parts, last = [], 0
    slots = (_guide_slots if public_guide and len(text) <= 1_200_000 else _image_slots)(text)
    if slots is None:
        return escape(text)
    for a, b, srcset, unquoted, attr_start in slots:
        if srcset is None:
            parts.append(text[last:a])
            last = b
            continue
        raw = text[a:b]
        value = unescape(raw)
        mapped = _map_srcset(value, resolve) if srcset else resolve(value)
        if not mapped:
            parts.append(text[last:attr_start])
            last = b + (not unquoted)
        elif mapped != value:
            replacement = escape(mapped, quote=True)
            parts.extend((text[last:a], '"' + replacement + '"' if unquoted else replacement))
            last = b
    return ''.join([*parts, text[last:]]) if parts else text


def display_icon_fields(payload, *, url_fields=(), icon_field=None):
    """Project named display fields in a JSON document, never edit canonical facts."""
    if isinstance(payload, list):
        return [display_icon_fields(v, url_fields=url_fields, icon_field=icon_field) for v in payload]
    if isinstance(payload, dict):
        out = {}
        for key, value in payload.items():
            if key in url_fields and isinstance(value, str):
                out[key] = display_image_url(value)
            elif key == icon_field and isinstance(value, str) and not re.fullmatch(r'[A-Za-z0-9_-]*', value):
                out[key] = display_image_url(value)
            else:
                out[key] = display_icon_fields(value, url_fields=url_fields, icon_field=icon_field)
        return out
    return payload


def display_article_blocks(blocks):
    result = []
    for block in blocks:
        if block.get('type') == 'html':
            block = {**block, **{k: display_image_html(block[k]) for k in ('html', 'original_html') if k in block}}
        elif block.get('type') == 'image':
            block = dict(block)
            source = block.pop('source_url', '')
            block['url'] = display_image_url(block.get('url')) or exact_receipt_url(source)
        result.append(block)
    return result
