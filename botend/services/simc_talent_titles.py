"""Read-only SimC talent title projection; stored names and frozen inputs stay intact.

Only a verified Icy Veins or Maxroll system key and an exact code occurrence in the current
source Markdown may add provenance. No title/code catalogue or persistent cache
is maintained here: changing a guide takes effect on the next projection.
"""
import hashlib
import json
import logging
import re
from collections import OrderedDict
from collections.abc import Mapping
from threading import RLock
from html import unescape
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from django.db.models import Q
from django.db.models.functions import SHA256
from markdown_it import MarkdownIt

from botend.constants.hero_talents import (
    HERO_SUBTREE_NAME_ZH, _LEGACY_HERO_SUBTREE_NAME_ZH, normalize_hero_subtree_names_zh,
)
from botend.constants.wow import SPEC_IDENTITY_MAP, canonical_class_spec
from botend.guide_models import ClassGuide
from botend.models import SimcTalentString
from botend.services.class_guide_talents import prepare_talent_data
from botend.services.class_guide_tags import TYPE_LABELS
from botend.services.simc_player_config import canonical_simc_spec_identity

logger = logging.getLogger(__name__)
_KEY = re.compile(r'guide:(icy-veins|maxroll):([1-9][0-9]*):([a-z][a-z0-9_]*):([0-9a-f]{64})')
_GENERIC_LABELS = {'', '天赋构筑', '天赋', 'talents', 'talent build', 'build'}
_BUILD_CHAPTERS = {'天赋与构筑', '天赋构筑', 'talents', 'talents and builds', 'talent builds'}
_MARKDOWN = MarkdownIt('commonmark', {'html': True})
_STRUCTURE = MarkdownIt('commonmark', {'html': True}).disable('inline')
_GUIDE_CACHE = OrderedDict()
_GUIDE_CACHE_LOCK = RLock()
_GUIDE_CACHE_LIMIT = 256
_SOURCE_HOSTS = {'icy-veins.com': 'icy-veins', 'www.icy-veins.com': 'icy-veins',
                 'maxroll.gg': 'maxroll', 'www.maxroll.gg': 'maxroll'}
# These are generic encounter/build terms, not a second game-name glossary.
_TITLE_PHRASES = (
    (re.compile(r'(?<![A-Za-z0-9_])(?:Mythic\+|M\+)(?![A-Za-z0-9_])', re.I), '大秘境'),
    (re.compile(r'(?<![A-Za-z0-9_])AoE(?![A-Za-z0-9_])', re.I), '范围伤害'),
)
_HERO_ALIASES = {**HERO_SUBTREE_NAME_ZH, **_LEGACY_HERO_SUBTREE_NAME_ZH,
                 **{name: name for name in HERO_SUBTREE_NAME_ZH.values()}}
_ENGLISH_HERO = '|'.join(re.escape(name) for name in sorted(HERO_SUBTREE_NAME_ZH, key=len, reverse=True))
_CHINESE_HERO = '|'.join(re.escape(name) for name in sorted(
    {*HERO_SUBTREE_NAME_ZH.values(), *_LEGACY_HERO_SUBTREE_NAME_ZH}, key=len, reverse=True))
# Chinese captions often join a hero and a build term without spaces. Accept
# those explicit suffixes, not arbitrary entity substrings (e.g. 陵寝哨兵).
_HERO_TOKEN = re.compile(
    rf'(?<![A-Za-z0-9_])(?:{_ENGLISH_HERO})(?![A-Za-z0-9_])'
    rf'|(?:(?<![\u4e00-\u9fff])|(?<=适用于))(?:{_CHINESE_HERO})'
    r'(?=$|[^\u4e00-\u9fff]|单体|单目标|多目标|团本|团队副本|大秘境|地下城|地下堡|天赋|构筑|构建)')


