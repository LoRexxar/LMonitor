"""One selected/hydrated log cohort for single and combined M+ publications.

Only fingerprints/references are persisted here; raw rankings and CombatantInfo
remain in their central tables. Cache hits never aggregate, enrich or publish.
"""
from botend.services.talent_source_retention import protect_sources
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from django.db.models import Exists, OuterRef, Q
from django.db.models.functions import Lower

from botend.models import (SeasonMeta, SpecDungeonRanking, WowItemSnapshot,
                           WowItemVariantSnapshot, WowTalentNodeMetadata,
                           WowTalentVersion, WowSpellSnapshot)
from botend.services import spec_stats_service as stats
from botend.services import wcl_combatant_snapshot as combatants
from botend.services.spec_stats_snapshot import content_digest, reusable_projection
from botend.wow.talents.versioning import TalentVersionResolver


# Bump when selection, aggregation, source interpretation or display code changes.
ALGORITHM_VERSION = 'dungeon-shared-cohort-wcl-v3-log-talents'
_NON_CONTENT_FIELDS = {'created_at', 'updated_at', 'last_updated', 'fetched_at'}


def _content_rows(queryset):
    """Hash contents, not MAX(timestamp): bulk .update() must invalidate too."""
    fields = [field.attname for field in queryset.model._meta.concrete_fields
              if field.name not in _NON_CONTENT_FIELDS]
    return list(queryset.order_by('pk').values(*fields))


def _variant_dependencies(items, gear_records):
    """Match the existing display scope before hydrating any variant JSON.

    Aggregate equipment keeps itemLevel but not bonusIDs; its display requests
    therefore choose the lowest default or the scored exact-level variant.
    Use the display scorer, and inspect branch JSON *keys*, never the full large
    metadata column on historical/unselected variants.
    """
    from botend.services.wow_item_display import _positive_int, _variant_score
    active = SeasonMeta.objects.filter(is_active=True).exclude(gear_batch_key='').annotate(
        has_gear_catalog=Exists(WowItemVariantSnapshot.objects.filter(
            season_id=OuterRef('pk'), batch_key=OuterRef('gear_batch_key'))))
    season = (active.filter(has_gear_catalog=True).order_by('-gear_synced_at', '-id').first()
              or active.order_by('-id').first())
    if season is None or not items:
        return None, []
    variants = WowItemVariantSnapshot.objects.filter(
        season=season, batch_key=season.gear_batch_key, item__item_id__in=items)
    grouped = {}
    for row in variants.values('pk', 'item_id', 'item_level', 'variant_type', 'data_branch',
                               'metadata__data_branch', 'metadata__ptr_preview'):
        branch = (row['metadata__data_branch'] or ('ptr' if row['metadata__ptr_preview'] else '')
                  or row['data_branch'])
        grouped.setdefault(row['item_id'], {}).setdefault(branch, []).append(SimpleNamespace(
            pk=row['pk'], item_level=row['item_level'], variant_type=row['variant_type'], bonus_ids=[]))
    levels = {item_id: {0} for item_id in items}
    for record in gear_records:
        for item in record.get('gear_json') or []:
            if not isinstance(item, dict):
                continue
            item_id = _positive_int(item.get('id') or item.get('itemID') or item.get('item_id'))
            if item_id in levels:
                levels[item_id].add(_positive_int(item.get('itemLevel')))
    selected = set()
    for item_id, item in items.items():
        branches = grouped.get(item.pk, {})
        branch = next(iter(branches)) if len(branches) == 1 else 'retail'
        candidates = branches.get(branch, [])
        for level in levels[item_id]:
            if level:
                variant = max((row for row in candidates if row.item_level == level),
                              key=lambda row: _variant_score(row, level, ()), default=None)
            else:
                variant = min(candidates, key=lambda row: (row.item_level, row.pk), default=None)
            if variant is not None:
                selected.add(variant.pk)
    return [season.pk, season.gear_batch_key], _content_rows(variants.filter(pk__in=selected))


