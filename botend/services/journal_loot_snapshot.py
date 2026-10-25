"""冒险手册掉落文件投影：按发布版本、副本、难度和资料分支隔离。"""
import hashlib
import json
import logging
from pathlib import Path
import threading
import time

from django.conf import settings
from django.db import close_old_connections
from botend.services.simc_benchmark_result_snapshot import _load, _lock, _write

logger = logging.getLogger(__name__)
_thread = None
_thread_lock = threading.Lock()
SCHEMA = 2


def snapshot_root():
    return Path(getattr(settings, 'JOURNAL_LOOT_SNAPSHOT_ROOT', Path(settings.BASE_DIR) / 'var' / 'journal-loot'))


def _directory(key):
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
    return snapshot_root() / digest


def coordinate(release_id, instance_id, difficulty, source, *, journal_version=None):
    key = {'schema': SCHEMA, 'release_id': release_id, 'instance_id': instance_id,
           'difficulty': difficulty, 'source': source}
    if journal_version is not None:
        key['journal_version'] = journal_version
    return key


def _scope_entry(row, *, equipment):
    return {**{field: row[field] for field in (
        'item_id', 'faction', 'condition_id', 'display_season_id', 'slot', 'item_type', 'class_id',
        'filter_classes', 'filter_specs')},
        'boss_ids': sorted(owner['id'] for owner in row['sources']), 'equipment': equipment}


def _expected_scope(bosses, difficulty, source):
    """Freeze the actual compiled difficulty scope, not another difficulty's count.

    Only the worker queries the central catalog; readers verify this frozen proof
    without ORM or network work. Compiler already applies flags/difficulty masks.
    """
    from botend.portal.adventure_journal import CLASSES
    from botend.services.journal_loot import (
        equipment_type, enrich_loot_specializations, class_matches, specialization_options)
    from botend.services.wow_item_display import load_item_display_metadata
    rows = {}
    for boss in bosses:
        for drop in boss.payload['loot']:
            if difficulty not in drop['difficulty_ids']:
                continue
            row = rows.setdefault(drop['item_id'], {**drop,
                'item_type': equipment_type(drop)[0], 'sources': []})
            if boss.journal_id not in {owner['id'] for owner in row['sources']}:
                row['sources'].append({'id': boss.journal_id})
            for field in ('condition_id', 'display_season_id'):
                row[field] = row[field] or drop[field]
    metadata = load_item_display_metadata(rows)
    fallback = source.get('data_branch') or ('ptr' if source['key'] == 'ptr' else 'retail')
    drops = enrich_loot_specializations(list(rows.values()), source['key'], fallback_branch=fallback)
    expected = []
    for row in drops:
        central = metadata[row['item_id']].get('catalog_type')
        # Slot alone must not turn mounts, consumables or miscellaneous loot into gear.
        gear = row.get('class_id') in (2, 4) or central == 'equipment'
        if not gear and central not in ('misc', 'consumable', 'gem', 'enchant', 'recipe'):
            raise ValueError(f"未知掉落物品类型：{row['item_id']}")
        row['filter_classes'] = [cid for cid, _ in CLASSES if class_matches(row, cid)]
        row['filter_specs'] = [spec['id'] for cid, _ in CLASSES for spec in specialization_options(cid)
                               if class_matches(row, cid, spec['id'])]
        expected.append(_scope_entry(row, equipment=gear))
    return sorted(expected, key=lambda row: row['item_id'])


def _has_properties(details):
    """Accept source property forms, including armor/weapon-only equipment."""
    def nonempty(value):
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, dict)):
            return bool(value) and any(nonempty(v) for v in (value.values() if isinstance(value, dict) else value))
        return type(value) in (int, float) and value > 0
    return any(nonempty(details.get(field)) for field in (
        'stats', 'effects', 'raw_stats', 'raw_effects', 'armor', 'weapon_damage', 'weapon'))