def _load_guides(guide_ids):
    """Hot reads transfer content fingerprints, not megabytes of Markdown.

    A miss reads content and its fingerprint in the same SQL statement. Never
    associate a second read's changed content with the first read's old digest.
    """
    guides, missing, prepared_codes = {}, [], {}
    query = ClassGuide.objects.filter(pk__in=guide_ids).annotate(
        title_source_hash=SHA256('source_markdown'), title_content_hash=SHA256('content_markdown'))

    def key(row):
        return (row['id'], row['spec_id'], row['source_url'], row['guide_type'], row['title_source_hash'], row['title_content_hash'])

    for row in query.values('id', 'spec_id', 'source_url', 'guide_type', 'title_source_hash', 'title_content_hash'):
        if urlsplit(row['source_url']).hostname not in _SOURCE_HOSTS:
            continue
        with _GUIDE_CACHE_LOCK:
            cached = _GUIDE_CACHE.get(key(row))
            if cached is not None:
                _GUIDE_CACHE.move_to_end(key(row))
                guides[row['id']] = cached
            else:
                missing.append(row['id'])
    for row in query.filter(pk__in=missing).values('id', 'spec_id', 'source_url', 'guide_type', 'source_markdown', 'content_markdown', 'title_source_hash', 'title_content_hash') if missing else ():
        try:
            if urlsplit(row['source_url']).hostname not in _SOURCE_HOSTS:
                continue
            source_tokens = _STRUCTURE.parse(row['source_markdown'])
            occurrences = _occurrences(row['source_markdown'], tokens=source_tokens, prepared_codes=prepared_codes)
            if row['content_markdown']:
                content_tokens = _STRUCTURE.parse(row['content_markdown'])
                _pair_content(occurrences, _occurrences(row['content_markdown'], tokens=content_tokens, prepared_codes=prepared_codes),
                              row['source_markdown'], row['content_markdown'],
                              source_tokens=source_tokens, content_tokens=content_tokens)
            source = _SOURCE_HOSTS[urlsplit(row['source_url']).hostname]
            value = (SPEC_IDENTITY_MAP.get(row['spec_id']), occurrences, source,
                     TYPE_LABELS.get(row['guide_type'], ''),
                     _maxroll_contexts(occurrences) if source == 'maxroll' else {})
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
    soup = BeautifulSoup(_MARKDOWN.renderInline(re.sub(r'\[\[[a-zA-Z_]+:[^\]]*\]\]', '', str(value or ''))), 'html.parser')
    for node in soup(['img', 'script', 'style']):
        node.decompose()
    return ' '.join(soup.get_text().split())


def _identity(resource):
    key = _get(resource, 'system_key')
    match = _KEY.fullmatch(key) if isinstance(key, str) else None
    code, spec = _get(resource, 'talent'), _get(resource, 'spec')
    if not match or not isinstance(code, str) or not code or match[3] != spec:
        return None
    if hashlib.sha256(code.encode('utf-8')).hexdigest() != match[4]:
        return None
    identity = canonical_class_spec(*canonical_simc_spec_identity(spec))
    if identity is None:
        return None
    return int(match[2]), identity, match[4], match[1]


def _hero_mentions(text):
    return {_HERO_ALIASES[match[0]] for match in _HERO_TOKEN.finditer(text)}


def _normalize_heroes(text):
    return _HERO_TOKEN.sub(lambda match: _HERO_ALIASES[match[0]], text)


def _conflicts(text, heroes):
    return bool(heroes and (_hero_mentions(text) - set(heroes)))


