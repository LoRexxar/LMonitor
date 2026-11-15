"""Conservative Icy-only layout projection, not a source Markdown repair.

Call only for Icy fragments after HTML sanitization. Number paragraphs are
absorbed into real h3-h6 nodes as ``data-source-number`` metadata. A caller
handling separate render blocks may pass the *immediately following* heading
Tag via ``following_heading`` and copy that metadata into its heading data.
No paragraph sequences, conditions or rotation actions are joined here.
"""
import re

from bs4 import Comment, Tag


_NUMBER = re.compile(r'[0-9]+(?:\.[0-9]+)*\.')
_LEVEL = re.compile(r'等级\s*[0-9]+\s*起\s*[:：]')
_HEADINGS = {'h3', 'h4', 'h5', 'h6'}
# Everything outside these presentation-only containers is semantic, even
# without text: img/hr/code/pre/ref/media, links, and unknown custom elements.
_EMPTY_WRAPPERS = {
    'li', 'tr', 'td', 'th', 'ul', 'ol', 'table', 'thead', 'tbody', 'tfoot',
    'div', 'section', 'p', 'span', 'strong', 'em', 'b', 'i', 'u', 's', 'br',
}
_NUMBER_WRAPPERS = {'p', 'span', 'strong', 'em', 'b', 'i', 'u', 's'}


def _has_identity(tag):
    return (any(key in tag.attrs for key in ('id', 'name', 'href', 'role', 'tabindex',
                                           'aria-label', 'aria-labelledby', 'title'))
            or any(key.startswith('data-reference') for key in tag.attrs)
            or 'data-guide-tooltip' in tag.attrs
            or 'guide-ref' in tag.get('class', [])
            or any(str(tag.get(key, '1')) != '1' for key in ('rowspan', 'colspan')))


def _truly_empty(node):
    if node.name == 'tr':
        table = node.find_parent('table')
        # A textless row can still be occupied by a preceding spanning cell.
        # Leave ambiguous span tables intact rather than reflow their geometry.
        if table and any(str(cell['rowspan']) != '1'
                         for cell in table.find_all(['td', 'th'], rowspan=True)):
            return False
    return (not node.get_text(strip=True)
            and all(tag.name in _EMPTY_WRAPPERS and not _has_identity(tag)
                    for tag in [node, *node.find_all(True)]))


def _meaningful(nodes):
    """Ignore only whitespace/comments, never intervening text or elements."""
    return next((node for node in nodes
                 if not isinstance(node, Comment)
                 and (isinstance(node, Tag) or str(node).strip())), None)


def _first_heading(node):
    # Source HTML can nest its heading inside a structural wrapper. Only walk
    # the first-child path: no scanning past prose, empty paragraphs or media.
    while isinstance(node, Tag) and node.name in {'div', 'section'}:
        node = _meaningful(node.children)
    if isinstance(node, Tag) and node.name in _HEADINGS and node.get_text(strip=True):
        return node
    return None


def _absorb_number(paragraph, heading):
    if (not isinstance(paragraph, Tag) or paragraph.name != 'p' or heading is None
            or paragraph.find_parent(['pre', 'code'])
            or heading.find_parent(['pre', 'code'])):
        return
    if any(tag.name not in _NUMBER_WRAPPERS or _has_identity(tag)
           for tag in [paragraph, *paragraph.find_all(True)]):
        return
    number = paragraph.get_text().strip()
    if not _NUMBER.fullmatch(number):
        # Only the evidenced, explicitly marked level label is eligible, and
        # only when that same applicability label survives on the heading.
        label = _meaningful(paragraph.children)
        if not (isinstance(label, Tag) and label.name == 'strong'
                and _LEVEL.fullmatch(label.get_text().strip())):
            return
        label_text = label.get_text().strip()
        if not number.startswith(label_text):
            return
        number = number[len(label_text):].strip()
        compact = lambda text: ''.join(text.split())
        if (not _NUMBER.fullmatch(number)
                or not compact(heading.get_text()).startswith(compact(label_text))):
            return
    if heading.get('data-source-number', number) != number:
        return
    heading['data-source-number'] = number
    paragraph.decompose()


def clean_icy_layout(soup, *, following_heading=None):
    """Mutate and return an already-sanitized Icy HTML soup, idempotently.

    Delete only truly empty li/tr nodes. Preserve textless semantic nodes and
    identities. Absorb number-only p nodes immediately before a real h3-h6,
    including nested HTML headings. Level-prefixed debris additionally needs
    the exact repeated level label. Ordinary numbers/prose are untouched.

    ``following_heading`` is an optional actual h3-h6 Tag in the next render
    block; only a top-level trailing p is considered at that explicit boundary.
    The caller owns source gating and must not pass a non-adjacent heading.
    """
    for paragraph in list(soup.find_all('p')):
        _absorb_number(paragraph, _first_heading(_meaningful(paragraph.next_siblings)))
    if following_heading is not None:
        last = _meaningful(reversed(soup.contents))
        heading = (following_heading if isinstance(following_heading, Tag)
                   and following_heading.name in _HEADINGS
                   and following_heading.get_text(strip=True) else None)
        _absorb_number(last, heading)
    # Work inside out so an outer item containing only empty nested items can
    # disappear too. Empty lists/tables themselves are left to existing layout.
    for node in reversed(soup.find_all(['li', 'tr'])):
        if not node.find_parent(['pre', 'code']) and _truly_empty(node):
            node.decompose()
    return soup
