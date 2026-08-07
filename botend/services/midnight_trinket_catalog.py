"""Auditable 12.1 PTR trinket catalog derived from exact Wago DB2 builds."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from botend.services.simc_player_config import SUPPORTED_SIMC_SPEC_IDENTITIES


PTR_PRODUCT = 'wowt'
PTR_BASELINE_BUILD = '12.0.5.67602'
PTR_TARGET_BUILD = '12.1.0.69111'
PTR_EXPECTED_ITEM_LEVELS = {
    268292: 219,
    270160: 219, 270161: 219, 270162: 219, 270163: 219,
    270164: 219, 270165: 219, 270166: 219, 270167: 219,
    270168: 219, 270169: 219, 270170: 219, 270171: 219,
    270173: 219, 270174: 219, 270175: 219,
    270555: 259, 270556: 259, 270557: 259, 270558: 259, 270559: 259,
    270602: 298, 270603: 298, 270604: 298, 270605: 298, 270606: 298,
    273649: 59,
    273794: 219, 273795: 219, 273796: 219, 273797: 219,
    274493: 197, 274494: 197, 274495: 197, 274496: 197,
    274497: 197, 274498: 197, 274499: 197,
    274890: 100, 274891: 100, 274892: 100, 274893: 100,
    280047: 197, 280091: 197, 280097: 259, 280118: 259,
    280123: 197, 280376: 250, 280377: 250,
}
PTR_EXPECTED_UNRESOLVED_IDS = frozenset({
    267631, 270172, 274370, 274371, 277735, 279190,
})
PTR_DIFF_FILTER = (
    'target Item.InventoryType == 12 and ID absent from baseline trinket IDs'
)
PTR_EXCLUDED_SPEC_KEYS = frozenset({
    'druid_restoration', 'evoker_augmentation', 'evoker_preservation',
    'monk_mistweaver', 'paladin_holy', 'priest_discipline', 'priest_holy',
    'shaman_restoration',
})
PTR_TARGET_ITEM_LEVELS_BY_SOURCE = {
    'delve_or_open_world': (308, 321),
    'mythic_dungeon': (321, 334),
    'raid': (321, 334),
    'pvp': (321,),
}
PTR_DEFAULT_SCENARIOS = (
    {
        'key': 'castingpatchwerk',
        'name': 'Casting Patchwerk (1 Target / 300s)',
        'simulation_params': {
            'iterations': 10000, 'fight_style': 'CastingPatchwerk',
            'desired_targets': 1, 'max_time': 300,
        },
    },
    {
        'key': 'castingpatchwerk5',
        'name': 'Casting Patchwerk (5 Targets / 40s)',
        'simulation_params': {
            'iterations': 10000, 'fight_style': 'CastingPatchwerk',
            'desired_targets': 5, 'max_time': 40,
        },
    },
    {
        'key': 'castingpatchwerk20',
        'name': 'Casting Patchwerk (20 Targets / 40s)',
        'simulation_params': {
            'iterations': 10000, 'fight_style': 'CastingPatchwerk',
            'desired_targets': 20, 'max_time': 40,
        },
    },
)


@dataclass(frozen=True)
class TrinketItem:
    item_id: int
    name: str
    name_enus: str
    item_level: int
    inventory_type: int
    source_category: str
    is_special_raid_item: bool
    source_evidence: str


@dataclass(frozen=True)
class TrinketVariant:
    candidate_key: str
    item_id: int
    name: str
    item_level: int
    spec_keys: tuple[str, ...]


@dataclass(frozen=True)
class PtrTrinketCatalog:
    product: str
    baseline_build: str
    target_build: str
    spec_keys: tuple[str, ...]
    items: tuple[TrinketItem, ...]
    unresolved_item_ids: tuple[int, ...]
    variants: tuple[TrinketVariant, ...]


def _candidate_key(item_id: int, item_level: int) -> str:
    digest = hashlib.sha256(
        f'ptr-12.1:{PTR_TARGET_BUILD}:{item_id}:{item_level}'.encode()
    ).hexdigest()[:24]
    return f'trinket-{digest}'


def _positive_int(value, field):
    if type(value) is not int or value <= 0:
        raise ValueError(f'{field} must be a positive integer')
    return value


def parse_ptr_12_1_catalog(payload: dict[str, Any]) -> PtrTrinketCatalog:
    if not isinstance(payload, dict) or payload.get('schema_version') != 1:
        raise ValueError('unsupported PTR trinket catalog schema')
    source = payload.get('source')
    if not isinstance(source, dict):
        raise ValueError('missing Wago DB2 provenance')
    if (
        source.get('provider') != 'wago_db2'
        or source.get('product') != PTR_PRODUCT
        or source.get('baseline_build') != PTR_BASELINE_BUILD
        or source.get('target_build') != PTR_TARGET_BUILD
        or source.get('tables') != ['Item', 'ItemSparse']
        or source.get('join') != 'Item.ID = ItemSparse.ID'
        or source.get('filter') != PTR_DIFF_FILTER
    ):
        raise ValueError('unexpected Wago DB2 build or table contract')

    raw_items = payload.get('items')
    raw_unresolved = payload.get('unresolved')
    if not isinstance(raw_items, list) or not isinstance(raw_unresolved, list):
        raise ValueError('items and unresolved must be lists')

    items = []
    seen_ids = set()
    for raw in raw_items:
        if not isinstance(raw, dict) or set(raw) != {
            'item_id', 'name_zhcn', 'name_enus', 'item_level', 'inventory_type',
            'source_category', 'is_special_raid_item', 'source_evidence',
        }:
            raise ValueError('invalid complete Item/ItemSparse record')
        item_id = _positive_int(raw['item_id'], 'item_id')
        item_level = _positive_int(raw['item_level'], 'item_level')
        if item_id in seen_ids or raw['inventory_type'] != 12:
            raise ValueError('duplicate ID or non-trinket InventoryType')
        if not isinstance(raw['name_zhcn'], str) or not raw['name_zhcn'].strip():
            raise ValueError('complete ItemSparse record requires a zhCN name')
        if not isinstance(raw['name_enus'], str) or not raw['name_enus'].strip():
            raise ValueError('complete ItemSparse record requires an enUS name')
        source_category = raw['source_category']
        if source_category not in PTR_TARGET_ITEM_LEVELS_BY_SOURCE:
            raise ValueError('unsupported trinket source category')
        if type(raw['is_special_raid_item']) is not bool:
            raise ValueError('is_special_raid_item must be a boolean')
        if raw['is_special_raid_item'] and source_category != 'raid':
            raise ValueError('only raid trinkets can be marked special')
        if not isinstance(raw['source_evidence'], str) or not raw['source_evidence'].strip():
            raise ValueError('trinket source evidence is required')
        seen_ids.add(item_id)
        items.append(TrinketItem(
            item_id=item_id,
            name=raw['name_zhcn'].strip(),
            name_enus=raw['name_enus'].strip(),
            item_level=item_level,
            inventory_type=12,
            source_category=source_category,
            is_special_raid_item=raw['is_special_raid_item'],
            source_evidence=raw['source_evidence'].strip(),
        ))

    unresolved_ids = []
    for raw in raw_unresolved:
        if (
            not isinstance(raw, dict)
            or set(raw) != {'item_id', 'inventory_type', 'reason'}
            or raw.get('inventory_type') != 12
            or raw.get('reason') != 'missing_target_itemsparse'
        ):
            raise ValueError('invalid unresolved Item record')
        item_id = _positive_int(raw['item_id'], 'unresolved.item_id')
        if item_id in seen_ids:
            raise ValueError('complete and unresolved Item IDs overlap')
        seen_ids.add(item_id)
        unresolved_ids.append(item_id)

    actual_item_levels = {item.item_id: item.item_level for item in items}
    if (
        actual_item_levels != PTR_EXPECTED_ITEM_LEVELS
        or set(unresolved_ids) != PTR_EXPECTED_UNRESOLVED_IDS
    ):
        raise ValueError('PTR exact-build trinket IDs or ItemSparse.ItemLevel changed')

    spec_keys = tuple(sorted(
        f'{class_name}_{spec}'
        for class_name, spec in SUPPORTED_SIMC_SPEC_IDENTITIES
        if f'{class_name}_{spec}' not in PTR_EXCLUDED_SPEC_KEYS
    ))
    items = tuple(sorted(items, key=lambda item: item.item_id))
    variants = tuple(
        TrinketVariant(
            candidate_key=_candidate_key(item.item_id, target_item_level),
            item_id=item.item_id,
            name=item.name,
            item_level=target_item_level,
            spec_keys=spec_keys,
        )
        for item in items
        for target_item_level in (
            PTR_TARGET_ITEM_LEVELS_BY_SOURCE[item.source_category]
            + ((344,) if item.is_special_raid_item else ())
        )
    )
    return PtrTrinketCatalog(
        product=PTR_PRODUCT,
        baseline_build=PTR_BASELINE_BUILD,
        target_build=PTR_TARGET_BUILD,
        spec_keys=spec_keys,
        items=items,
        unresolved_item_ids=tuple(sorted(unresolved_ids)),
        variants=variants,
    )


def build_ptr_12_1_panel_payload(
    catalog: PtrTrinketCatalog,
    user_id: int,
    slug: str = 'ptr-12-1-mythic-trinkets',
):
    from botend.services.simc_benchmark_config import resolve_default_benchmark_resources

    resources = resolve_default_benchmark_resources(catalog.spec_keys, user_id)
    specs = []
    for order, spec_key in enumerate(catalog.spec_keys):
        selected = resources[spec_key]
        specs.append({
            'class_name': spec_key.split('_', 1)[0],
            'spec_key': spec_key,
            'label': spec_key.replace('_', ' ').title(),
            'apl_id': selected['apl'].pk,
            'template_id': selected['template'].pk,
            'backend_id': selected['backend'].pk,
            'profiles': [{
                'profile_id': selected['profile'].pk,
                'label': selected['profile'].name,
            }],
            'display_order': order,
        })

    candidates = []
    for order, variant in enumerate(catalog.variants):
        candidates.append({
            'key': variant.candidate_key,
            'label': f'{variant.name} ({variant.item_level})',
            'candidate_type': 'gear_swap',
            'params': {
                'slot': 'trinket1',
                'raw_value': f'id={variant.item_id},ilevel={variant.item_level}',
                'benchmark_profile': {
                    'kind': 'trinket_standard_reference',
                    'item_level': 240,
                },
            },
            'spec_keys': list(variant.spec_keys),
            'source_label': f'Wago DB2 {catalog.target_build}; source-specific target ilevels',
            'display_order': order,
        })
    return {
        'name': '12.1 PTR New Trinkets',
        'slug': slug,
        'description': (
            f'All new trinket IDs in wowt {catalog.target_build} versus '
            f'{catalog.baseline_build}; source-specific target ilevels are used '
            '(delve/open world 308/321, Mythic+ 321/334, raid 321/334, PvP 321; '
            'explicit special raid items additionally 344).'
        ),
        'is_active': True,
        'is_public': True,
        'schedule_enabled': False,
        'interval_seconds': 86400,
        'specs': specs,
        'scenarios': [
            dict(scenario, simulation_params=dict(scenario['simulation_params']))
            for scenario in PTR_DEFAULT_SCENARIOS
        ],
        'candidates': candidates,
    }


def ptr_12_1_matrix_plan(payload, unresolved_item_ids=()):
    specs = []
    total_cases = total_runs = 0
    for spec in payload['specs']:
        applicable = [
            candidate['key'] for candidate in payload['candidates']
            if not candidate['spec_keys'] or spec['spec_key'] in candidate['spec_keys']
        ]
        cases = len(spec['profiles']) * len(payload['scenarios'])
        runs = cases * (1 + len(applicable))
        total_cases += cases
        total_runs += runs
        specs.append({
            'spec_key': spec['spec_key'],
            'apl_id': spec['apl_id'],
            'template_id': spec['template_id'],
            'backend_id': spec['backend_id'],
            'profile_id': spec['profiles'][0]['profile_id'],
            'candidate_keys': applicable,
            'case_count': cases,
            'run_count': runs,
        })
    return {
        'slug': payload['slug'],
        'spec_count': len(specs),
        'scenario_count': len(payload['scenarios']),
        'candidate_count': len(payload['candidates']),
        'case_count': total_cases,
        'run_count': total_runs,
        'unresolved_item_ids': list(unresolved_item_ids),
        'specs': specs,
    }
