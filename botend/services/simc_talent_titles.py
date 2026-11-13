"""Read-only SimC talent title projection; stored names and frozen inputs stay intact.

Only a verified Icy Veins system key and an exact code occurrence in the current
source Markdown may add provenance. No title/code catalogue or persistent cache
is maintained here: changing a guide takes effect on the next projection.
"""
import hashlib
import logging
import re
from collections import OrderedDict
from collections.abc import Mapping
from threading import RLock
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from django.db.models import Q
from django.db.models.functions import SHA256
from markdown_it import MarkdownIt

from botend.constants.hero_talents import (
    HERO_SUBTREE_NAME_ZH, normalize_hero_subtree_names_zh,
    normalize_hero_subtree_title_zh,
)
from botend.constants.wow import SPEC_IDENTITY_MAP, canonical_class_spec
from botend.guide_models import ClassGuide
from botend.models import SimcTalentString
from botend.services.class_guide_markdown import compile_markdown
from botend.services.simc_player_config import canonical_simc_spec_identity

logger = logging.getLogger(__name__)
_KEY = re.compile(r'guide:icy-veins:([1-9][0-9]*):([a-z][a-z0-9_]*):([0-9a-f]{64})')
_GENERIC_LABELS = {'', '天赋构筑', '天赋', 'talents', 'talent build', 'build'}
_BUILD_CHAPTERS = {'天赋与构筑', '天赋构筑', 'talents', 'talents and builds', 'talent builds'}
_MARKDOWN = MarkdownIt('commonmark', {'html': True})
_GUIDE_CACHE = OrderedDict()
_GUIDE_CACHE_LOCK = RLock()
_GUIDE_CACHE_LIMIT = 64


def _load_guides(guide_ids):
    """Hot reads transfer content fingerprints, not megabytes of Markdown.

    A miss reads content and its fingerprint in the same SQL statement. Never
    associate a second read's changed content with the first read's old digest.
    """
    guides, missing = {}, []
    query = ClassGuide.objects.filter(pk__in=guide_ids).annotate(title_source_hash=SHA256('source_markdown'))

    def key(row):
        return (row['id'], row['spec_id'], row['source_url'], row['title_source_hash'])

    for row in query.values('id', 'spec_id', 'source_url', 'title_source_hash'):
        if urlsplit(row['source_url']).hostname not in {'icy-veins.com', 'www.icy-veins.com'}:
            continue
        with _GUIDE_CACHE_LOCK:
            cached = _GUIDE_CACHE.get(key(row))
            if cached is not None:
                _GUIDE_CACHE.move_to_end(key(row))
                guides[row['id']] = cached
            else:
                missing.append(row['id'])
    for row in query.filter(pk__in=missing).values('id', 'spec_id', 'source_url', 'source_markdown', 'title_source_hash') if missing else ():
        try:
            if urlsplit(row['source_url']).hostname not in {'icy-veins.com', 'www.icy-veins.com'}:
                continue
            value = (SPEC_IDENTITY_MAP.get(row['spec_id']), _occurrences(row['source_markdown']))
            guides[row['id']] = value
            with _GUIDE_CACHE_LOCK:
                _GUIDE_CACHE[key(row)] = value
                _GUIDE_CACHE.move_to_end(key(row))
                while len(_GUIDE_CACHE) > _GUIDE_CACHE_LIMIT:
                    _GUIDE_CACHE.popitem(last=False)
        except Exception:
            logger.warning('Unable to project talent titles from guide %s', row['id'], exc_info=True)
    return guides


def _get(resource, key, default=None):
    return resource.get(key, default) if isinstance(resource, Mapping) else getattr(resource, key, default)


def _plain(value):
    """Use the Markdown parser for balanced URLs, then discard images/HTML."""
    soup = BeautifulSoup(_MARKDOWN.renderInline(str(value or '')), 'html.parser')
    for node in soup(['img', 'script', 'style']):
        node.decompose()
    return ' '.join(soup.get_text().split())


