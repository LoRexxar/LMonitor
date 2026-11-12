"""统计文件按范围发布；概览与详情共用一个原子替换的入口。"""
from contextlib import nullcontext
from pathlib import Path
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import re
import time
import uuid
from utils.log import logger

from botend.services.simc_benchmark_result_snapshot import _load
from botend.services.stats_storage_codec import pack_detail, unpack_detail


SCHEMA = 1


def _bounded_canonical_chunks(value, encoder, *, byte_budget=256 * 1024, node_budget=4096):
    """Use the C encoder on bounded subtrees, preserving canonical JSON bytes.

    Never materialize a whole large publication just to hash it. The cheap
    iterative bound limits both encoded bytes and temporary traversal references.
    Oversize scalar strings keep the stdlib encoder's existing scalar behavior.
    """
    def fits(root):
        remaining, nodes, pending = byte_budget, node_budget, [root]
        while pending:
            item = pending.pop()
            nodes -= 1
            if nodes < 0:
                return False
            if isinstance(item, str):
                remaining -= 6 * len(item) + 2  # Worst-case JSON escaping.
            elif isinstance(item, dict):
                if len(item) * 2 > nodes:
                    return False
                remaining -= 2 + 2 * len(item)
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, (list, tuple)):
                if len(item) > nodes:
                    return False
                remaining -= 2 + len(item)
                pending.extend(item)
            elif isinstance(item, int):
                remaining -= item.bit_length() + 8
            elif isinstance(item, (Decimal, date, datetime)):
                remaining -= 6 * len(str(item)) + 4
            else:
                remaining -= 32
            if remaining < 0:
                return False
        return True

    active = set()
    def chunks(item):
        container = isinstance(item, (dict, list, tuple))
        if not container or fits(item):
            # encode() selects the stdlib C encoder; iterencode() defaults to
            # Python recursion and millions of tiny hash.update calls.
            yield encoder.encode(item)
            return
        identity = id(item)
        if identity in active:
            raise ValueError('Circular reference detected')
        active.add(identity)
        try:
            if isinstance(item, dict):
                yield '{'
                for index, key in enumerate(sorted(item)):
                    if index:
                        yield ','
                    # Let the same encoder handle legal non-string JSON keys.
                    yield encoder.encode({key: None})[1:-5]
                    yield from chunks(item[key])
                yield '}'
            else:
                yield '['
                for index, child in enumerate(item):
                    if index:
                        yield ','
                    yield from chunks(child)
                yield ']'
        finally:
            active.remove(identity)
    yield from chunks(value)


