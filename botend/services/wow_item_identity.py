"""Exact Item + ItemSparse identity in the central catalog, never in variants.

A missing exact/branch fact is an error, not permission to use the base row.
The no-store, no-build path alone retains the legacy base contract.
"""
from copy import copy, deepcopy
from urllib.parse import parse_qs, urlsplit
from typing import NoReturn
import hashlib
import json
import re

from django.core.exceptions import ValidationError
from django.db import transaction
from botend.models import WowItemSnapshot
from botend.services.wow_item_catalog_import import INVENTORY_SLOTS

META_KEY = 'item_identity_by_build'
BUILD_RE = re.compile(r'[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+')
IDENTITY_FIELDS = ('inventory_type', 'slot_key', 'item_class_id', 'item_subclass_id',
                   'allowable_class_mask', 'name')
# SimC engine/dbc/data_enums.hh item_mod_type (midnight). Unknown/new enum
# values require review; they must not silently become "known no primary stat".
KNOWN_STAT_MODIFIERS = frozenset((-1, 0, 1, *range(3, 8), *range(12, 58),
                                *range(59, 67), *range(71, 75)))


def build_key(build):
    if not isinstance(build, str) or not BUILD_RE.fullmatch(build):
        raise ValidationError('装备 game_build 必须是精确四段数字版本')
    return tuple(map(int, build.split('.')))


def _invalid() -> NoReturn:
    raise ValidationError('中央装备 Item/ItemSparse 来源身份不一致或不完整')


def _canonical_json(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False,
                          separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError):
        _invalid()


def identity_reference(fact, *, is_ptr):
    """Validate every source identity before producing a detached compact ref."""
    if type(is_ptr) is not bool or not isinstance(fact, dict):
        _invalid()
    item_id, build = fact.get('item_id'), fact.get('game_build')
    build_key(build)
    if (type(fact.get('schema_version')) is not int or fact['schema_version'] != 1
            or type(item_id) is not int or item_id <= 0):
        _invalid()
    source = fact.get('source')
    if not isinstance(source, dict) or source.get('provider') != 'wago_db2':
        _invalid()
    pages = source.get('evidence')
    if not isinstance(pages, list) or len(pages) != 2:
        _invalid()
    tables, locale = {}, None
    for page in pages:
        if not isinstance(page, dict):
            _invalid()
        table = page.get('table')
        if table not in ('Item', 'ItemSparse') or table in tables:
            _invalid()
        if page.get('game_build') != build or page.get('locale') not in ('enUS', 'zhCN'):
            _invalid()
        locale = locale or page['locale']
        if page['locale'] != locale:
            _invalid()
        filters = {'build': build, 'locale': locale, 'filter': {'ID': f'exact:{item_id}'}}
        if page.get('filters') != filters:
            _invalid()
        if not isinstance(page.get('url'), str):
            _invalid()
        try:
            url = urlsplit(page['url'])
        except ValueError:
            _invalid()
        query = parse_qs(url.query, keep_blank_values=True)
        expected = {'build': [build], 'locale': [locale], 'filter[ID]': [f'exact:{item_id}']}
        if query.get('page') == ['1']:
            query.pop('page')
        if (url.scheme != 'https' or url.netloc != 'wago.tools' or url.path != f'/db2/{table}'
                or url.fragment or query != expected):
            _invalid()
        rows, pagination = page.get('rows'), page.get('pagination')
        if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
                or type(rows[0].get('ID')) is not int or rows[0]['ID'] != item_id
                or not isinstance(pagination, dict)
                or any(type(pagination.get(k)) is not int or pagination[k] != 1
                       for k in ('current_page', 'last_page', 'total', 'from', 'to'))
                or pagination.get('next_page_url') is not None):
            _invalid()
        tables[table] = rows[0]
    item, sparse = tables['Item'], tables['ItemSparse']
    for row, keys in ((item, ('InventoryType', 'ClassID', 'SubclassID')),
                      (sparse, ('InventoryType', 'AllowableClass', 'OverallQualityID'))):
        if any(type(row.get(key)) is not int for key in keys):
            _invalid()
    inventory = item['InventoryType']
    if (item['ClassID'] < 0 or item['SubclassID'] < 0 or sparse['AllowableClass'] < -1
            or not 0 <= sparse['OverallQualityID'] <= 8):
        _invalid()
    if inventory != sparse['InventoryType'] or inventory not in INVENTORY_SLOTS:
        _invalid()
    expected = dict(inventory_type=inventory, slot_key=INVENTORY_SLOTS[inventory],
                    item_class_id=item['ClassID'], item_subclass_id=item['SubclassID'],
                    allowable_class_mask=sparse['AllowableClass'], name=sparse.get('Display_lang'))
    if not isinstance(expected['name'], str) or not expected['name'].strip():
        _invalid()
    if any(type(fact.get(key)) is not type(value) or fact[key] != value for key, value in expected.items()):
        _invalid()
    # Primary stat *identity* is in ItemSparse. These are not scaled stat values.
    primary_mods = {3: ['agility'], 4: ['strength'], 5: ['intellect'],
                    71: ['agility', 'strength', 'intellect'], 72: ['agility', 'strength'],
                    73: ['agility', 'intellect'], 74: ['strength', 'intellect']}
    # ItemSparse has ten stat cells. Missing cells mean unknown, not known empty.
    stat_keys = {f'StatModifier_bonusStat_{n}' for n in range(10)}
    if ({key for key in sparse if key.startswith('StatModifier_bonusStat_')} != stat_keys
            or any(type(sparse.get(key)) is not int or sparse[key] not in KNOWN_STAT_MODIFIERS
                   for key in stat_keys)):
        _invalid()
    primary = sorted({stat for key in stat_keys for stat in primary_mods.get(sparse[key], [])})
    digest = hashlib.sha256(_canonical_json(fact).encode()).hexdigest()
    return {'schema_version': 1, 'item_id': item_id, 'game_build': build, 'is_ptr': is_ptr,
            **expected, 'name_zh': expected['name'] if locale == 'zhCN' else '',
            'primary_stat_options': primary, 'quality': sparse['OverallQualityID'],
            'fact_hash': digest}


