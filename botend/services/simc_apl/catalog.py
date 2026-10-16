"""只读合并 SimC token、WoW 技能快照与 APL 本地化元数据。"""
from dataclasses import dataclass
from typing import Optional

from django.db.models import Q
from botend.wow.branches import spell_branch

from botend.models import (
    SimcAplSymbol, SimcAplSymbolScope, WowSpellSnapshot, WowSpecSpellMapSnapshot,
    WowTalentNodeMetadata, WowTalentVersion,
)

UNBOUND_REASON = '尚无 SimC APL token 映射'


@dataclass(frozen=True)
class CatalogItem:
    token: Optional[str]
    kind: str
    spell_id: Optional[int]
    name: str
    name_en: str
    description: str
    icon: str
    insertable: bool
    reason: Optional[str]
    source: str
    class_name: Optional[str]
    spec: Optional[str]
    hero_tree: Optional[str]
    availability: str = ''
    actor: str = ''
    expression_template: str = ''


def _fold(value):
    return str(value or '').strip().casefold().replace('_', '')


def _scope_rank(symbol, class_name, spec, hero_tree):
    if symbol.hero_tree is not None:
        return 3 if symbol.hero_tree == hero_tree else -1
    if symbol.spec is not None:
        return 2 if symbol.spec == spec and symbol.class_name == class_name else -1
    if symbol.class_name is not None:
        return 1 if symbol.class_name == class_name else -1
    return 0


def _spell_details(spell_ids, wow_build=None, *, branch='retail', class_name='', spec=''):
    from botend.services.wow_spell_display import SpellDisplayReader
    reader = SpellDisplayReader(branch)
    reader.preload(spell_ids)
    details = {}
    for sid in spell_ids:
        row = reader.get(sid, class_name=class_name, spec_name=spec)
        details[sid] = {'zh': row.get('name_zh', ''), 'en': row.get('name_en', ''),
                        'description': row.get('description_zh') or row.get('description', ''),
                        'icon': row.get('icon', '')}
    return details


def _symbol_preference(symbol):
    """同一字段有重叠归属时选择人工或信息更完整的一条。"""
    source_rank = {
        SimcAplSymbol.SOURCE_MANUAL: 3,
        SimcAplSymbol.SOURCE_SIMC_MANIFEST: 2,
        SimcAplSymbol.SOURCE_SYSTEM_APL: 1,
    }.get(symbol.source, 0)
    return (
        source_rank,
        bool(str(symbol.name_zh or '').strip()),
        bool(str(symbol.name_en or '').strip()),
        symbol.spell_id is not None or symbol.trait_id is not None,
        symbol.updated_at,
        symbol.id,
    )


def query_visible_symbols(class_name, spec, hero_tree=None, *,
                          simc_revision=None, wow_build=None):
    """按 global→class→spec→hero 合并无版本字段及其归属绑定。

    两个版本参数仅为兼容旧调用签名保留，绝不参与数据库筛选。
    """
    class_name = str(class_name or '').strip().lower()
    spec = str(spec or '').strip().lower()
    hero_tree = str(hero_tree or '').strip().lower() or None
    candidates = SimcAplSymbolScope.objects.select_related('symbol').filter(
        is_active=True, symbol__is_active=True,
    ).filter(
        Q(class_name__isnull=True) |
        Q(class_name=class_name, spec__isnull=True) |
        Q(class_name=class_name, spec=spec, hero_tree__isnull=True) |
        Q(class_name=class_name, spec=spec, hero_tree=hero_tree)
    )
    selected = {}
    ranks = {}
    for symbol in candidates:
        identity = (symbol.token, symbol.symbol_kind)
        rank = _scope_rank(symbol, class_name, spec, hero_tree)
        if rank < 0:
            continue
        current = selected.get(identity)
        if (rank > ranks.get(identity, -1) or
                (rank == ranks.get(identity, -1) and current is not None and
                 _symbol_preference(symbol) > _symbol_preference(current))):
            selected[identity], ranks[identity] = symbol, rank
    return list(selected.values())