def _occurrences(markdown, *, tokens=None, prepared_codes=None):
    """Read Markdown headings/fences, compiling only actual talent components.

    The Markdown tokenizer owns fence boundaries (so code examples are not
    mistaken for headings). The article's shared talent adapter still owns code
    validation and conversion; title reads never render a decoded talent tree.
    """
    occurrences, headings = {}, []
    if prepared_codes is None:
        prepared_codes = {}
    lines = markdown.splitlines()
    if tokens is None:
        tokens = _STRUCTURE.parse(markdown)
    heading_titles = {index: _plain(tokens[index + 1].content)
                      for index, token in enumerate(tokens) if token.type == 'heading_open'}

    def talent_header(token):
        if token.type != 'fence' or token.info.strip() != 'json' or not token.map:
            return None
        header = token.map[0] - 1
        while header >= 0 and not lines[header].strip():
            header -= 1
        if header >= 0 and re.match(r'^:::talents(?:\s|$)', lines[header]):
            return header
        return None

    # Explanations and code components can live in separate repetitions of the
    # same numbered chapter. Match their section + scheme, never nearest h4.
    descriptions, chapter, scheme = {}, '', ''
    for index, token in enumerate(tokens):
        if talent_header(token) is not None:
            # A code-only repetition cannot borrow later talent discussion.
            scheme = ''
        if token.type != 'heading_open':
            continue
        level, title = int(token.tag[1:]), heading_titles[index]
        if level <= 2:
            chapter, scheme = title, ''
        elif level == 3:
            scheme = title if re.fullmatch(r'天赋方案\s+[0-9]+-[0-9]+', title) else ''
        elif level == 4 and scheme:
            if title:
                descriptions.setdefault((chapter, scheme), []).append(title)
            # Only the first h4 in each h3 block describes the scheme. Distinct
            # first descriptions in repeated blocks remain ambiguous below.
            scheme = ''
    for index, token in enumerate(tokens):
        if token.type == 'heading_open':
            level = int(token.tag[1:])
            title = heading_titles[index]
            headings = [(depth, text) for depth, text in headings if depth < level]
            if title:
                headings.append((level, title))
        elif token.type == 'fence' and token.info.strip() == 'json' and token.map:
            start, end = token.map
            header = talent_header(token)
            if header is None:
                continue
            # Match the article compiler's JSON fence grammar, then reuse its
            # exact code adapter without generating unrelated component HTML.
            if lines[start].strip() != '```json' or lines[end - 1].strip() != '```':
                continue
            data = json.loads(token.content)
            if not isinstance(data, dict):
                raise ValueError('组件参数必须是 JSON 对象')
            candidate = str(data.get('code') or data.get('source_code') or '')
            # The same exact import code recurs in source/translation and tabs.
            # Reuse only within this batch, never across source-tree updates.
            if candidate not in prepared_codes:
                prepared_codes[candidate] = prepare_talent_data(data).get('code')
            code = prepared_codes[candidate]
            if code:
                section = next((text for depth, text in headings if depth == 2), '')
                scheme_title = next((text for depth, text in headings if depth == 3), '')
                labels = set(descriptions.get((section, scheme_title), ()))
                occurrences.setdefault(code, []).append({
                    'description_label': next(iter(labels)) if len(labels) == 1 else '',
                    'label': _plain(lines[header][len(':::talents'):].strip()),
                    'path': tuple(title for _, title in headings),
                    'order': start,
                })
    return occurrences