@lru_cache(maxsize=24)
def _reference_table(attribute, file_key):
    """Share only immutable file tables, never a resolver's mutable DB cache.

    Spell text/effect/misc indexes already have bounded file-identity caches in
    spell_text. These three remaining loaders are instance-local there. Delegate
    parsing to them too, but don't reread their entire CSV for every spec/hit.
    """
    from botend.wow.spell_text import SpellTextResolver
    resolver = SpellTextResolver(dump_dir=Path(file_key[0]).parent)
    if attribute == '_max_stacks_cache':
        resolver._max_stacks_value(0)
    else:
        getattr(resolver, {'_duration_cache': '_load_duration_table',
                           '_radius_cache': '_load_radius_table'}[attribute])()
    return getattr(resolver, attribute)


@lru_cache(maxsize=16)
def _structural_dump_digest(file_key):
    """Choice ordering and decoder placeholders also consume exact-build CSVs."""
    path = Path(file_key[0])
    if not path.is_file():
        return None
    digest = sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


@protect_sources
def _resolved_talent_dependencies(nodes, version, supplements):
    """Fingerprint the authority's output, not a second token dependency graph.

    Resolve this class's rows in the actual statistics version, once per row.
    Fresh per-publication resolvers recheck all reachable DB fallbacks (including
    references introduced by unchanged dump text). Their own recursion/context,
    locale/build selection and dump precedence remain authoritative. No ranking
    enrichment, talent usage aggregation, or render-model hydration occurs here.
    """
    from botend.wow.spell_text import _file_cache_key
    from botend.wow.talents.metadata import TalentMetadataProvider

    if version is None:
        return {}
    provider = TalentMetadataProvider(talent_version=version)
    provider._name_supplement_cache[version.pk] = supplements
    rows = [row for row in nodes if row['talent_version_id'] == version.pk]
    if not rows:
        return {}
    for locale in ('enUS', 'zhCN'):
        resolver = provider._spell_resolver(locale)
        for attribute, filename in (('_duration_cache', 'SpellDuration.csv'),
                                    ('_radius_cache', 'SpellRadius.csv'),
                                    ('_max_stacks_cache', 'SpellAuraOptions.csv')):
            setattr(resolver, attribute, _reference_table(
                attribute, _file_cache_key(resolver._dump_file(filename))))
    resolved = []
    for row in rows:
        # Real model instances preserve localized-name supplementation semantics.
        data = provider._as_dict(WowTalentNodeMetadata(**row))
        resolved.append((data, provider._build_choice_option(data)))
    source = Path(version.source_dir or '')
    return {
        'nodes': resolved,
        'structure': [_structural_dump_digest(_file_cache_key(source / filename))
                      for filename in ('TraitNodeXTraitNodeEntry.csv', 'TraitNode.csv')],
    }