def _validate_coverage(payload, key, expected):
    if not isinstance(expected, list) or any(type(row.get('equipment')) is not bool for row in expected):
        raise ValueError('掉落冻结范围无效')
    rows = payload.get('loot')
    if not isinstance(rows, list) or type(payload.get('loot_total')) is not int:
        raise ValueError('掉落覆盖缺少真实计数')
    if len(rows) != len(expected) or payload['loot_total'] != len(expected):
        raise ValueError('掉落覆盖不完整')
    by_id = {row['item_id']: row for row in expected}
    if len(by_id) != len(expected):
        raise ValueError('掉落覆盖身份重复')
    actual = []
    for row in rows:
        item = by_id.get(row['item_id'])
        if item is None or type(row['item_id']) is not int:
            raise ValueError('掉落覆盖含额外物品')
        actual.append(_scope_entry(row, equipment=item['equipment']))
        details = row.get('details')
        if not isinstance(details, dict):
            raise ValueError('掉落缺少详情事实')
        if row.get('class_id') in (2, 4) and not item['equipment']:
            raise ValueError('装备不能标为非装备')
        if item['equipment']:
            # 掉落范围完整与属性完整分别校验；公共特效可独立发布，不借用其他难度数值。
            if details.get('status') == 'partial':
                if (details.get('complete') is not False or details.get('stats_status') != 'unknown'
                        or details.get('item_level') is not None or details.get('stats') != []
                        or details.get('effects_status') not in ('reference', 'localized', 'untranslated')
                        or not isinstance(details.get('effects'), list) or not details['effects']
                        or not all(isinstance(effect, str) and effect.strip() for effect in details['effects'])):
                    raise ValueError('公共特效部分展示状态无效')
                continue
            if (details.get('complete') is not True or details.get('status') != 'complete'
                    or type(details.get('item_level')) is not int or details['item_level'] <= 0
                    or type(details.get('quality')) is not int or not 0 <= details['quality'] <= 7
                    or type(row.get('quality')) is not int or row['quality'] != details['quality']
                    or 'stats' not in details or 'effects' not in details or not _has_properties(details)):
                raise ValueError(f"装备缺少完整难度事实：{row['item_id']}")
        elif details.get('status') != 'not_equipment':
            raise ValueError('非装备类型未被中央目录确认')
    if sorted(actual, key=lambda row: row['item_id']) != expected:
        raise ValueError('掉落覆盖身份/首领/资格与冻结范围不符')
    return {'schema': 1, 'frozen_identity': key, 'expected': expected,
            'expected_count': len(expected), 'actual_count': len(actual),
            'equipment_count': sum(row['equipment'] for row in expected), 'complete': True}


def _read_projection(key):
    data = _load(_directory(key) / 'data.json')
    try:
        if (not isinstance(data, dict) or data.get('coordinate') != key
                or data.get('snapshot', {}).get('state') != 'ready'):
            return None
        proof = data['coverage']
        if (not isinstance(proof, dict) or type(proof.get('schema')) is not int
                or proof.get('complete') is not True
                or any(type(proof.get(field)) is not int for field in (
                    'expected_count', 'actual_count', 'equipment_count'))
                or proof != _validate_coverage(data, key, proof['expected'])):
            return None
        return data
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _read_legacy_reference(key):
    """Read only the exact schema-1 coordinate; never use this as publish proof.

    Schema 1 used the same canonical JSON directory hash and optional
    journal_version field. Do not scan siblings or drop any identity fields.
    """
    legacy_key = {**key, 'schema': 1}
    try:
        data = _load(_directory(legacy_key) / 'data.json')
        if (not isinstance(data, dict) or data.get('coordinate') != legacy_key
                or not isinstance(data.get('snapshot'), dict)
                or data['snapshot'].get('state') != 'ready'):
            return None
        rows = data.get('loot')
        if (not isinstance(rows, list) or not rows
                or type(data.get('loot_total')) is not int or data['loot_total'] != len(rows)):
            return None
        identities = set()
        for row in rows:
            if (not isinstance(row, dict) or type(row.get('item_id')) is not int
                    or row['item_id'] <= 0 or row['item_id'] in identities
                    or not isinstance(row.get('name'), str) or not row['name'].strip()):
                return None
            identities.add(row['item_id'])
            details = row.get('details')
            if (not isinstance(details, dict) or not isinstance(details.get('name'), str)
                    or not details['name'].strip()
                    or not isinstance(details.get('stats'), list)
                    or not isinstance(details.get('effects'), list)
                    or not isinstance(details.get('note'), str)):
                return None
        # _load returns a private decoded object. Preserve every row/tooltip
        # verbatim, including its original reference warning and attributes.
        data.pop('coverage', None)
        data['snapshot'] = {**data['snapshot'], 'representation': 'legacy_reference',
            'native_difficulty_verified': False,
            'reference_note': '沿用此坐标原有参考快照；尚未验证所选难度的初始掉落属性。'}
        return data
    except (OSError, KeyError, TypeError, ValueError, AttributeError):
        return None