def content_digest(value):
    """Canonical content hash using the publisher's JSON scalar encoding."""
    def encode(obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        raise TypeError(f'Unsupported snapshot scalar: {type(obj).__name__}')
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(sort_keys=True, ensure_ascii=False,
                               separators=(',', ':'), allow_nan=False, default=encode)
    for fragment in _bounded_canonical_chunks(value, encoder):
        digest.update(fragment.encode('utf-8'))
    return digest.hexdigest()


def _summary(detail):
    """保留概览卡片全部指标，剥离天赋、装备与人物明细。"""
    fields = ('dungeon_id', 'dungeon_name', 'boss_id', 'boss_name', 'name', 'short_name',
              'sample_size', 'dps', 'keystone', 'clear_time', 'kill_time',
              'faction_distribution', 'updated_at', 'last_updated', 'data_contract')
    return {key: detail[key] for key in fields if key in detail}


def publish_projection(path, payload, write_json, *, publication_guard=None):
    """Stage immutable bodies before the short DB-fenced index/ledger commit.

    The scope file lock still covers preparation, publication and rollback/GC.
    Source and metadata fences are rechecked immediately before making staged
    bodies visible; failed fences leave the old index untouched.
    """
    from .spec_stats_retention import publication, record_publication, _disable
    path = Path(path)
    guard = publication_guard if publication_guard is not None else nullcontext()
    if path.stem not in ('dungeon', 'raid'):
        with guard:
            return write_json(path, payload)
    prepared_started = time.monotonic()
    with publication(path.parent):
        from .simc_benchmark_result_snapshot import _load as load_previous
        previous = load_previous(path)
        staged = []
        def prepare(target, data):
            if Path(target) == path:
                staged.append(data)
            else:
                write_json(target, data)
        _publish_projection_locked(path, payload, prepare)
        prepared_at = time.monotonic()
        with guard:
            if staged:
                if len(staged) != 1:
                    raise ValueError('multiple staged publication indexes')
                write_json(path, staged[0])
            try:
                record_publication(path.parent)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                # Accounting failure must not turn an unrecorded publication into
                # permission to prune. Restore last-good where one existed.
                try:
                    if previous is not None:
                        from .simc_benchmark_result_snapshot import _write
                        _write(path, previous)
                finally:
                    _disable(path.parent, exc)
                raise
        logger.info('[SpecStatsPublish] %s prepare_seconds=%.3f fenced_commit_seconds=%.3f',
                    '/'.join(path.parts[-4:]), prepared_at - prepared_started,
                    time.monotonic() - prepared_at)


def _publish_projection_locked(path, payload, write_json):
    """先写完整不可变详情，再发布轻量入口；中途失败保留原入口。"""
    from botend.services.spec_overview_service import SpecOverviewService

    path = Path(path)
    module = path.stem
    if module not in ('dungeon', 'raid'):
        write_json(path, payload)
        return
    scope = list(path.parent.parts[-3:])
    details = []
    detail_hashes = {}
    detail_bodies = {}
    # 同一发布中的首领/副本共用一次元数据读取，缓存随本次发布结束释放。
    catalog_cache = {}

    def save(key, detail):
        if not re.fullmatch(r'(dungeon-(all|\d+)|raid-[45]-\d+)', key) or key in details:
            raise ValueError('统计详情范围缺失或重复')
        if not isinstance(detail, dict):
            raise ValueError('统计详情不是有效对象')
        detail = pack_detail(detail, catalog_cache=catalog_cache)
        details.append(key)
        detail_hashes[key] = content_digest(detail)
        detail_bodies[key] = detail

    index = {'_data_contract': payload.get('_data_contract'),
             'generated_at': payload.get('generated_at'),
             'updated_at': payload.get('updated_at') or SpecOverviewService._latest_timestamp(
                 payload.get('dungeons') if module == 'dungeon' else payload.get('zone_groups'))
                 or payload.get('generated_at')}
    if module == 'dungeon':
        index['dungeons'] = []
        for detail in payload.get('dungeons', []):
            save(f"dungeon-{detail.get('dungeon_id')}", detail)
            index['dungeons'].append(_summary(detail))
        if payload.get('summary') is not None:
            save('dungeon-all', payload['summary'])
    else:
        difficulties = payload.get('difficulties') or [
            {'difficulty': 5, 'zone_groups': payload.get('zone_groups', [])}]
        index['difficulties'] = []
        for item in difficulties:
            difficulty = item['difficulty']
            if difficulty not in (4, 5):
                raise ValueError('统计团本难度无效')
            zones = []
            for zone in item.get('zone_groups', []):
                compact = {key: zone[key] for key in ('zone_id', 'zone_name', 'zone_cn', 'name') if key in zone}
                compact['bosses'] = []
                for detail in zone.get('bosses', []):
                    save(f"raid-{difficulty}-{detail.get('boss_id')}", detail)
                    compact['bosses'].append(_summary(detail))
                zones.append(compact)
            index['difficulties'].append({'difficulty': difficulty, 'label': item.get('label'), 'zone_groups': zones})
        index['zone_groups'] = next((row['zone_groups'] for row in index['difficulties'] if row['difficulty'] == 5), [])
    # Hash the FULL supplied business payload, not only the compact overview.
    # Only top-level publication/check clocks are excluded. Nested fact times,
    # algorithm markers, labels and resolved display metadata remain dependencies.
    if module == 'raid':
        payload_hash = content_digest({'projection_version': 1, 'payload': {
            key: value for key, value in payload.items() if key not in ('generated_at', 'checked_at')}})
        old = read_projection(path)
        meta = old.get('_snapshot', {}) if isinstance(old, dict) else {}
        try:
            reusable = (isinstance(old, dict) and meta.get('payload_hash') == payload_hash
                        and meta.get('detail_hashes') == detail_hashes
                        and _validated_details(path, old, details) is not None)
        except (TypeError, ValueError):
            reusable = False
        if reusable:
            return
    generation = uuid.uuid4().hex
    for key, detail in detail_bodies.items():
        write_json(path.parent / 'stats-details' / generation / f'{key}.json', {
            'schema': SCHEMA, 'generation': generation, 'scope': scope,
            'key': key, 'data': detail,
        })
    index['_snapshot'] = {'schema': SCHEMA, 'module': module, 'generation': generation,
                          'scope': scope, 'details': details}
    if module == 'raid':
        index['_snapshot'].update(payload_hash=payload_hash, detail_hashes=detail_hashes,
                                  index_hash=content_digest({k: v for k, v in index.items() if k != '_snapshot'}))
    if payload.get('_input_fingerprint'):
        index['_snapshot'].update(input_fingerprint=payload['_input_fingerprint'],
                                  detail_hashes=detail_hashes)
    index['_snapshot'].update(detail_hashes=detail_hashes,
                              index_hash=content_digest({k: v for k, v in index.items() if k != '_snapshot'}))
    if payload.get('_talent_structure_token'):
        index['_snapshot']['talent_structure_token'] = payload['_talent_structure_token']
    write_json(path, index)


def reusable_projection(path, fingerprint, required_keys):
    """Validate the captured generation, including every required detail body.

    A readable overview alone is not evidence of a complete publication. No
    mutable timestamp is refreshed here and legacy generations rebuild once.
    """
    path = Path(path)
    index = read_projection(path)
    meta = index.get('_snapshot', {}) if isinstance(index, dict) else {}
    if meta.get('input_fingerprint') != fingerprint:
        return None
    return _validated_details(path, index, required_keys)


def _validated_details(path, index, required_keys):
    """Reuse only fully decodable bodies from one checksum-validated index."""
    meta = index.get('_snapshot', {}) if isinstance(index, dict) else {}
    required_keys = list(required_keys)
    if (meta.get('details') != required_keys or not meta.get('index_hash')
            or not isinstance(meta.get('detail_hashes'), dict)):
        return None
    try:
        _check_talent_structure(meta)
        catalog_cache = {}  # This validation call only; never retain mutable DB facts.
        for key in required_keys:
            # Validate and decode the same loaded body; discard each decoded
            # detail immediately rather than hydrating the whole publication.
            if _decoded_detail(path, meta, key, catalog_cache=catalog_cache) is None:
                return None
    except (ValueError, KeyError, TypeError):
        return None
    return index


def _check_talent_structure(meta):
    from .stats_talent_references import StaleTalentReferences
    from .stats_talent_dependencies import structure_token
    token = meta.get('talent_structure_token')
    if token and token != structure_token(meta['scope'][1]):
        raise StaleTalentReferences('统计天赋结构已变化')


def _decoded_detail(path, meta, key, *, catalog_cache=None):
    """One read/parse, envelope and content checks, then the real storage codec."""
    if (not isinstance(key, str) or not re.fullmatch(r'(dungeon-(all|\d+)|raid-[45]-\d+)', key)
            or key not in meta['details']):
        return None
    envelope = _load(path.parent / 'stats-details' / meta['generation'] / f'{key}.json')
    if (not isinstance(envelope, dict) or envelope.get('schema') != SCHEMA
            or envelope.get('generation') != meta['generation']
            or envelope.get('scope') != meta['scope'] or envelope.get('key') != key
            or not isinstance(envelope.get('data'), dict)):
        return None
    if ('detail_hashes' in meta
            and content_digest(envelope['data']) != meta['detail_hashes'].get(key)):
        raise ValueError('统计详情摘要不一致')
    return unpack_detail(envelope['data'], catalog_cache=catalog_cache)


def _valid_index_containers(payload, module):
    """合法 JSON 仍可能结构损坏；概览与详情都不能消费这样的入口。"""
    def valid_rows(rows, identity):
        return isinstance(rows, list) and all(
            isinstance(row, dict) and re.fullmatch(r'\d+', str(row.get(identity, '')))
            for row in rows
        )

    def valid_zones(zones):
        return isinstance(zones, list) and all(
            isinstance(zone, dict) and valid_rows(zone.get('bosses'), 'boss_id')
            for zone in zones
        )

    if module == 'dungeon':
        return valid_rows(payload.get('dungeons'), 'dungeon_id')
    if module == 'raid':
        difficulties = payload.get('difficulties')
        return (isinstance(difficulties, list) and all(
            isinstance(item, dict) and item.get('difficulty') in (4, 5)
            and valid_zones(item.get('zone_groups')) for item in difficulties
        ) and valid_zones(payload.get('zone_groups')))
    return False


def read_projection(path, detail_key=None):
    """旧完整文件仍只读兼容，新版只读取入口和一个所选详情。"""
    path = Path(path)
    payload = _load(path)
    if not isinstance(payload, dict):
        return None
    if '_snapshot' not in payload:
        return payload
    meta = payload['_snapshot']
    if (not isinstance(meta, dict) or meta.get('schema') != SCHEMA
            or meta.get('module') != path.stem or meta.get('scope') != list(path.parent.parts[-3:])
            or not re.fullmatch(r'[0-9a-f]{32}', str(meta.get('generation', '')))
            or not isinstance(meta.get('details'), list)
            or not _valid_index_containers(payload, path.stem)):
        return None
    # Pre-checksum sharded publications remain readable last-good data, but
    # cannot be reused. Once either digest field exists, require the pair.
    if 'index_hash' in meta or 'detail_hashes' in meta:
        try:
            if (not isinstance(meta.get('detail_hashes'), dict)
                    or meta.get('index_hash') != content_digest({
                        key: value for key, value in payload.items() if key != '_snapshot'})):
                return None
        except (ValueError, TypeError):
            return None
    if detail_key is None:
        return payload
    from .stats_talent_references import StaleTalentReferences
    try:
        _check_talent_structure(meta)
        detail = _decoded_detail(path, meta, detail_key)
    except StaleTalentReferences:
        return {'_stats_refresh_required': True}
    except (ValueError, KeyError, TypeError):
        return None
    if path.stem == 'dungeon':
        if detail_key == 'dungeon-all':
            payload['summary'] = detail
        else:
            payload['dungeons'] = [detail] if detail is not None else []
    elif path.stem == 'raid':
        # 仅替换选中难度、首领，其他卡片仍使用轻量概览。
        for item in payload.get('difficulties', []):
            for zone in item.get('zone_groups', []):
                zone['bosses'] = [detail if f"raid-{item['difficulty']}-{boss['boss_id']}" == detail_key else boss
                                  for boss in zone['bosses']]
                zone['bosses'] = [boss for boss in zone['bosses'] if boss is not None]
        payload['zone_groups'] = next((row['zone_groups'] for row in payload['difficulties'] if row['difficulty'] == 5), [])
    return payload


def read_log_projection(path, detail_key=None):
    """公开统计只接受当前纯日志版本；旧混合数据等待后台重建。"""
    payload = read_projection(path, detail_key)
    if isinstance(payload, dict) and Path(path).stem in ('dungeon', 'raid'):
        if payload.get('_data_contract') != 'wcl-log-v2':
            return {'_stats_refresh_required': True}
    return payload