def _identity(resource):
    key = _get(resource, 'system_key')
    match = _KEY.fullmatch(key) if isinstance(key, str) else None
    code, spec = _get(resource, 'talent'), _get(resource, 'spec')
    if not match or not isinstance(code, str) or not code or match[2] != spec:
        return None
    if hashlib.sha256(code.encode('utf-8')).hexdigest() != match[3]:
        return None
    identity = canonical_class_spec(*canonical_simc_spec_identity(spec))
    if identity is None:
        return None
    return int(match[1]), identity, match[3]


def _hero_mentions(text):
    # The central normalizer includes legacy Chinese aliases as well as English.
    normalized = normalize_hero_subtree_title_zh(text)
    return {hero for hero in HERO_SUBTREE_NAME_ZH.values() if hero in normalized}


def _conflicts(text, heroes):
    return bool(heroes and (_hero_mentions(text) - set(heroes)))


def _occurrences(markdown):
    """Read Markdown headings/fences, compiling only actual talent components.

    The Markdown tokenizer owns fence boundaries (so code examples are not
    mistaken for headings). The existing component compiler still owns JSON,
    code validation and conversion; unrelated prose need not be HTML-rendered.
    """
    occurrences, headings = {}, []
    lines = markdown.splitlines()
    tokens = _MARKDOWN.parse(markdown)
    for index, token in enumerate(tokens):
        if token.type == 'heading_open':
            level = int(token.tag[1:])
            title = _plain(tokens[index + 1].content)
            headings = [(depth, text) for depth, text in headings if depth < level]
            if title:
                headings.append((level, title))
        elif token.type == 'fence' and token.info.strip() == 'json' and token.map:
            start, end = token.map
            header = start - 1
            while header >= 0 and not lines[header].strip():
                header -= 1
            if header < 0 or not re.match(r'^:::talents(?:\s|$)', lines[header]):
                continue
            # Include the original fence bytes rather than reserialize its data.
            source = '\n'.join(lines[header:end]) + '\n:::'
            for block in compile_markdown(source):
                code = block.get('data', {}).get('code')
                if block['type'] == 'talents' and code:
                    occurrences.setdefault(code, []).append({
                        'label': _plain(block.get('title')),
                        'path': tuple(title for _, title in headings),
                    })
    return occurrences


def _project(resource, identity, guide):
    old = _plain(_get(resource, 'name'))
    if not old.startswith('[Icy Veins]'):
        old = normalize_hero_subtree_title_zh(old)
    if not identity or not guide:
        return old
    guide_identity, occurrences = guide
    if guide_identity != identity[1]:
        return old
    matches = occurrences.get(_get(resource, 'talent'), ())
    if not matches:
        return old
    heroes = normalize_hero_subtree_names_zh(_get(resource, 'hero_talent_names', []))

    def usable_label(occurrence):
        label = occurrence['label']
        return (label.casefold() not in _GENERIC_LABELS
                and not _conflicts(label, heroes)
                and not _conflicts(' / '.join(occurrence['path']), heroes))

    # Explicit, nonconflicting labels outrank generic components. Within that
    # tier the build chapter wins; ties retain source order (e.g. AoE vs Delves).
    chosen = min(enumerate(matches), key=lambda item: (
        not usable_label(item[1]),
        not any(part.casefold() in _BUILD_CHAPTERS for part in item[1]['path']),
        item[0],
    ))[1]
    if usable_label(chosen):
        label = chosen['label']
    else:
        # Preserve old semantic text, not its duplicated source/hero/hash shell.
        parts = old.removeprefix('[Icy Veins]').strip().split(' · ')
        parts = [part for part in parts if part and part != identity[2][:8]
                 and part not in heroes and not _conflicts(part, heroes)]
        label = ' · '.join(parts) or '天赋构筑'
    # The chapter distinguishes provenance; repeated hero headings and generated
    # block numbers add no useful build meaning to a selector title.
    path = list(dict.fromkeys(part for part in chosen['path'] if not _conflicts(part, heroes)))[:1]
    pieces = list(dict.fromkeys([*heroes, label, *path, identity[2][:8]]))
    return '[Icy Veins] ' + ' · '.join(pieces)