def _link_translations(source_markdown, content_markdown, *, source_tokens=None, content_tokens=None, needed_terms=None):
    """Derive names from same-guide, same-href link text, never URL slugs."""
    def links(markdown, tokens, *, terms=None, hrefs=None):
        result = {}
        for token in (_STRUCTURE.parse(markdown) if tokens is None else tokens):
            text = unescape(token.content)
            if terms and not any(term in text.casefold().replace("'", '').replace('’', '') for term in terms):
                continue
            # The parser may unescape punctuation, encode Unicode/spaces or
            # normalize host case. Only skip plain ASCII, unencoded candidates;
            # ambiguous spellings must reach the real link parser.
            if (hrefs is not None and not any(href.casefold() in text.casefold() for href in hrefs)
                    and '\\' not in token.content and text.isascii()
                    and not any('%' in href for href in hrefs)):
                continue
            if token.type == 'inline' and ('](' in token.content or '<a ' in token.content):
                html = _MARKDOWN.renderInline(token.content)
            elif token.type == 'html_block' and '<a ' in token.content:
                html = token.content
            else:
                continue
            for link in BeautifulSoup(html, 'html.parser').find_all('a', href=True):
                text = ' '.join(link.get_text().split())
                if text:
                    result.setdefault(link['href'], set()).add(text)
        return result

    original = links(source_markdown, source_tokens, terms=needed_terms)
    if not original:
        return {}
    if needed_terms:
        # Retain every source label for the selected hrefs so filtering cannot
        # turn an ambiguous link identity into a trusted translation.
        original = links(source_markdown, source_tokens, hrefs=original)
    translated = links(content_markdown, content_tokens, hrefs=original)
    candidates = {}
    for href, names in original.items():
        # Apostrophes inside an attested English spelling may be omitted by a
        # caption. No other punctuation, suffix or URL-derived aliases qualify.
        keys = {re.sub(r"(?<=[A-Za-z])['’](?=[A-Za-z])", '', name).casefold() for name in names}
        if len(keys) != 1 or not all(re.fullmatch(r"[A-Za-z][A-Za-z0-9 '’&()-]*", name) for name in names):
            continue
        targets = translated.get(href, set())
        if targets:
            candidates.setdefault(next(iter(keys)), set()).update(targets)
    return {name: next(iter(targets)) for name, targets in candidates.items()
            if len(targets) == 1 and re.search(r'[\u4e00-\u9fff]', next(iter(targets)))
            and not re.search(r'[A-Za-z]', next(iter(targets)))}


def _translate_link_text(text, translations):
    if not translations or not re.search(r'[\u4e00-\u9fff]', text):
        return text
    # Match complete English phrases; normalizing only apostrophes cannot turn
    # an entity suffix or a URL slug into an attested name.
    return re.sub(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9]*(?:['’][A-Za-z]+)*(?:[ &()-]+[A-Za-z][A-Za-z0-9]*(?:['’][A-Za-z]+)*)*(?![A-Za-z0-9_])",
                  lambda match: translations.get(re.sub(r"(?<=[A-Za-z])['’](?=[A-Za-z])", '', match[0]).casefold(), match[0]), text)


def _pair_content(source, content, source_markdown='', content_markdown='', *, source_tokens=None, content_tokens=None):
    """Pair exact code occurrences only after the whole code sequence agrees."""
    def sequence(items):
        return [code for _, code in sorted(
            (item['order'], code) for code, matches in items.items() for item in matches)]
    if sequence(source) != sequence(content):
        return
    translations = {}
    for code, matches in source.items():
        for original, translated in zip(matches, content[code]):
            original['translated'] = translated
            pairs = [(original['label'], translated['label'])]
            if len(original['path']) == len(translated['path']):
                pairs.extend(zip(original['path'], translated['path']))
            for english, chinese in pairs:
                if re.search(r'[\u4e00-\u9fff]', chinese):
                    translations.setdefault(english, set()).add(chinese)
    trusted = {key: next(iter(values)) for key, values in translations.items() if len(values) == 1}
    link_translations = {}
    needed_terms = {term.casefold() for matches in content.values() for item in matches
                    for part in (item['label'], item['description_label'], *item['path'])
                    if re.search(r'[\u4e00-\u9fff]', part)
                    for term in re.findall('[A-Za-z]+', _without_heroes(part, HERO_SUBTREE_NAME_ZH.values()))}
    if source_markdown and content_markdown and needed_terms:
        link_translations = _link_translations(source_markdown, content_markdown,
                                               source_tokens=source_tokens, content_tokens=content_tokens,
                                               needed_terms=needed_terms)
    for matches in source.values():
        for item in matches:
            item['translations'] = trusted
            translated = item['translated']
            for key in ('label', 'description_label'):
                translated[key] = _translate_link_text(translated[key], link_translations)
            translated['path'] = tuple(_translate_link_text(part, link_translations) for part in translated['path'])


