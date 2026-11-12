"""复用同一批日志生成大秘境单本与汇总统计。

原始排名与战斗事实保留在中央表；可重建的范围结果独立缓存。
完整投影命中时不再计算、补全或发布。
"""
from botend.services.talent_source_retention import protect_sources
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

from django.db.models import Q
from django.db.models.functions import Lower

from botend.models import (SpecDungeonRanking, WowItemSnapshot,
                           WowItemVariantSnapshot, WowTalentNodeMetadata)
from botend.services import spec_stats_service as stats
from botend.services import wcl_combatant_snapshot as combatants
from botend.services.spec_stats_snapshot import content_digest, reusable_projection
from botend.wow.talents.versioning import TalentVersionResolver


# Bump when selection, aggregation, source interpretation or display code changes.
ALGORITHM_VERSION = 'dungeon-shared-cohort-wcl-v6-sample-facts'
_NON_CONTENT_FIELDS = {'created_at', 'updated_at', 'last_updated', 'fetched_at', 'variant_text_templates'}


def _content_rows(queryset, *, items_by_pk=None):
    """Hash contents, not MAX(timestamp): bulk .update() must invalidate too."""
    excluded = _NON_CONTENT_FIELDS
    if queryset.model is WowTalentNodeMetadata:
        excluded = excluded | {'reference_aliases', 'localization_evidence'}
    fields = [field.attname for field in queryset.model._meta.concrete_fields
              if field.name not in excluded]
    if queryset.model is WowItemVariantSnapshot:
        # 存储格式变化不是装备事实变化，不能触发各专精聚合重复重建。
        rows = queryset.order_by('pk')
        if items_by_pk is None:
            rows = rows.select_related('item')
        result = []
        for row in rows:
            if items_by_pk is not None:
                row.item = items_by_pk[row.item_id]
            result.append({**{field: getattr(row, field) for field in fields},
                           'effects_json': row.effects_data})
        return result
    return list(queryset.order_by('pk').values(*fields))


def _variant_dependencies(items, gear_records):
    """Track the same current variants as ordinary aggregate display."""
    from botend.services.wow_item_display import (
        _active_item_catalog_season, _needs_effects_compilation, _positive_int,
        _select_ordinary_item_variant,
    )
    if not items:
        return None, []
    season = _active_item_catalog_season()
    if season is None:
        return None, []
    items_by_pk = {item.pk: item for item in items.values()}
    variants = WowItemVariantSnapshot.objects.filter(
        season=season, batch_key=season.gear_batch_key, item_id__in=items_by_pk).order_by()
    grouped = {}
    # Only selection fields/JSON keys: never hydrate all historical variant JSON.
    # Presence, not truthiness, matters even for a null difficulty_drop marker.
    for row in variants.annotate(dedicated=Q(metadata__has_key='difficulty_drop')).values(
            'pk', 'item_id', 'item_level', 'variant_type', 'data_branch', 'bonus_ids',
            'metadata__data_branch', 'metadata__ptr_preview', 'dedicated'):
        metadata = {'data_branch': row['metadata__data_branch'],
                    'ptr_preview': row['metadata__ptr_preview']}
        if row['dedicated']:
            metadata['difficulty_drop'] = None
        grouped.setdefault(row['item_id'], []).append(SimpleNamespace(
            pk=row['pk'], item_level=row['item_level'], variant_type=row['variant_type'],
            bonus_ids=row['bonus_ids'], data_branch=row['data_branch'], metadata=metadata))
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
        candidates = grouped.get(item.pk, [])
        if _needs_effects_compilation(item):
            # The reader compiles across *all* current item variants, including
            # other branches and dedicated drops, before selecting display facts.
            selected.update(row.pk for row in candidates)
            continue
        for level in levels[item_id]:
            # _compute_gear_popularity retains itemLevel but drops bonusIDs.
            variant, _branch = _select_ordinary_item_variant(
                candidates, item_level=level, allow_default_variant=True, default_order='lowest')
            if variant is not None:
                selected.add(variant.pk)
    return [season.pk, season.gear_batch_key], _content_rows(
        variants.filter(pk__in=selected), items_by_pk=items_by_pk)


