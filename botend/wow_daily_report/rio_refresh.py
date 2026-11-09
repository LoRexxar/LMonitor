"""Exact targeted insertion: existing prose and unrelated bytes stay intact."""
import re
from html.parser import HTMLParser

from bs4 import BeautifulSoup, Tag

from botend.wow_daily_report.presentation import render_daily_document
from botend.wow_daily_report.generator import _render_rio_items

_MARKERS = ('section', 'nav', 'style')


class _BalancedFragment(HTMLParser):
    """Validate original source boundaries; never accept parser auto-repair."""
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack = []
        self.invalid = False

    _VOID = frozenset({'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'})

    def _attributes(self, attrs):
        if len({name for name, _ in attrs}) != len(attrs):
            self.invalid = True

    def handle_starttag(self, tag, attrs):
        self._attributes(attrs)
        if tag not in self._VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self._attributes(attrs)
        if tag not in self._VOID:
            self.invalid = True

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.invalid = True
        else:
            self.stack.pop()

    def handle_comment(self, data):
        self.invalid = True

    def handle_decl(self, decl):
        self.invalid = True


def _validate_fragment(fragment, kind):
    inner = fragment[len('<!--daily-rio:' + kind + '-->'):-len('<!--/daily-rio:' + kind + '-->')]
    parser = _BalancedFragment()
    parser.feed(inner)
    parser.close()
    if parser.invalid or parser.stack:
        raise ValueError('日报数据观察组件边界不完整，未覆盖')
    dom = BeautifulSoup(inner, 'html.parser')
    roots = [n for n in dom.contents if not (isinstance(n, str) and not n.strip())]
    if len(roots) != 1 or not isinstance(roots[0], Tag):
        raise ValueError('日报数据观察组件跨越其他节点，未覆盖')
    root = roots[0]
    if root.find(['script', 'iframe', 'object', 'embed', 'base', 'link', 'meta', 'section', 'style', 'head', 'body']):
        raise ValueError('日报数据观察组件包含其他根节点，未覆盖')
    if kind == 'section':
        valid = (root.name == 'section' and root.get('data-section') == 'rio'
                 and set(root.get('class', [])) == {'daily-section', 'daily-rio'}
                 and root.get('aria-labelledby') == 'section-rio'
                 and not root.find(attrs={'data-section': True})
                 and len(root.select('h2#section-rio')) == 1)
    elif kind == 'nav':
        valid = (root.name == 'li' and root.get('data-section') == 'rio' and not root.find('li')
                 and len(root.find_all('a')) == 1 and root.find('a').get('href') == '#section-rio'
                 and not root.find(attrs={'data-section': True}))
    else:
        valid = root.name == 'style' and root.get('id') == 'daily-rio-style'
    if not valid:
        raise ValueError('日报数据观察组件身份不符，未覆盖')


def _part(document, kind):
    pattern = re.compile(r'<!--daily-rio:' + kind + r'-->.*?<!--/daily-rio:' + kind + r'-->', re.S)
    matches = list(pattern.finditer(document))
    if (len(matches) > 1 or document.count('<!--daily-rio:' + kind + '-->') != len(matches)
            or document.count('<!--/daily-rio:' + kind + '-->') != len(matches)):
        raise ValueError('日报存在重复的数据观察组件，未覆盖')
    for match in matches:
        _validate_fragment(match.group(), kind)
    return pattern, matches


def without_rio(document):
    for kind in _MARKERS:
        pattern, _ = _part(document, kind)
        document = pattern.sub('', document)
    return document


def merge_rio_section(document, day, section):
    """Change only tagged RIO fragments, not source articles or AI summaries."""
    if not section.get('items'):
        return document
    fresh = render_daily_document(day, [section], {'rio': _render_rio_items}, generated_at='')
    fragments = {}
    for kind in _MARKERS:
        _, matches = _part(fresh, kind)
        if len(matches) != 1:
            raise ValueError('数据观察组件生成不完整，未覆盖')
        fragments[kind] = matches[0].group()
    base = without_rio(document)
    # Reject an unrecognised pre-existing projection rather than duplicate it.
    if re.search(r'<(?:section|li)\b[^>]*\bdata-section=["\']rio["\']', base):
        raise ValueError('日报数据观察格式不兼容，未覆盖')
    anchors = {'section': '<div id="edition-content">\n',
               'nav': '<ul class="overview-list">', 'style': '</head>'}
    for kind, anchor in anchors.items():
        if base.count(anchor) != 1:
            raise ValueError('日报编辑式壳不兼容，未覆盖')
        replacement = fragments[kind] + anchor if kind == 'style' else anchor + fragments[kind]
        base = base.replace(anchor, replacement, 1)
    if without_rio(base) != without_rio(document):
        raise ValueError('非数据观察正文发生变化，未覆盖')
    return base
