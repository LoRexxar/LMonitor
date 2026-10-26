"""攻略链接投影：仅来源说明可以跳转原文，章节链接必须有真实目标。"""
from collections import Counter, defaultdict
from urllib.parse import unquote, urljoin, urlparse

from django.urls import Resolver404, resolve
from django.utils.html import strip_tags
from django.utils.text import slugify

from botend.services.class_guide_content import walk_blocks


def _page(url):
    parsed = urlparse(url)
    return parsed._replace(fragment='', query='').geturl().rstrip('/')


class GuideLinkPolicy:
    def __init__(self, blocks, guide):
        rows = list(walk_blocks(blocks))
        counts = Counter(row.get('id') for row in rows if row['type'] != 'separator')
        self.ids = {key for key, count in counts.items() if key and count == 1}
        self.aliases = defaultdict(set)
        self.chapters, self.sections = {}, {}
        self.source = guide.source_url or ''
        self.local_path = f'/portal/class-guides/{getattr(guide, "pk", "")}/'
        self.originals = {self.source} if self.source else set()
        titles = defaultdict(list)
        current = None
        for row in rows:
            if row['type'] == 'heading':
                title = strip_tags(row.get('title', '')).strip()
                if row.get('data', {}).get('level') == 2:
                    current = row['id']
                    titles[title].append(current)
                for alias in (title, slugify(title, allow_unicode=True)):
                    if alias:
                        self.aliases[alias].add(row['id'])
            self.sections[row.get('id')] = current
        payload = getattr(guide, 'source_payload', None) or {}
        for chapter in payload.get('chapters', []):
            url = chapter.get('url') or chapter.get('source_url', '')
            if not url:
                continue
            self.originals.add(url)
            matches = titles.get(chapter.get('title'), [])
            if len(matches) == 1:
                self.chapters[_page(url)] = matches[0]

    def target(self, fragment, section=None):
        fragment = unquote(fragment)
        if fragment in self.ids:
            target = fragment
        else:
            matches = self.aliases.get(fragment, set()) & self.ids
            if section:
                matches = {key for key in matches if self.sections.get(key) == section}
            target = next(iter(matches)) if len(matches) == 1 else None
        if target and (not section or self.sections.get(target) == section):
            return '#' + target
        return None

    def href(self, value, *, source_note=False, section=None):
        value = str(value or '').strip()
        if not value or '\\' in value:
            return None
        parsed = urlparse(value)
        if parsed.username or parsed.password:
            return None
        if source_note and value in self.originals and parsed.scheme == 'https':
            return value
        if value.startswith('#'):
            return self.target(parsed.fragment)
        if not parsed.scheme and not parsed.netloc and parsed.path == self.local_path:
            return self.target(parsed.fragment) if parsed.fragment else value
        absolute = urljoin(self.source, value)
        chapter = self.chapters.get(_page(absolute))
        if chapter:
            fragment = urlparse(absolute).fragment
            return self.target(fragment, chapter) if fragment else '#' + chapter
        if parsed.scheme or parsed.netloc or not value.startswith('/'):
            return None
        # 源站遗留的 /wow/ 路径不属于本站路由，不能误当成站内链接。
        try:
            resolve(parsed.path)
        except Resolver404:
            return None
        return value

    def clean(self, soup, *, source_note=False, section=None):
        for anchor in list(soup.find_all('a')):
            href = self.href(anchor.get('href'), source_note=source_note, section=section)
            if href:
                anchor['href'] = href
                if href.startswith('#'):
                    anchor.attrs.pop('target', None)
            else:
                anchor.unwrap()