def read_loot_projection(release_id, instance_id, difficulty, source, *, journal_version=None):
    """冷启动只登记任务；失败保留本坐标的旧版，不回退到在线聚合。"""
    key = coordinate(release_id, instance_id, difficulty, source, journal_version=journal_version)
    directory = _directory(key)
    try:
        if _load(directory / 'request.json') != key:
            _write(directory / 'request.json', key)
        data = _read_projection(key)
        if data is not None:
            return data
        if time.time() - (directory / 'request.json').stat().st_mtime > 60:
            (directory / 'request.json').touch()
        reference = _read_legacy_reference(key)
        if reference is not None:
            return reference
    except OSError:
        logger.exception('冒险手册掉落快照读取失败')
        return {'snapshot': {'state': 'unavailable'}}
    return {'snapshot': {'state': 'building'}}


def refresh_journal_loot_snapshots(*, batch_size=4, force=False, poll=False, targets=None, strict=False):
    from botend.journal_models import JournalInstance
    from botend.portal.adventure_journal import current_release, instance_source, build_loot_projection
    from botend.services.gear_catalog_snapshot import _source, snapshot_root as gear_root
    from botend.services.journal_snapshot import loot_version
    built = []
    root = snapshot_root()
    with _lock(root / 'worker.lock', blocking=False) as acquired:
        if not acquired:
            if strict:
                raise RuntimeError('冒险手册掉落快照锁忙，未完成预热')
            return built
        if targets is not None:
            targets = list({json.dumps(key, sort_keys=True): key for key in targets}.values())
        requests = ([_directory(key) / 'request.json' for key in targets]
                    if targets is not None else list(root.glob('*/request.json')))
        if not requests:
            return built
        checked = _load(root / 'source-check.json', {})
        revision = _load(gear_root() / 'revision.json', {})
        if poll and not strict and not force and checked.get('revision') == revision and time.time() - checked.get('at', 0) < 300:
            if (not checked.get('pending') and all(_read_projection(_load(path, {})) is not None for path in requests)
                    and not any(path.stat().st_mtime > checked.get('at', 0) for path in requests)):
                return built
        started_at = time.time()
        season, _, version = _source()
        release = current_release()
        if not release:
            if strict:
                raise RuntimeError('冒险手册尚未同步')
            return built
        pending = []
        for path in requests:
            key = _load(path, {})
            if key.get('schema') != SCHEMA or key.get('release_id') != release.id or _directory(key) != path.parent:
                continue
            previous = _read_projection(key) or {}
            if not strict and not force and previous.get('source_version') == version and time.time() - previous.get('built_at', 0) < 3600:
                continue
            attempt = _load(path.parent / 'attempt.json', {}).get('at', 0)
            if poll and not strict and attempt > previous.get('built_at', 0) and time.time() - attempt < 60:
                continue
            pending.append((attempt, path, key))
        selected = sorted(pending, key=lambda entry: entry[0])[:max(1, batch_size)]
        for _, path, key in selected:
            _write(path.parent / 'attempt.json', {'at': time.time()})
            try:
                instance = JournalInstance.objects.get(release=release, journal_id=key['instance_id'])
                if key['difficulty'] not in instance.payload['difficulty_ids']:
                    continue
                source = instance_source(release, instance.journal_id, season=season)
                if source != key['source']:
                    continue
                bosses = [boss for boss in instance.encounters.all() if key['difficulty'] in boss.payload['difficulty_ids']]
                if key.get('journal_version') and loot_version(bosses, key['difficulty']) != key['journal_version']:
                    continue
                expected_scope = _expected_scope(bosses, key['difficulty'], source)
                payload = build_loot_projection(release, instance.journal_id, bosses, key['difficulty'], source)
                coverage = _validate_coverage(payload, key, expected_scope)
                if _source()[2] != version:
                    break
                active = current_release()
                latest_bosses = list(instance.encounters.all())
                if (not active or active.id != release.id
                        or instance_source(active, instance.journal_id, season=season) != source
                        or loot_version(latest_bosses, key['difficulty']) != loot_version(bosses, key['difficulty'])):
                    continue
                _write(path.parent / 'data.json', {**payload, 'coordinate': key, 'source_version': version,
                    'coverage': coverage, 'built_at': time.time(), 'snapshot': {'state': 'ready', 'generated_at': time.time()}})
                built.append(key)
            except Exception:
                logger.exception('冒险手册掉落快照构建失败：%s', key)
        _write(root / 'source-check.json', {'at': started_at, 'revision': revision, 'version': version,
                                          'pending': targets is not None or len(pending) > max(1, batch_size)})
        if strict:
            expected = targets if targets is not None else [key for _, _, key in selected]
            required = pending if targets is not None else selected
            failed = [key for _, _, key in required if key not in built]
            for key in expected:
                data = _read_projection(key)
                if (not data or data.get('source_version') != version or key.get('release_id') != release.id):
                    if key not in failed:
                        failed.append(key)
            if failed:
                raise RuntimeError(f'冒险手册掉落预热未完成：{len(failed)} 个目标分片生成或回读失败')
            active = current_release()
            if expected and (_source()[2] != version or not active or active.id != release.id):
                raise RuntimeError('冒险手册来源在预热期间变化，请重试')
            for key in expected:
                instance = JournalInstance.objects.filter(release=active, journal_id=key['instance_id']).first()
                if (not instance or key['difficulty'] not in instance.payload['difficulty_ids']
                        or instance_source(active, instance.journal_id, season=season) != key['source']
                        or (key.get('journal_version') and
                            loot_version(list(instance.encounters.all()), key['difficulty']) != key['journal_version'])):
                    raise RuntimeError('冒险手册目标坐标在预热期间变化，请重试')
                data = _read_projection(key)
                bosses = [boss for boss in instance.encounters.all() if key['difficulty'] in boss.payload['difficulty_ids']]
                try:
                    scope = _expected_scope(bosses, key['difficulty'], key['source'])
                    valid = data and data['coverage'] == _validate_coverage(data, key, scope)
                except (KeyError, TypeError, ValueError) as exc:
                    raise RuntimeError('冒险手册掉落业务覆盖回读失败') from exc
                if not valid:
                    raise RuntimeError('冒险手册掉落业务覆盖回读失败')
    return built


def start_journal_loot_snapshot_worker(*, lifecycle=None):
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return _thread
        from botend.backend_lifecycle import worker_cycles
        def run():
            for _ in worker_cycles(lifecycle, interval=15, label='journal-loot-snapshot'):
                close_old_connections()
                try:
                    refresh_journal_loot_snapshots(poll=True)
                except Exception:
                    logger.exception('冒险手册掉落维护失败')
                finally:
                    close_old_connections()
        _thread = threading.Thread(target=run, name='journal-loot-snapshot', daemon=True)
        _thread.start()
        return _thread