@transaction.atomic
def merge_item_identity(facts, *, is_ptr):
    unique = {}
    for fact in facts:
        ref = identity_reference(fact, is_ptr=is_ptr)
        key = (ref['item_id'], ref['game_build'])
        if key in unique and unique[key][1]['fact_hash'] != ref['fact_hash']:
            raise ValidationError('同批同构建装备身份事实冲突')
        unique[key] = (fact, ref)
    refs = [ref for _, ref in unique.values()]
    objects = {item.item_id: item for item in WowItemSnapshot.objects.select_for_update()
               .filter(item_id__in=[ref['item_id'] for ref in refs])}
    if set(objects) != {ref['item_id'] for ref in refs}:
        raise ValidationError('待补采装备不在中央目录中')
    changed = set()
    for fact, ref in unique.values():
        item = objects[ref['item_id']]
        metadata = deepcopy(item.metadata or {})
        entries = metadata.setdefault(META_KEY, {})
        if not isinstance(entries, dict):
            _invalid()
        entry = {'is_ptr': is_ptr, 'fact': deepcopy(fact)}
        existing = entries.get(ref['game_build'])
        if existing is not None and (not isinstance(existing, dict)
                                     or existing.get('is_ptr') is not is_ptr):
            raise ValidationError('同构建的装备来源分支冲突')
        if _canonical_json(existing) != _canonical_json(entry):
            entries[ref['game_build']] = entry
            item.metadata = metadata
            changed.add(item.item_id)
    for item_id in changed:
        objects[item_id].save(update_fields=['metadata'])
    return {'changed_item_ids': sorted(changed), 'references': refs}


def has_identity_store(item):
    return item is not None and META_KEY in (item.metadata or {})


def resolve_item_identity(item, *, game_build='', is_ptr=None):
    if game_build:
        build_key(game_build)
    if type(is_ptr) is not bool:
        raise ValidationError('必须显式声明装备正式服/PTR分支')
    if not has_identity_store(item):
        if game_build:
            raise ValidationError('缺少精确构建装备身份')
        return None
    entries = item.metadata[META_KEY]
    if not isinstance(entries, dict):
        _invalid()
    refs = []
    for build, entry in entries.items():
        if game_build and build != game_build:
            continue
        build_key(build)
        if not isinstance(entry, dict) or type(entry.get('is_ptr')) is not bool:
            _invalid()
        if entry['is_ptr'] != is_ptr:
            continue
        ref = identity_reference(entry.get('fact'), is_ptr=is_ptr)
        if ref['item_id'] != item.item_id or ref['game_build'] != build:
            _invalid()
        refs.append(ref)
    if not refs:
        raise ValidationError('缺少同分支精确构建装备身份')
    return max(refs, key=lambda ref: build_key(ref['game_build']))