def _metadata_dependencies(records, facts, class_name, spec_name):
    """Bound reads to the cohort's items and this class's talent universe.

    Reuse the statistics item discovery (gear/gems/enchants) and central enchant
    mapping. No all-item/all-spec JSON scan per spec and no timestamp-only cache.
    The decoder consumes other specs of the *same class*, not only selected nodes.
    """
    events = [event for payloads in facts.values() for event in payloads]
    gear_records = [*records, *({'gear_json': event.get('gear') or []} for event in events)]
    catalog = combatants._enchant_catalog([
        {'gear': row.get('gear_json') or []} for row in gear_records])
    gear_records.append({'gear_json': [{'id': row['item_id']} for row in catalog.values()]})
    items = stats._collect_item_ids_from_records(gear_records)
    item_rows = [{field.attname: getattr(item, field.attname)
                  for field in WowItemSnapshot._meta.concrete_fields
                  if field.name not in _NON_CONTENT_FIELDS}
                 for _, item in sorted(items.items())]
    gear_scope, variants = _variant_dependencies(items, gear_records)
    version = TalentVersionResolver.get_default(TalentVersionResolver.USAGE_STATS)
    node_query = WowTalentNodeMetadata.objects.filter(class_name=class_name)
    if version is not None:
        # Build summaries also read unversioned hero-anchor names for this spec.
        node_query = node_query.filter(Q(talent_version=version) |
                                       Q(tree_type='hero_anchor', spec_name=spec_name))
    nodes = _content_rows(node_query)
    reference_ids = {row[field] for row in nodes for field in
                     ('node_id', 'talent_id', 'spell_id', 'display_spell_id') if row[field]}
    def collect_talent_ids(value):
        if isinstance(value, dict):
            for field in ('id', 'node_id', 'talent_id', 'spell_id', 'spellID', 'display_spell_id'):
                if identifier := combatants._positive_int(value.get(field)):
                    reference_ids.add(identifier)
            for child in value.values():
                if isinstance(child, (dict, list)):
                    collect_talent_ids(child)
        elif isinstance(value, list):
            for child in value:
                collect_talent_ids(child)
    for row in records:
        collect_talent_ids(row.get('talents_json'))
    for event in events:
        collect_talent_ids(event.get('talentTree'))
    # Name supplements can match by ID or case-insensitive English name.
    names = {row['name'].casefold() for row in nodes if row['name']}
    supplements = list(WowTalentNodeMetadata.all_objects.filter(
        localization_only=True, name_kind__in=('talent', 'spell'),
        talent_version_id__in={row['talent_version_id'] for row in nodes}
    ).annotate(folded_name=Lower('name')).filter(
        Q(reference_id__in=reference_ids) | Q(folded_name__in=names)
    ).order_by('pk').values('talent_version_id', 'reference_id', 'name_kind', 'name', 'name_zh', 'icon'))
    subtree_ids = sorted({row['db2_subtree_id'] for row in nodes if row['db2_subtree_id']})
    resolved_versions = {usage: getattr(TalentVersionResolver.get_default(usage), 'pk', None)
                         for usage in TalentVersionResolver.DEFAULT_FLAG_BY_USAGE}
    try:
        resolved_versions['retail'] = TalentVersionResolver.resolve(version_key='retail').pk
    except ValueError:
        resolved_versions['retail'] = None
    return {
        'items': item_rows,
        'enchant_catalog': sorted(catalog.items()),
        'variants': variants,
        'gear_scope': gear_scope,
        'talent_versions': _content_rows(WowTalentVersion.objects.all()),
        'resolved_versions': resolved_versions,
        'talent_nodes': nodes,
        'talent_names': supplements,
        # Direct snapshot fallbacks also supply names for raw WCL nodes.
        'spells': _content_rows(WowSpellSnapshot.objects.filter(spell_id__in=reference_ids)),
        'resolved_talents': _resolved_talent_dependencies(nodes, version, [
            row for row in supplements if version and row['talent_version_id'] == version.pk]),
        'hero_names': [(sid, stats._hero_subtree_name_from_table(sid)) for sid in subtree_ids],
    }