def query_symbol_catalog(simc_revision, wow_build, class_name, spec,
                         hero_tree=None, search=None, spec_id=None, talent_version=None, branch='retail'):
    """合并当前分支中央展示资料；构建参数仅兼容旧签名。"""
    class_name = str(class_name or '').strip().lower()
    spec = str(spec or '').strip().lower()
    hero_tree = str(hero_tree or '').strip().lower() or None
    selected_symbols = query_visible_symbols(
        class_name, spec, hero_tree,
        simc_revision=simc_revision, wow_build=wow_build,
    )

    from botend.wow.talents.versioning import TalentVersionResolver
    from botend.wow.talents.metadata import TalentMetadataProvider
    if talent_version is not None:
        if getattr(talent_version, 'pk', None) is None:
            talent_version = WowTalentVersion.objects.get(pk=talent_version)
        branch = talent_version.branch
    try:
        talent_version = TalentVersionResolver.resolve(version_key=branch)
    except ValueError:
        talent_version = None
    talent_rows = []
    if talent_version is not None:
        talent_rows = list(WowTalentNodeMetadata.objects.filter(
            class_name__iexact=class_name, spec_name__iexact=spec,
            talent_version=talent_version,
        ).exclude(spell_id__isnull=True).order_by('id'))
    if talent_version:
        provider = TalentMetadataProvider(talent_version=talent_version)
        for row in provider._prime_display(talent_rows):
            display = provider._as_dict(row)
            for field in ('name', 'name_zh', 'description', 'description_zh', 'icon'):
                setattr(row, field, display.get(field, ''))
    spell_ids = {s.spell_id for s in selected_symbols if s.spell_id is not None}
    spell_ids.update(row.spell_id for row in talent_rows)

    # No audited slug→spec-id map exists here. An authoritative Blizzard id may
    # be supplied by the caller; this service deliberately never guesses one.
    mapped_spell_ids = set()
    if spec_id is not None:
        maps = WowSpecSpellMapSnapshot.objects.filter(
            spec_id=spec_id, branch=spell_branch(branch),
        )
        mapped_spell_ids.update(maps.values_list('spell_id', flat=True))
        spell_ids.update(mapped_spell_ids)
    details = _spell_details(spell_ids, branch=branch, class_name=class_name, spec=spec)
    talents_by_spell = {}
    for talent in talent_rows:
        talents_by_spell.setdefault(talent.spell_id, talent)

    items = []
    bound_spell_ids = set()
    for symbol in selected_symbols:
        sid = symbol.spell_id
        if sid is not None:
            bound_spell_ids.add(sid)
        spell = details.get(sid, {})
        talent = talents_by_spell.get(sid)
        en = spell.get('en') or (talent.name if talent else '') or symbol.name_en or symbol.token
        zh = spell.get('zh') or (talent.name_zh if talent else '') or symbol.name_zh
        label = zh or en or symbol.token or (f'Spell {sid}' if sid else '')
        metadata = symbol.metadata if isinstance(symbol.metadata, dict) else {}
        coverage = metadata.get('source_coverage')
        coverage = coverage if isinstance(coverage, dict) else {}
        insertable = coverage.get('insertable') is not False
        reason = None if insertable else (
            str(coverage.get('insert_reason') or '').strip() or '该源码字段不适合直接插入当前 APL 上下文'
        )
        items.append(CatalogItem(
            token=symbol.token, kind=symbol.symbol_kind, spell_id=sid,
            name=label, name_en=en, description=(getattr(talent, 'description_zh', '') or
                spell.get('description') or getattr(talent, 'description', '')),
            icon=getattr(talent, 'icon', '') or spell.get('icon', ''), insertable=insertable, reason=reason,
            source=symbol.source, class_name=symbol.class_name, spec=symbol.spec,
            hero_tree=symbol.hero_tree,
            availability=str(coverage.get('availability') or ''),
            actor=str(coverage.get('actor') or ''),
            expression_template=str(metadata.get('apl_expression_template') or ''),
        ))

    for talent in talent_rows:
        if talent.spell_id in bound_spell_ids:
            continue
        spell = details.get(talent.spell_id, {})
        en = spell.get('en') or talent.name
        zh = spell.get('zh') or talent.name_zh
        items.append(CatalogItem(
            token=None, kind='talent' if talent.tree_type != 'hero' else 'hero_tree',
            spell_id=talent.spell_id,
            name=zh or en or f'Spell {talent.spell_id}', name_en=en,
            description=talent.description_zh or spell.get('description') or talent.description,
            icon=talent.icon, insertable=False, reason=UNBOUND_REASON, source='wago',
            class_name=class_name, spec=spec, hero_tree=None,
        ))

    talent_spell_ids = {talent.spell_id for talent in talent_rows}
    for spell_id in sorted(mapped_spell_ids - bound_spell_ids - talent_spell_ids):
        spell = details.get(spell_id, {})
        en, zh = spell.get('en', ''), spell.get('zh', '')
        items.append(CatalogItem(
            token=None, kind='action', spell_id=spell_id,
            name=zh or en or f'Spell {spell_id}', name_en=en,
            description=spell.get('description', ''), icon='', insertable=False,
            reason=UNBOUND_REASON, source='wago', class_name=class_name, spec=spec,
            hero_tree=None,
        ))

    if search:
        needle = str(search).casefold()
        items = [item for item in items if needle in ' '.join((
            item.token or '', item.name, item.name_en,
            str(item.spell_id or ''), item.description,
        )).casefold()]
    return sorted(items, key=lambda item: (item.name.casefold(), item.kind, item.token or ''))