def _without_heroes(text, heroes):
    removed = False

    def replace(match):
        nonlocal removed
        hero = _HERO_ALIASES[match[0]]
        if hero in heroes:
            removed = True
            return ''
        return hero

    text = _HERO_TOKEN.sub(replace, text)
    for pattern, translation in _TITLE_PHRASES:
        text = pattern.sub(translation, text)
    if removed:
        text = text.strip(' ·-/：:—–')
        text = re.sub(r'[\s，,：:]*适用于\s*$', '', text).rstrip(' ·-/：:—–')
    return ' '.join(text.split())


def _maxroll_contexts(occurrences):
    """Resolve once per guide, independent of callers, resource IDs and hashes.

    Reference-only subheadings do not identify a hero. Prefer the build chapter
    and source headings over copy-pasted component captions. Number only distinct
    codes in their chosen source-block order, never repeated occurrences.
    """
    def context(occurrence, translated=False):
        if translated:
            occurrence = occurrence.get('translated', occurrence)
        parts = []
        for part in occurrence['path']:
            if part.casefold() in _BUILD_CHAPTERS:
                continue
            # Known hero names are redundant with frozen metadata; do not join
            # potentially conflicting source hero names onto that metadata.
            part = _without_heroes(part, HERO_SUBTREE_NAME_ZH.values()).strip(' ·-/—–')
            if part and part.casefold() not in _GENERIC_LABELS:
                parts.append(part)
        if parts:
            return ' / '.join(dict.fromkeys(parts))
        # A heading containing only a reference is intentionally unresolved;
        # its caption can be copied from the wrong hero/scenario. Use ordinal.
        if not occurrence['path'] and not _hero_mentions(occurrence['label']):
            label = occurrence['label']
            return label if label.casefold() not in _GENERIC_LABELS else ''
        return ''

    selected = {}
    for code, matches in occurrences.items():
        chosen = min(matches, key=lambda item: (
            not any(part.casefold() in _BUILD_CHAPTERS for part in item['path']),
            not bool(context(item)), item['order']))
        selected[code] = (chosen, context(chosen, translated=True))
    groups = {}
    for code, (chosen, label) in sorted(selected.items(), key=lambda item: item[1][0]['order']):
        groups.setdefault(label, []).append(code)
    result = {}
    for label, codes in groups.items():
        for index, code in enumerate(codes, 1):
            pieces = [label]
            if not label or len(codes) > 1:
                pieces.append(f'方案{index}')
            result[code] = ' · '.join(piece for piece in pieces if piece)
    return result