def resolve_display_identity(item, *, game_build='', is_ptr=None):
    """Legacy display callers know the exact build, not necessarily its branch.

    Only that exact central entry may supply an omitted branch. Execution callers
    still use resolve_item_identity and must explicitly declare their branch.
    """
    if is_ptr is None and game_build:
        build_key(game_build)
        entries = (item.metadata or {}).get(META_KEY)
        entry = entries.get(game_build) if isinstance(entries, dict) else None
        if not isinstance(entry, dict) or type(entry.get('is_ptr')) is not bool:
            raise ValidationError('缺少同分支精确构建装备身份')
        is_ptr = entry['is_ptr']
    return resolve_item_identity(item, game_build=game_build, is_ptr=is_ptr)


def variant_identity(item, variant):
    """Variant build is exact; its branch comes from the explicit central entry."""
    if getattr(item, '_resolved_identity', None):
        return item._resolved_identity
    if not has_identity_store(item):
        return None
    build = str(getattr(variant, 'game_build', '') or '')
    entry = (item.metadata[META_KEY] or {}).get(build, {})
    return resolve_item_identity(item, game_build=build, is_ptr=entry.get('is_ptr'))


def project_item_identity(item, ref):
    if ref is None:
        return item
    projected = copy(item)
    for field in IDENTITY_FIELDS:
        setattr(projected, field, ref[field])
    projected.name_zh = ref.get('name_zh', '')
    projected.description = projected.description_zh = ''
    projected.eligible_specs = []
    projected.effect_refs = []
    projected.quality = ref.get('quality', 0)
    projected.metadata = {'primary_stat_options': list(ref['primary_stat_options'])}
    projected._resolved_identity = deepcopy(ref)
    return projected


def validate_swap_identity_context(swap, *, trusted_item_identity=False):
    """Validate the task boundary without consulting or enriching live catalog data.

    Only internal plan/run callers may carry an already frozen reference. The
    flag is a Python call argument, never a candidate/request JSON field.
    """
    if 'game_build' in swap:
        build_key(swap['game_build'])
    if 'is_ptr' in swap and type(swap['is_ptr']) is not bool:
        raise ValidationError('装备 is_ptr 必须严格为布尔值')
    if 'item_identity' not in swap:
        return
    if not trusted_item_identity:
        raise ValidationError('客户端不能提交服务端冻结的 item_identity')
    ref = swap['item_identity']
    if (not isinstance(ref, dict) or type(ref.get('schema_version')) is not int
            or ref['schema_version'] != 1
            or type(ref.get('item_id')) is not int or ref['item_id'] <= 0
            or type(swap.get('item_id')) is not int
            or type(ref.get('is_ptr')) is not bool
            or any(ref.get(key) != swap.get(key) for key in ('item_id', 'game_build', 'is_ptr'))
            or any(type(ref.get(key)) is not int for key in (
                'inventory_type', 'item_class_id', 'item_subclass_id', 'allowable_class_mask', 'quality'))
            or ref['inventory_type'] not in INVENTORY_SLOTS
            or ref.get('slot_key') != INVENTORY_SLOTS[ref['inventory_type']]
            or ref['item_class_id'] < 0 or ref['item_subclass_id'] < 0
            or ref['allowable_class_mask'] < -1 or not 0 <= ref['quality'] <= 8
            or not isinstance(ref.get('name'), str) or not ref['name'].strip()
            or not isinstance(ref.get('name_zh'), str)
            or not isinstance(ref.get('primary_stat_options'), list)
            or any(type(value) is not str or value not in ('strength', 'agility', 'intellect')
                   for value in ref['primary_stat_options'])
            or not isinstance(ref.get('fact_hash'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', ref['fact_hash'])):
        raise ValidationError('冻结装备身份与候选构建或分支冲突')
    build_key(ref['game_build'])
    raw_id = re.search(r'(?:^|,)\s*id=(\d+)(?:,|$)', str(swap.get('raw_value') or ''))
    if not raw_id or int(raw_id.group(1)) != ref['item_id']:
        raise ValidationError('冻结装备身份与装备行冲突')


def freeze_equipment_identity(params, items):
    """Only called while constructing NEW inputs. Never backfill stored inputs."""
    from simc_equipment_control import candidate_swaps
    result = deepcopy(params)
    for swap in candidate_swaps(result):
        item = items.get(swap.get('item_id'))
        if item is None:
            continue
        ref = resolve_item_identity(item, game_build=swap.get('game_build', ''),
                                    is_ptr=swap.get('is_ptr', False))
        if ref:
            swap.update(game_build=ref['game_build'], is_ptr=ref['is_ptr'], item_identity=ref)
    return result
