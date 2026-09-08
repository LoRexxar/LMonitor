"""Public read model for collected NGA facts, independent of news/hot projections."""
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Comment
from django.core.paginator import Paginator
from django.db.models import Q
from django.db.models.functions import Coalesce, NullIf, Substr
from django.db.models import Value
from django.shortcuts import get_object_or_404
from django.utils.html import escape
from django.utils.safestring import mark_safe

from botend.models import WowArticle


# author is a collector classification here, NOT a forum username.
BOARDS = {'nga前瞻区': '前瞻区', 'nga水区': '水区'}
BODY_TAGS = set('p br div span strong b em i u s del ins blockquote pre code ul ol li dl dt dd '
                'table thead tbody tfoot tr th td caption colgroup col h1 h2 h3 h4 h5 h6 '
                'hr a img picture source figure figcaption details summary sup sub center font'.split())
DROP_TAGS = {'script', 'style', 'iframe', 'object', 'embed', 'svg', 'math', 'template', 'noscript', 'link', 'meta', 'base'}


def safe_url(value, base='https://bbs.nga.cn/'):
    raw = re.sub(r'[\x00-\x20\x7f]+', '', str(value or ''))
    if not raw:
        return ''
    try:
        resolved = urljoin(base, raw)
        parts = urlsplit(resolved)
        return resolved if parts.scheme in ('http', 'https') and parts.hostname else ''
    except ValueError:
        return ''


def render_main_post(content):
    """Legacy text stays escaped; HTML structures survive without active behavior.

    URL/attribute allowlists also cover entity-obfuscated schemes. No raw style,
    DOM IDs, custom elements or source-controlled event/data attributes survive.
    """
    raw = (content or '').strip()
    if not raw:
        return ''
    if not re.search(r'</?[a-zA-Z][^>]*>', raw):
        return mark_safe('<div class="nga-plain-text">' + str(escape(raw)) + '</div>')
    soup = BeautifulSoup(raw, 'html.parser')
    for comment in soup.find_all(string=lambda node: isinstance(node, Comment)):
        comment.extract()
    for tag in list(soup.find_all(True)):
        if tag.name is None:
            continue
        if tag.name in DROP_TAGS:
            tag.decompose()
            continue
        if tag.name not in BODY_TAGS:
            tag.unwrap()
            continue
        attrs = {}
        for key in ('title', 'alt', 'colspan', 'rowspan', 'width', 'height', 'align', 'valign', 'dir', 'lang'):
            if key in tag.attrs:
                attrs[key] = tag[key]
        if tag.name == 'a':
            url = safe_url(tag.get('href'))
            if url:
                attrs.update(href=url, rel='nofollow noopener noreferrer', target='_blank')
        if tag.name in ('img', 'source'):
            url = safe_url(tag.get('src'))
            if url:
                attrs['src'] = url
            # srcset is intentionally rebuilt, never copied verbatim.
            candidates = []
            for candidate in str(tag.get('srcset') or '').split(','):
                fields = candidate.split()
                if fields and safe_url(fields[0]) and (len(fields) == 1 or
                        (len(fields) == 2 and re.fullmatch(r'\d+(?:\.\d+)?[wx]', fields[1]))):
                    candidates.append(' '.join([safe_url(fields[0]), *fields[1:]]))
            if candidates:
                attrs['srcset'] = ', '.join(candidates)
            if tag.name == 'img':
                attrs.update(loading='lazy', referrerpolicy='no-referrer')
        tag.attrs = attrs
    result = str(soup).strip()
    has_image = any(image.get('src') or image.get('srcset') or image.get('alt')
                    for image in soup.find_all('img'))
    if not soup.get_text(strip=True) and not has_image:
        return ''
    return mark_safe(result)


def public_posts():
    # is_active is the existing publication boundary. No category, age or reply threshold.
    return WowArticle.objects.filter(source='nga', is_active=True)


def board_label(author):
    return BOARDS.get(author, '板块未记录')


def browse_posts(params):
    q = (params.get('q') or '').strip()[:120]
    board = (params.get('board') or '').strip()[:255]
    sort = params.get('sort') if params.get('sort') in ('newest', 'replies') else 'newest'
    base = public_posts()
    # Only actual collector classifications are presented as forum boards.
    available = list(base.filter(author__in=BOARDS).order_by('author').values_list('author', flat=True).distinct()[:len(BOARDS)])
    qs = base
    if board == 'unknown':
        qs = qs.exclude(author__in=BOARDS)
    elif board:
        qs = qs.filter(author=board) if board in BOARDS else qs.none()
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q) | Q(content__icontains=q))
    ordering = ('-reply_count', '-publish_time', '-id') if sort == 'replies' else ('-publish_time', '-id')
    # Substr happens in SQL: at most 20 short excerpts, never deferred-field N+1s.
    rows = qs.order_by(*ordering).annotate(excerpt=Substr(
        Coalesce(NullIf('description', Value('')), 'content', Value('')), 1, 600
    )).values('id', 'title', 'author', 'publish_time', 'reply_count', 'excerpt')
    page = Paginator(rows, 20).get_page((params.get('page') or '1')[:12])
    for row in page.object_list:
        soup = BeautifulSoup(row.pop('excerpt') or '', 'html.parser')
        for tag in soup.find_all(['script', 'style']):
            tag.decompose()
        text = soup.get_text(' ', strip=True)
        row['summary'] = text[:220] + ('…' if len(text) > 220 else '')
        row['board_label'] = board_label(row['author'])
    return {'page': page, 'q': q, 'board': board, 'sort': sort,
            'boards': [{'key': key, 'label': BOARDS[key]} for key in available]}


def post_detail(pk):
    post = get_object_or_404(public_posts().only('id', 'title', 'url', 'author', 'reply_count', 'publish_time', 'content'), pk=pk)
    return {'post': post, 'body_html': render_main_post(post.content),
            'board_label': board_label(post.author), 'source_url': safe_url(post.url)}
