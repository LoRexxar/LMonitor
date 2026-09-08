"""Public read model for collected NGA facts, independent of news/hot projections."""
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup, Comment
from django.core.paginator import Paginator
from django.db.models import Q

from django.shortcuts import get_object_or_404
from django.utils.html import escape
from django.utils.safestring import mark_safe

from botend.models import WowArticle
from botend.services.nga_facts_service import LEGACY_AUTHORS
from botend.services.wowhead_bbcode_renderer import render_wowhead_bbcode


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


def nga_source_url(value):
    """Accept only HTTPS NGA thread facts; never invent a tid from the local ID."""
    raw = str(value or '')
    if re.search(r'[\x00-\x20\x7f\\]', raw):
        return ''
    try:
        parts = urlsplit(raw)
        if (parts.scheme != 'https' or parts.hostname not in
                {'bbs.nga.cn', 'nga.178.com', 'ngabbs.com'} or
                parts.username is not None or parts.password is not None or
                parts.port not in (None, 443) or parts.path != '/read.php'):
            return ''
        tids = parse_qs(parts.query).get('tid', [])
        if len(tids) != 1 or not re.fullmatch(r'[1-9][0-9]*', tids[0]):
            return ''
        return f'https://{parts.hostname}/read.php?tid={tids[0]}'
    except ValueError:
        return ''


def nga_image_url(value):
    """Only dated NGA image attachments use the source's attachment-view host."""
    url = safe_url(value)
    if not url:
        return ''
    parts = urlsplit(url)
    # BBCode has already resolved ./mon_* against the forum by this point.
    if parts.netloc == 'bbs.nga.cn' and re.fullmatch(
            r'/(?:attachments/)?mon_\d{6}/\d{2}/[\w-]+\.(?:jpg|jpeg|png|gif|webp)(?:\.(?:medium|thumb(?:_[a-z]+)?)\.(?:jpg|jpeg|png|gif|webp))?',
            parts.path, re.I):
        path = parts.path.removeprefix('/attachments').lstrip('/')
        # Source page __ATTACH_BASE_VIEW_SEC = 'img.nga.cn'; verified by GET.
        return 'https://img.nga.cn/attachments/' + path + (
            '?' + parts.query if parts.query else '') + ('#' + parts.fragment if parts.fragment else '')
    return url


def readable_smileys(text):
    # Preserve the source's name, not an invented image URL or raw BBCode.
    return re.sub(r'\[s:(?:[\w-]+:)?([^\[\]<>:]+)\]', r'（表情：\1）', text, flags=re.I)


def render_main_post(content):
    """Legacy text stays escaped; HTML structures survive without active behavior.

    URL/attribute allowlists also cover entity-obfuscated schemes. No raw style,
    DOM IDs, custom elements or source-controlled event/data attributes survive.
    """
    raw = readable_smileys((content or '').strip())
    if not raw:
        return ''
    if re.search(r'\[(?:quote|b|i|u|s|del|ins|url|img|list|ul|ol|li|table|tr|td|th|h[1-6]|color|size|collapse|code|pre|center)(?:[=\] ])', raw, re.I):
        # Keep HTML intact across nested BBCode, then sanitize the combined result.
        html_tags = []
        def shield(match):
            html_tags.append(match.group(0))
            return '\ue000NGAHTML' + str(len(html_tags) - 1) + '\ue001'
        raw = raw.replace('\ue000', '').replace('\ue001', '')
        raw = re.sub(r'(?is)<[^>]+>', shield, raw)
        raw = re.sub(r'\[\*\]', '[li]', raw)
        raw = re.sub(r'\[collapse(?:=[^\]]*)?\]', '[quote]', raw, flags=re.I)
        raw = re.sub(r'\[/collapse\]', '[/quote]', raw, flags=re.I)
        raw = render_wowhead_bbcode(raw, base_url='https://bbs.nga.cn/')
        for index, tag in enumerate(html_tags):
            raw = raw.replace('\ue000NGAHTML' + str(index) + '\ue001', tag)
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
        # Stored collapsible HTML is presented in full, just like BBCode collapse.
        if tag.name in ('details', 'summary'):
            tag.name = 'div'
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
            url = nga_image_url(tag.get('src'))
            if url:
                attrs['src'] = url
            # srcset is intentionally rebuilt, never copied verbatim.
            candidates = []
            for candidate in str(tag.get('srcset') or '').split(','):
                fields = candidate.split()
                if fields and safe_url(fields[0]) and (len(fields) == 1 or
                        (len(fields) == 2 and re.fullmatch(r'\d+(?:\.\d+)?[wx]', fields[1]))):
                    candidates.append(' '.join([nga_image_url(fields[0]), *fields[1:]]))
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


def board_label(name):
    return name or '板块未记录'


def browse_posts(params):
    q = (params.get('q') or '').strip()[:120]
    board = (params.get('board') or '').strip()[:255]
    sort = params.get('sort') if params.get('sort') in ('newest', 'replies') else 'newest'
    base = public_posts()
    # Only observed forum IDs/names are presented as boards.
    available = list(base.exclude(nga_board_id='').exclude(nga_board_name='').order_by('nga_board_id').values_list('nga_board_id', 'nga_board_name').distinct()[:100])
    qs = base
    if board == 'unknown':
        qs = qs.filter(nga_board_id='')
    elif board:
        qs = qs.filter(nga_board_id=board)
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q) | Q(content__icontains=q))
    ordering = ('-reply_count', '-publish_time', '-id') if sort == 'replies' else ('-publish_time', '-id')
    rows = qs.order_by(*ordering).values('id', 'title', 'author', 'publish_time',
                                        'reply_count', 'nga_board_name', 'nga_replies_updated_at')
    page = Paginator(rows, 20).get_page((params.get('page') or '1')[:12])
    page.object_list = list(page.object_list)
    # Fetch full bodies only after pagination, in one query bounded to this batch.
    bodies = {row['id']: row for row in base.filter(
        id__in=[row['id'] for row in page.object_list]
    ).values('id', 'content', 'url')}
    for row in page.object_list:
        body = bodies.get(row['id'], {})
        row['body_html'] = render_main_post(body.get('content'))
        row['source_url'] = nga_source_url(body.get('url'))
        row['board_label'] = board_label(row['nga_board_name'])
        row['author_label'] = row['author'] if row['author'] and row['author'] not in LEGACY_AUTHORS else '作者未记录'
    return {'page': page, 'q': q, 'board': board, 'sort': sort,
            'boards': [{'key': key, 'label': label} for key, label in available]}


def post_detail(pk):
    post = get_object_or_404(public_posts().only('id', 'title', 'url', 'author', 'reply_count', 'publish_time', 'content', 'nga_board_name', 'nga_replies_updated_at'), pk=pk)
    return {'post': post, 'body_html': render_main_post(post.content),
            'board_label': board_label(post.nga_board_name), 'source_url': nga_source_url(post.url),
            'author_label': post.author if post.author and post.author not in LEGACY_AUTHORS else '作者未记录'}