@lru_cache(maxsize=16)
def _structural_dump_digest(file_key, entry_ids=(), tree_ids=()):
    """Choice ordering and decoder placeholders also consume exact-build CSVs."""
    path = Path(file_key[0])
    if not path.is_file():
        return None
    import csv
    entries, trees = set(entry_ids), set(tree_ids)
    selected = []
    with path.open(encoding='utf-8-sig', newline='') as handle:
        for row in csv.DictReader(handle):
            try:
                relevant = (int(row.get('TraitNodeEntryID') or 0) in entries
                            if path.name == 'TraitNodeXTraitNodeEntry.csv'
                            else int(row.get('TraitTreeID') or 0) in trees)
            except (ValueError, TypeError):
                continue
            if relevant:
                selected.append(row)
    # Unique consumer keys make physical CSV order immaterial. Duplicates keep
    # their original order: entry maps are last-wins, decoder nodes first-wins.
    identity = 'TraitNodeEntryID' if path.name == 'TraitNodeXTraitNodeEntry.csv' else 'ID'
    keys = [row.get(identity) for row in selected]
    try:
        numeric_keys = [int(key) for key in keys]
    except (TypeError, ValueError):
        numeric_keys = None
    if (numeric_keys is not None and all(key > 0 for key in numeric_keys)
            and len(set(numeric_keys)) == len(numeric_keys)):
        selected = [row for _, row in sorted(zip(numeric_keys, selected), key=lambda pair: pair[0])]
    return content_digest(selected)



@protect_sources
def _resolved_talent_dependencies(nodes, version, supplements, *, display_reader=None):
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
    if display_reader is not None:
        provider._display_reader = display_reader
    provider._name_supplement_cache[version.pk] = supplements
    rows = [row for row in nodes if row['talent_version_id'] == version.pk]
    if not rows:
        return {}
    resolved = []
    # Match the provider's other readers: batch central display data before
    # serializing nodes, rather than issuing one lookup per talent.
    for row in provider._prime_display(WowTalentNodeMetadata(**values) for values in rows):
        # Real model instances preserve localized-name supplementation semantics.
        data = provider._as_dict(row)
        resolved.append((data, provider._build_choice_option(data)))
    source = Path(version.source_dir or '')
    return {
        'nodes': resolved,
        'structure': [_structural_dump_digest(_file_cache_key(source / filename),
                      tuple(sorted({row['node_id'] for row in rows if row['node_id']})),
                      tuple(sorted({row['db2_tree_id'] for row in rows if row['db2_tree_id']})))
                      for filename in ('TraitNodeXTraitNodeEntry.csv', 'TraitNode.csv')],
    }


def _metadata_dependencies(records, facts, class_name, spec_name, *, context=None):
    """Bound reads to the cohort's items and this class's talent universe.

    Reuse the statistics item discovery (gear/gems/enchants) and central enchant
    mapping. No all-item/all-spec JSON scan per spec and no timestamp-only cache.
    The decoder consumes other specs of the *same class*, not only selected nodes.
    """
    from .stats_talent_dependencies import effective_grants
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
    # Caller-owned, one fingerprint batch only. Never retain DB facts on a
    # process-wide provider or on the long-lived monitor instance.
    context = context if context is not None else {}
    base_key = ('talent-base', class_name, spec_name)
    if base_key not in context:
        version = TalentVersionResolver.get_default(TalentVersionResolver.USAGE_STATS)
        node_query = WowTalentNodeMetadata.objects.filter(class_name=class_name)
        if version is not None:
            # Build summaries also read unversioned hero-anchor names for this spec.
            node_query = node_query.filter(Q(talent_version=version) |
                                           Q(tree_type='hero_anchor', spec_name=spec_name))
        nodes = _content_rows(node_query)
        subtree_ids = sorted({row['db2_subtree_id'] for row in nodes if row['db2_subtree_id']})
        context[base_key] = (version, nodes, [
            (sid, stats._hero_subtree_name_from_table(sid)) for sid in subtree_ids])
    version, nodes, hero_names = context[base_key]
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
    # Raw WCL references can differ by scope. Resolve only identical complete
    # supplement inputs once; never share a scope's reference set with another.
    # One actual reader per invocation/batch: node contexts and raw fallbacks
    # must use the same captured central rows, not independently reload them.
    from botend.services.wow_spell_display import SpellDisplayReader
    reader_key = ('spell-reader', getattr(version, 'branch', 'retail'))
    if reader_key not in context:
        context[reader_key] = SpellDisplayReader(reader_key[1])
    reader = context[reader_key]
    reader.preload(reference_ids)
    resolved_key = ('resolved-talents', base_key, content_digest(supplements))
    if resolved_key not in context:
        context[resolved_key] = _resolved_talent_dependencies(nodes, version, [
            row for row in supplements if version and row['talent_version_id'] == version.pk],
            display_reader=reader)
    # 当前版本解析器将所有使用场景固定到正式服；同一依赖检查无需重复查询四次。
    resolved_versions = {usage: getattr(version, 'pk', None)
                         for usage in (*TalentVersionResolver.DEFAULT_FLAG_BY_USAGE, 'retail')}
    # 单本拆分后，相同引用集合仍只查询一次；上下文仅存活于本轮发布。
    spell_key = ('spell-rows', tuple(sorted(reference_ids)))
    if spell_key not in context:
        # The same reader owns branch/locale fallback and compiled/default
        # precedence. Contextual node reads are already captured above in
        # resolved_talents; raw WCL fallback calls get() without a node context.
        context[spell_key] = [(sid, reader.get(sid)) for sid in sorted(reference_ids)]
    grants_key = ('talent-grants', getattr(version, 'pk', None), class_name)
    if grants_key not in context:
        context[grants_key] = effective_grants(version, class_name)
    return {
        # Earlier dependency contracts omitted some actually consumed variants.
        # A matching legacy digest cannot certify those historical results.
        'metadata_contract': 'v2-complete-items-effective-spells',
        'items': item_rows,
        'enchant_catalog': sorted(catalog.items()),
        'variants': variants,
        'gear_scope': gear_scope,
        # 当前分支版本的内容由节点、解析输出及结构文件覆盖；无关版本和构建时间不触发全量重算。
        'talent_grants': context[grants_key],
        'talent_versions': {'id': getattr(version, 'pk', None),
                            'branch': getattr(version, 'branch', None)},
        'resolved_versions': resolved_versions,
        'talent_nodes': nodes,
        'talent_names': supplements,
        # Direct snapshot fallbacks also supply names for raw WCL nodes.
        'spells': context[spell_key],
        'resolved_talents': context[resolved_key],
        'hero_names': hero_names,
    }