def _project(resource, identity, guide):
    old = _plain(_get(resource, 'name'))
    if '[Icy Veins]' in old or '[Maxroll]' in old:
        # Import fingerprints are technical identities, not user-facing titles.
        # Also covers old frozen/aggregate labels without a surviving source.
        old = re.sub(r'\s*·\s*[0-9a-f]{8}(?:[0-9a-f]{56})?\s*$', '', old)
        old = ' · '.join(part.strip() for part in old.split('·') if part.strip())
    if not old.startswith('[Icy Veins]'):
        old = _normalize_heroes(old)
    if not identity or not guide:
        return old
    guide_identity, occurrences, source, category, contexts = guide
    if guide_identity != identity[1] or source != identity[3]:
        return old
    matches = occurrences.get(_get(resource, 'talent'), ())
    if not matches:
        return old
    heroes = normalize_hero_subtree_names_zh(_get(resource, 'hero_talent_names', []))
    if source == 'maxroll':
        context = contexts[_get(resource, 'talent')]
        pieces = list(dict.fromkeys(piece for piece in [*heroes, category, context] if piece))
        return '[Maxroll] ' + ' · '.join(pieces)

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
    if usable_label(chosen) and not _conflicts(chosen.get('translated', chosen)['label'], heroes):
        label = chosen.get('translated', chosen)['label']
    else:
        # Preserve old semantic text, not its duplicated source/hero/hash shell.
        parts = old.removeprefix('[Icy Veins]').strip().split(' · ')
        parts = [part for part in parts if part and part != identity[2][:8]
                 and part not in heroes and not _conflicts(part, heroes)]
        label = ' · '.join(parts) or '天赋构筑'
        if 'translated' in chosen:
            # Exact, unambiguous bilingual evidence may translate a legacy label.
            translated = chosen['translations'].get(label)
            if translated and _conflicts(translated, heroes):
                translated = None
            candidates = [part for part in reversed(chosen['translated']['path'])
                          if re.search(r'[\u4e00-\u9fff]', part)
                          and part.casefold() not in _BUILD_CHAPTERS
                          and not re.fullmatch(r'天赋方案 [0-9-]+', part)
                          and not _conflicts(part, heroes)]
            description = chosen['translated'].get('description_label', '')
            if _conflicts(description, heroes):
                description = ''
            label = description or translated or next(iter(candidates), '天赋构筑')
    # The chapter distinguishes provenance; repeated hero headings and generated
    # block numbers add no useful build meaning to a selector title.
    path = list(dict.fromkeys(part for part in chosen.get('translated', chosen)['path']
                              if not _conflicts(part, heroes)))[:1]
    # Keep legacy source-only fallbacks intact; localized titles carry heroes once.
    if 'translated' in chosen:
        label = _without_heroes(label, heroes)
        path = [_without_heroes(part, heroes) for part in path]
    pieces = list(dict.fromkeys(piece for piece in [*heroes, label, *path] if piece))
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
    projected, verified = [], []
    for resource, identity in zip(resources, identities):
        guide = guides.get(identity[0]) if identity else None
        row = {'display_name': _project(resource, identity, guide),
               'spec': _get(resource, 'spec'), 'system_key': _get(resource, 'system_key')}
        projected.append(row)
        if (identity and guide and guide[0] == identity[1] and guide[2] == identity[3]
                and _get(resource, 'talent') in guide[1]):
            verified.append(row)
    try:
        from botend.services.simc_talent_title_terms import localize_imported_title_terms
        localize_imported_title_terms(verified)
    except Exception:
        logger.warning('Unable to resolve central talent title terms', exc_info=True)
    for resource, row in zip(resources, projected):
        if isinstance(resource, dict):
            resource['display_name'] = row['display_name']
        else:
            resource.display_name = row['display_name']
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
            sources = SimcTalentString.objects.filter(filters, Q(system_key__startswith='guide:icy-veins:') |
                                                       Q(system_key__startswith='guide:maxroll:')).order_by('pk').values(
                'id', 'spec', 'talent', 'system_key')
            by_identity, by_id = {}, {}
            for source in sources:
                identity = _identity(source)
                if identity:
                    by_identity.setdefault((source['spec'], source['talent']), source)
                    by_identity.setdefault((source['spec'], source['talent'], identity[3]), source)
                    by_id[source['id']] = source
            for index, resource_id in valid:
                row = rows[index]
                origin = next((key for prefix, key in (('[Icy Veins]', 'icy-veins'), ('[Maxroll]', 'maxroll'))
                               if str(row.get('name') or '').startswith(prefix)), None)
                lookup = (row['spec'], row['talent'], origin) if origin else (row['spec'], row['talent'])
                source = by_id.get(resource_id) if resource_id is not None else by_identity.get(lookup)
                # Python exact equality also fences case-insensitive DB collations.
                if (source and source['spec'] == row['spec'] and source['talent'] == row['talent']
                        and (not origin or _identity(source)[3] == origin)):
                    row['system_key'] = source['system_key']
        except Exception:
            logger.warning('Unable to load frozen talent title provenance', exc_info=True)
    return [row['display_name'] for row in apply_talent_display_names(rows)]


def frozen_talent_display_name(payload, resource_id=None):
    """Return one frozen title; use the plural helper for history/result lists."""
    return frozen_talent_display_names([payload], [resource_id])[0]
