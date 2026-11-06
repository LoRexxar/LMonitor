"""Read-only, source-verified Journal terminology for a single news article.

No downloader, new fact store or cross-branch/name-based spell join lives here.
Journal relationships retain their release build; SpellName pairs use the exact
active retail source directory, and keep that separate name build in evidence.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
import csv
import hashlib
import io
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse

from django.conf import settings

from utils.log import logger


_BUILD = re.compile(r'\d+\.\d+\.\d+\.\d+\Z')
_BOUNDARY = r'(?<![A-Za-z0-9]){}(?![A-Za-z0-9])'
_REFERENCES = re.compile(r'(?:\|Hspell:|\$@spell(?:name|desc|aura)|\$(?=\d))(\d+)', re.I)


@dataclass
class JournalTerms:
    pairs: list = field(default_factory=list)
    trusted: set = field(default_factory=set)
    controlled: set = field(default_factory=set)
    blocked: set = field(default_factory=set)
    evidence: dict = field(default_factory=dict)
    suppress_fallback: bool = False


def _aliases(name, source):
    """Only source-present The/apostrophe variants and the attested epithet comma."""
    candidates = {name}
    if name.startswith('The '):
        candidates.add(name[4:])
    for value in tuple(candidates):
        candidates.update((value.replace("'", '’'), value.replace('’', "'")))
    # Add punctuation only to an official "X the Epithet" name; never shorten it.
    for value in tuple(candidates):
        if re.fullmatch(r'.+ the [A-Z].+', value):
            candidates.add(value.replace(' the ', ', the ', 1))
    return {value for value in candidates if value and re.search(
        _BOUNDARY.format(re.escape(value)), source, re.I)}


def _signature(path):
    stat = path.stat()
    if stat.st_size > 256 * 1024 * 1024:
        raise ValueError('oversized terminology source')
    return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _proof(metadata, table, build, locale, *, branch):
    if not isinstance(metadata, dict) or not re.fullmatch(r'[a-f0-9]{64}', str(metadata.get('sha256', ''))):
        raise ValueError('missing source hash')
    identity = metadata.get('identity')
    if identity is not None and identity != dict(branch=branch, build=build, table=table, locale=locale):
        raise ValueError('source identity mismatch')
    # Old published release tables have URL/hash, not a sidecar identity.
    url = urlparse(metadata.get('url', ''))
    query = parse_qs(url.query)
    valid_url = (url.scheme == 'https' and url.netloc == 'wago.tools'
                 and url.path == f'/db2/{table}/csv'
                 and query == {'build': [build], 'locale': [locale]})
    if not valid_url and identity is None:
        raise ValueError('unproven source coordinates')
    if metadata.get('url') and not valid_url:
        raise ValueError('source URL disagrees with identity')
    return metadata['sha256']


@lru_cache(maxsize=48)
def _csv_rows(path_string, signature, digest, fields, filter_field, selected):
    """Cache bounded projections, never the complete current SpellName table."""
    path = Path(path_string)
    wanted = set(selected) if selected is not None else None
    result, seen = {}, set()
    with path.open('rb') as raw:
        hasher = hashlib.sha256()
        for chunk in iter(lambda: raw.read(1024 * 1024), b''):
            hasher.update(chunk)
        if hasher.hexdigest() != digest:
            raise ValueError('source hash mismatch')
        raw.seek(0)
        reader = csv.DictReader(io.TextIOWrapper(raw, encoding='utf-8-sig', newline=''))
        if not {'ID', filter_field, *fields}.issubset(reader.fieldnames or ()):
            raise ValueError('source schema mismatch')
        for count, row in enumerate(reader, 1):
            if count > 2000000 or None in row or None in row.values():
                raise ValueError('invalid source rows')
            identity = int(row['ID'])
            if identity <= 0 or identity in seen:
                raise ValueError('invalid/duplicate source ID')
            seen.add(identity)
            if wanted is None or int(row[filter_field]) in wanted:
                if len(result) >= 10000:
                    raise ValueError('terminology scope too large')
                result[identity] = {name: row[name] for name in fields}
    if _signature(path) != signature:
        raise ValueError('source changed during read')
    return result


def _read(path, metadata, table, build, locale, fields, *, branch, filter_field='ID', selected=None):
    digest = _proof(metadata, table, build, locale, branch=branch)
    return _csv_rows(str(path), _signature(path), digest, tuple(fields), filter_field,
                     tuple(sorted(selected)) if selected is not None else None)


def _journal_table(directory, manifest, table, build, locale, fields, **selection):
    key = table + ('.enUS' if locale == 'enUS' else '')
    path = directory / f'{key}.csv'
    try:
        metadata = manifest.get('tables', {}).get(key)
        if metadata is None:
            metadata = json.loads(path.with_suffix('.csv.manifest.json').read_text())
            # Supplemental files require an explicit branch, not just a URL.
            if metadata.get('identity') != dict(branch='wow', build=build, locale=locale, table=table):
                raise ValueError('supplemental identity mismatch')
        return _read(path, metadata, table, build, locale, fields, branch='wow', **selection)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        logger.debug('[NewsJournalGlossary] unavailable %s: %s', key, exc)
        return {}


def _spell_pairs(version, needed):
    if not version or version.branch != 'retail' or not needed or not _BUILD.fullmatch(version.current_build or '') or not version.source_dir:
        return {}, {}
    source = Path(version.source_dir)
    try:
        manifest = json.loads((source / 'manifest.json').read_text())
        rows = {}
        for locale in ('enUS', 'zhCN'):
            key = 'SpellName' + ('.enUS' if locale == 'enUS' else '')
            try:
                rows[locale] = _read(source / f'SpellName_{locale}.csv', manifest[key], 'SpellName',
                                     version.current_build, locale, ('Name_lang',), branch=version.branch, selected=needed)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                logger.debug('[NewsJournalGlossary] unavailable current SpellName %s: %s', locale, exc)
                rows[locale] = {}
        return rows['enUS'], rows['zhCN']
    except (OSError, ValueError, TypeError, KeyError) as exc:
        logger.debug('[NewsJournalGlossary] unavailable current SpellName pair: %s', exc)
        return {}, {}


def _ids(text):
    return {int(value) for value in _REFERENCES.findall(str(text or '')) if int(value) > 0}


def read_news_journal_terms(source_text):
    """Return only terms in a verified raid/boss scope; missing facts stay absent."""
    empty = JournalTerms()
    if not source_text or re.search(r'\b(?:ptr|beta)\b|public test realm', source_text, re.I):
        return empty
    from botend.journal_models import JournalEncounter, JournalInstance, JournalState
    from botend.models import WowTalentVersion

    state = JournalState.objects.select_related('active_release').filter(key='wow-zhCN').first()
    release = state.active_release if state else None
    if not release or release.status != 'completed' or release.locale != 'zhCN':
        return empty
    manifest = release.manifest
    build = manifest.get('retail_build', '')
    if not _BUILD.fullmatch(build):
        return empty
    directory = Path(settings.BASE_DIR) / '.cache/adventure-journal' / build / 'zhCN'
    # Retail raw tables cannot attest a PTR overlay or its display override.
    overlays = set(manifest.get('ptr_overlays', {}))
    instances = list(JournalInstance.objects.filter(release=release, kind='raid').values('id', 'journal_id', 'name'))
    instances = [row for row in instances if str(row['journal_id']) not in overlays]
    if not instances:
        return empty
    table = lambda name, locale, fields, **kw: _journal_table(directory, manifest, name, build, locale, fields, **kw)
    instance_en = table('JournalInstance', 'enUS', ('Name_lang',))
    instance_zh = table('JournalInstance', 'zhCN', ('Name_lang',))
    bosses = list(JournalEncounter.objects.filter(instance_id__in=[row['id'] for row in instances]).values(
        'id', 'journal_id', 'instance_id', 'name', 'payload__dungeon_encounter_id'))
    boss_en = table('JournalEncounter', 'enUS', ('Name_lang', 'JournalInstanceID'))
    boss_zh = table('JournalEncounter', 'zhCN', ('Name_lang', 'JournalInstanceID'))
    dungeon_en = table('DungeonEncounter', 'enUS', ('Name_lang',))
    by_instance = {row['id']: row for row in instances}
    candidates = []
    name_sources = {}
    matched_instances, matched_bosses = set(), set()
    for row in instances:
        jid = row['journal_id']
        if jid in instance_en and jid in instance_zh and instance_zh[jid]['Name_lang'] == row['name']:
            name = instance_en[jid]['Name_lang']
            candidates.append((name, row['name'], 'JournalInstance', jid, False))
            if _aliases(name, source_text):
                matched_instances.add(row['id'])
    for row in bosses:
        jid = row['journal_id']
        instance = by_instance[row['instance_id']]
        zh = boss_zh.get(jid, {})
        if zh.get('Name_lang') != row['name'] or int(zh.get('JournalInstanceID', 0)) != instance['journal_id']:
            continue
        english = boss_en.get(jid, {})
        name = english.get('Name_lang', '') if int(english.get('JournalInstanceID', 0)) == instance['journal_id'] else ''
        row['english_table'], row['english_id'] = 'JournalEncounter', jid
        # Existing release proof supports the native DungeonEncounter ID bridge.
        if not name:
            dungeon_id = int(row['payload__dungeon_encounter_id'] or 0)
            name = dungeon_en.get(dungeon_id, {}).get('Name_lang', '')
            row['english_table'], row['english_id'] = 'DungeonEncounter', dungeon_id
        row['english'] = name
        if name and _aliases(name, source_text):
            matched_bosses.add(row['id'])
    # A boss alias that also names a current global spell cannot establish raid
    # context alone. An explicit raid or another unambiguous boss can attest it.
    unanchored = [row for row in bosses if row['id'] in matched_bosses
                  and row['instance_id'] not in matched_instances]
    if unanchored:
        from django.db.models import Subquery
        from botend.models import WowSpellSnapshot, WowSpellSnapshotState
        aliases = {alias for row in unanchored for alias in _aliases(row['english'], source_text)}
        global_names = {name.casefold() for name in WowSpellSnapshot.objects.filter(
            branch='wow', locale='zhCN', name__in=aliases,
            snapshot_build=Subquery(WowSpellSnapshotState.objects.filter(
                branch='wow', locale='zhCN').values('snapshot_build')[:1]),
        ).values_list('name', flat=True)}
        ambiguous = {row['id'] for row in unanchored
                     if any(alias.casefold() in global_names for alias in _aliases(row['english'], source_text))}
        anchored_instances = {row['instance_id'] for row in unanchored if row['id'] not in ambiguous}
        matched_bosses -= {row['id'] for row in unanchored
                           if row['id'] in ambiguous and row['instance_id'] not in anchored_instances}
    scoped = [row for row in bosses if row['instance_id'] in matched_instances or row['id'] in matched_bosses]
    if not scoped:
        return empty
    for row in scoped:
        if row.get('english'):
            candidates.append((row['english'], row['name'], 'JournalEncounter', row['journal_id'], False))
            name_sources['JournalEncounter', row['journal_id']] = {
                'english_table': row['english_table'], 'english_id': row['english_id']}
    scoped_jids = {row['journal_id'] for row in scoped}
    payloads = JournalEncounter.objects.filter(id__in=[row['id'] for row in scoped]).values_list('payload__sections', flat=True)
    needed = set()
    for sections in payloads:
        for section in sections or []:
            spell_id = int(section.get('spell_id') or 0)
            if spell_id > 0:
                needed.add(spell_id)
            needed.update(_ids(json.dumps(section, ensure_ascii=False)))
    # Raw section IDs are joined within their native encounter relation, not by title.
    for table_name, name_field in [('JournalEncounterSection', 'Title_lang'), ('JournalEncounterCreature', 'Name_lang')]:
        fields = (name_field, 'JournalEncounterID')
        if table_name == 'JournalEncounterSection':
            fields += ('SpellID', 'BodyText_lang')
        english = table(table_name, 'enUS', fields, filter_field='JournalEncounterID', selected=scoped_jids)
        chinese = table(table_name, 'zhCN', fields, filter_field='JournalEncounterID', selected=scoped_jids)
        for identity, en in english.items():
            zh = chinese.get(identity)
            if not zh or en['JournalEncounterID'] != zh['JournalEncounterID']:
                continue
            if table_name == 'JournalEncounterSection':
                if en['SpellID'] != zh['SpellID']:
                    continue
                needed.update(_ids(json.dumps([en, zh], ensure_ascii=False)))
                spell_id = int(en['SpellID'])
                if spell_id > 0:
                    needed.add(spell_id)
                    continue  # Current ID-paired SpellName, not an old compiled title.
            candidates.append((en[name_field], zh[name_field], table_name, identity,
                               table_name == 'JournalEncounterSection'))
    version = WowTalentVersion.objects.filter(branch='retail', is_active=True).order_by('-is_default_player_tree', '-id').first()
    # Bound explicit name-reference closure, including cycles; never query by name.
    for _depth in range(8):
        en_spells, zh_spells = _spell_pairs(version, needed)
        references = set().union(*(_ids(row['Name_lang']) for row in [*en_spells.values(), *zh_spells.values()]))
        additional = references - needed
        if not additional:
            break
        needed.update(additional)
        if len(needed) > 10000:
            return empty
    else:
        en_spells, zh_spells = {}, {}
    for identity, en in en_spells.items():
        zh = zh_spells.get(identity)
        if zh:
            candidates.append((en['Name_lang'], zh['Name_lang'], 'SpellName', identity, True))
    groups = defaultdict(list)
    # Boss/instance identity is stronger than a homonymous player spell.
    entity_keys = {alias.casefold() for en, zh, kind, identity, controlled in candidates
                   if kind in ('JournalInstance', 'JournalEncounter') for alias in _aliases(en, source_text)}
    for en, zh, kind, identity, controlled in candidates:
        if not en or not zh or en == zh or '$' in en or '|' in en or not re.search('[\u3400-\u9fff]', zh):
            continue
        for alias in _aliases(en, source_text):
            key = alias.casefold()
            if kind == 'SpellName' and key in entity_keys:
                continue
            groups[key].append((alias, zh, kind, identity, controlled))
    result = JournalTerms(suppress_fallback=bool(
        needed - en_spells.keys() or needed - zh_spells.keys()
        or any(not re.search('[\u3400-\u9fff]', row['Name_lang'])
               or '$' in row['Name_lang'] or '|' in row['Name_lang']
               for row in zh_spells.values())))
    # A readable English locale can veto stale lower-priority translations even
    # when the matching official Chinese row is absent or untrusted.
    for identity, en in en_spells.items():
        zh = zh_spells.get(identity, {}).get('Name_lang', '')
        if not re.search('[\u3400-\u9fff]', zh) or '$' in zh or '|' in zh:
            result.blocked.update(alias.casefold() for alias in _aliases(en['Name_lang'], source_text)
                                  if '$' not in alias and '|' not in alias)

    from botend.constants.wow import CLASS_CN, SPEC_CN
    from botend.services.wow_news_glossary_service import _extract_name_candidates, _split_pascal_name
    class_terms = set(CLASS_CN)
    class_terms.update(_split_pascal_name(name) for name in tuple(class_terms))
    spec_terms = set(SPEC_CN)
    spec_terms.update(_split_pascal_name(name) for name in tuple(spec_terms))
    # Spec labels include ordinary words (Blood, Fury, Balance). Require a
    # standalone heading or explicit player usage, not e.g. Blood Venom.
    class_context = any(_aliases(name, source_text) for name in class_terms) or any(
        re.search(r'(?im)^\s*' + re.escape(name) + r'\s*(?:[.:]|$)', source_text)
        or re.search(_BOUNDARY.format(re.escape(name))
                     + r'\s+(?:spec(?:ialization)?|talents?|uses?|casts?)\b', source_text, re.I)
        for name in spec_terms)
    if class_context:
        from django.db.models import Subquery
        from botend.models import WowSpellSnapshot, WowSpellSnapshotState
        from botend.services.wow_news_glossary_service import _name_candidate_queries
        source_candidates = _extract_name_candidates(source_text)
        # Case-insensitive Journal matches retain official casing for the
        # bounded legacy lookup, even when the source spells them lowercase.
        source_candidates.update(alias for en, zh, kind, identity, controlled in candidates
                                 if kind in ('JournalInstance', 'JournalEncounter')
                                 for alias in _aliases(en, source_text)
                                 if alias.casefold() in entity_keys)
        query = WowSpellSnapshot.objects.filter(
            branch='wow', locale='zhCN',
            snapshot_build=Subquery(WowSpellSnapshotState.objects.filter(
                branch='wow', locale='zhCN').values('snapshot_build')[:1]),
        ).values_list('name', 'spell_id')
        global_rows = [row for batch in _name_candidate_queries(query, source_candidates) for row in batch]
        global_names = {name.casefold() for name, _sid in global_rows}
        if class_context:
            # Without occurrence positions, an explicit class/spec and a player
            # spell homonym make the whole key unsafe, including boss mentions.
            result.blocked.update(entity_keys & global_names)
    result.blocked.difference_update(entity_keys - (global_names if class_context else set()))
    for key, rows in groups.items():
        result.evidence[key] = [dict(table=kind, id=identity,
                                     branch=version.branch if kind == 'SpellName' else 'wow',
                                     relation_branch='wow', relation_build=build,
                                     name_build=version.current_build if kind == 'SpellName' else build,
                                     name_en=alias, name_zh=zh,
                                     **name_sources.get((kind, identity), {}))
                                for alias, zh, kind, identity, controlled in rows]
        if len({row[1] for row in rows}) != 1:
            result.blocked.add(key)
            continue
        alias, zh, _kind, _identity, controlled = rows[0]
        result.pairs.append((alias, zh))
        if len(alias.split()) == 1 and all(row[4] for row in rows):
            result.controlled.add(key)
        else:
            result.trusted.add(key)
    return result