def aggregate_dungeon(season, class_name, spec_name, path, publish, *,
                      selected_ids=None, source_cutoff=None):
    """Publish once or reuse a verified generation; return publication evidence.

    selected_ids is {str(dungeon_id): [ranking_id, ...]} in frozen selector order.
    Missing frozen rows/scopes fail closed rather than silently changing cohort.
    source_cutoff limits central facts, but the wall clock itself is not hashed.
    publish(path, payload, class_name, spec_name) must publish a stats projection.
    """
    from .stats_talent_dependencies import structure_token
    token = structure_token(class_name)
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
            selected = [row for row in selected if combatants._fight_key(row)]
        by_dungeon[dungeon_id] = [row for row in selected if combatants._fight_key(row)]
    selected = [row for rows in by_dungeon.values() for row in rows]
    facts = combatants.load_matched_facts(selected, class_name, spec_name, fetched_before=source_cutoff)
    metadata, context = {}, {}
    for dungeon_id, rows in by_dungeon.items():
        scope_facts = combatants.matched_facts(rows, facts, class_name, spec_name)
        metadata[dungeon_id] = content_digest(
            _metadata_dependencies(rows, scope_facts, class_name, spec_name, context=context))
    fingerprint = content_digest({
        'algorithm': ALGORITHM_VERSION,
        'storage_version': 4,
        'scope': [season.pk, class_name, spec_name],
        'encounters': [(key, stats._lookup_dungeon_cn(enc['name'])) for key, enc in encounters.items()],
        'selected': list(by_dungeon.items()),
        'facts': [(list(key), events) for key, events in sorted(facts.items())],
        'metadata': metadata,
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
            from botend.services.spec_aggregation_cache import load_work, save_work
            scope = f'dungeon:{class_name}:{spec_name}:{dungeon_id}'
            scope_fingerprint = content_digest({'algorithm': ALGORITHM_VERSION, 'name': name,
                'storage_version': 4, 'rows': rows,
                'facts': [enriched_by_id[row['id']] for row in rows], 'metadata': metadata[dungeon_id]})
            detail = load_work(season.id, scope, scope_fingerprint)
            if detail is None:
                detail = stats.SpecStatsService._compute_dungeon_records(
                    rows, season.id, dungeon_id, name, class_name, spec_name, full=True,
                    enriched_records=[enriched_by_id[row['id']] for row in rows])
                detail = save_work(season.id, scope, scope_fingerprint, detail)
            singles.append(detail)
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
        from botend.services.spec_aggregation_cache import load_work, save_work
        summary_scope = f'dungeon:{class_name}:{spec_name}:all'
        summary = load_work(season.id, summary_scope, fingerprint)
        if summary is None:
            summary = stats.SpecStatsService._compute_dungeon_records(
                union, season.id, 'all', '全部副本汇总', class_name, spec_name, full=True,
                include_talent_build_popularity=False, enriched_records=union_enriched)
            summary = save_work(season.id, summary_scope, fingerprint, summary)
        target = len(encounters) * 100
        summary.update(target_sample_size=target, missing_sample_size=max(0, target - len(union)),
                       dungeon_samples=coverage)
        publish(path, {'_data_contract': 'wcl-log-v2', 'dungeons': singles, 'summary': summary, '_input_fingerprint': fingerprint,
                       '_talent_structure_token': token},
                class_name, spec_name)
        index = reusable_projection(path, fingerprint, required_keys)
        if index is None:
            raise RuntimeError('Dungeon publication did not produce a complete matching generation')
    return {'reused': reused, 'generation': index['_snapshot']['generation'],
            'generated_at': index.get('generated_at'), 'fingerprint': fingerprint,
            'path': str(path), 'selected': len(selected)}