def apply_talent_display_names(resources):
    """Add only ``display_name`` to ORM rows/dicts, returning the same container.

    Call with already-materialized rows to avoid deferred-field queries. Guides
    use one bounded fingerprint query; cold sources add one batch content query.
    """
    identities = [_identity(resource) for resource in resources]
    guide_ids = {identity[0] for identity in identities if identity}
    guides = {}
    if guide_ids:
        try:
            guides = _load_guides(guide_ids)
        except Exception:
            logger.warning('Unable to load talent title provenance', exc_info=True)
    for resource, identity in zip(resources, identities):
        title = _project(resource, identity, guides.get(identity[0]) if identity else None)
        if isinstance(resource, dict):
            resource['display_name'] = title
        else:
            resource.display_name = title
    return resources


def talent_display_name(resource):
    """Return one projected title without modifying even the input object."""
    row = {key: _get(resource, key) for key in ('name', 'spec', 'talent', 'hero_talent_names', 'system_key')}
    return apply_talent_display_names([row])[0]['display_name']


def frozen_talent_display_names(payloads, resource_ids=None):
    """Return titles for a page of frozen payloads without mutating that page.

    Two queries per bounded batch (current source identities, then guides), not
    per payload. IDs, when supplied, must align with payloads; ``None`` means an
    exact spec/code lookup. Only source keys are borrowed, never current names
    or hero metadata. The scalar wrapper below has the same identity fence.
    """
    payloads = list(payloads)
    ids = [None] * len(payloads) if resource_ids is None else list(resource_ids)
    if len(payloads) != len(ids):
        raise ValueError('resource_ids must align with payloads')
    if len(payloads) > 200:
        return [title for start in range(0, len(payloads), 200)
                for title in frozen_talent_display_names(payloads[start:start + 200], ids[start:start + 200])]
    rows = [{key: payload.get(key) if isinstance(payload, Mapping) else None
             for key in ('name', 'spec', 'talent', 'hero_talent_names')} for payload in payloads]
    valid = []
    filters = Q()
    for index, (row, resource_id) in enumerate(zip(rows, ids)):
        if not isinstance(row['spec'], str) or not row['spec'] or not isinstance(row['talent'], str) or not row['talent']:
            continue
        condition = Q(spec=row['spec'], talent=row['talent'])
        if resource_id is not None:
            if isinstance(resource_id, bool) or not re.fullmatch(r'[1-9][0-9]*', str(resource_id)):
                continue
            resource_id = int(resource_id)
            condition &= Q(pk=resource_id)
        filters |= condition
        valid.append((index, resource_id))
    if valid:
        try:
            sources = SimcTalentString.objects.filter(filters, system_key__startswith='guide:icy-veins:').order_by('pk').values(
                'id', 'spec', 'talent', 'system_key')
            by_identity, by_id = {}, {}
            for source in sources:
                if _identity(source):
                    by_identity.setdefault((source['spec'], source['talent']), source)
                    by_id[source['id']] = source
            for index, resource_id in valid:
                row = rows[index]
                source = by_id.get(resource_id) if resource_id is not None else by_identity.get((row['spec'], row['talent']))
                # Python exact equality also fences case-insensitive DB collations.
                if source and source['spec'] == row['spec'] and source['talent'] == row['talent']:
                    row['system_key'] = source['system_key']
        except Exception:
            logger.warning('Unable to load frozen talent title provenance', exc_info=True)
    return [row['display_name'] for row in apply_talent_display_names(rows)]


def frozen_talent_display_name(payload, resource_id=None):
    """Return one frozen title; use the plural helper for history/result lists."""
    return frozen_talent_display_names([payload], [resource_id])[0]