def aggregate_dungeon(season, class_name, spec_name, path, publish, *,
                      selected_ids=None, source_cutoff=None):
    """Publish once or reuse a verified generation; return publication evidence.

    selected_ids is {str(dungeon_id): [ranking_id, ...]} in frozen selector order.
    Missing frozen rows/scopes fail closed rather than silently changing cohort.
    Legacy frozen cohorts only lose rows below their own highest level minus 2;
    references/order remain intact, with no reselection or replacement logs.
    source_cutoff limits central facts, but the wall clock itself is not hashed.
    publish(path, payload, class_name, spec_name) must publish a stats projection.
    """
    path = Path(path)
    encounters = {int(enc['id']): enc for enc in (season.mplus_encounters or [])}
    if selected_ids is not None and set(selected_ids) != {str(key) for key in encounters}:
        raise ValueError('Frozen dungeon scopes do not match the season')
    by_dungeon = {}
    for dungeon_id in encounters:
        queryset = SpecDungeonRanking.objects.filter(
            season_id=season.id, dungeon_id=dungeon_id,
            class_name=class_name, spec_name=spec_name)
        if selected_ids is None:
            selected = stats._select_dungeon_sample_records(queryset, max_samples=100)
        else:
            ids = selected_ids[str(dungeon_id)]
            if (not isinstance(ids, list) or len(ids) > 100
                    or any(type(pk) is not int or pk <= 0 for pk in ids)
                    or len(set(ids)) != len(ids)):
                raise ValueError('Invalid frozen dungeon ranking references')
            selected = stats._hydrate_dungeon_samples(
                queryset, [{'id': pk} for pk in ids], stats.DUNGEON_SAMPLE_FIELDS)
            if len(selected) != len(ids):
                raise ValueError('Frozen dungeon ranking references are missing')
            level_floor = stats._dungeon_sample_level_floor(selected)
            selected = [row for row in selected
                        if (row.get('keystone_level') or 0) >= level_floor]
        by_dungeon[dungeon_id] = selected
    selected = [row for rows in by_dungeon.values() for row in rows]
    facts = combatants._load_facts(selected, fetched_before=source_cutoff)
    fingerprint = content_digest({
        'algorithm': ALGORITHM_VERSION,
        'scope': [season.pk, class_name, spec_name],
        'encounters': [(key, stats._lookup_dungeon_cn(enc['name'])) for key, enc in encounters.items()],
        'selected': list(by_dungeon.items()),
        'facts': [(list(key), events) for key, events in sorted(facts.items())],
        'metadata': _metadata_dependencies(selected, facts, class_name, spec_name),
    })
    required_keys = [f'dungeon-{key}' for key in encounters] + ['dungeon-all']
    index = reusable_projection(path, fingerprint, required_keys)
    reused = index is not None
    if not reused:
        enriched = combatants.enrich_dungeon_records(selected, class_name, spec_name, facts=facts)
        enriched_by_id = {row['id']: row for row in enriched}
        singles, union, union_enriched, coverage, seen = [], [], [], [], set()
        for dungeon_id, enc in encounters.items():
            rows = by_dungeon[dungeon_id]
            name = stats._lookup_dungeon_cn(enc['name'])
            singles.append(stats.SpecStatsService._compute_dungeon_records(
                rows, season.id, dungeon_id, name, class_name, spec_name, full=True,
                enriched_records=[enriched_by_id[row['id']] for row in rows]))
            contributed = 0
            for row in rows:
                identity = stats._dungeon_log_identity(row)
                if identity not in seen:
                    seen.add(identity)
                    union.append(row)
                    union_enriched.append(enriched_by_id[row['id']])
                    contributed += 1
            coverage.append({'dungeon_id': dungeon_id, 'dungeon_name': name,
                             'sample_size': len(rows), 'contributed_sample_size': contributed,
                             'target_sample_size': 100})
        summary = stats.SpecStatsService._compute_dungeon_records(
            union, season.id, 'all', '全部副本汇总', class_name, spec_name, full=True,
            include_talent_build_popularity=False, enriched_records=union_enriched)
        target = len(encounters) * 100
        summary.update(target_sample_size=target, missing_sample_size=max(0, target - len(union)),
                       dungeon_samples=coverage)
        publish(path, {'dungeons': singles, 'summary': summary, '_input_fingerprint': fingerprint},
                class_name, spec_name)
        index = reusable_projection(path, fingerprint, required_keys)
        if index is None:
            raise RuntimeError('Dungeon publication did not produce a complete matching generation')
    return {'reused': reused, 'generation': index['_snapshot']['generation'],
            'generated_at': index.get('generated_at'), 'fingerprint': fingerprint,
            'path': str(path), 'selected': len(selected)}
