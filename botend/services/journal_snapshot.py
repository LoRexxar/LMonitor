"""冒险手册目录、首领菜单、技能及引用的统一文件发布。"""
from copy import deepcopy
from functools import lru_cache
import hashlib
import json
import logging
from pathlib import Path
import re
import threading
import time
import uuid

from django.conf import settings
from django.db import close_old_connections, transaction
from django.db.models.signals import post_delete, post_save
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from botend.journal_models import JournalEncounter, JournalInstance, JournalRelease, JournalState
from botend.models import SeasonMeta
from botend.services.journal_classification import JournalClassification, classification_reference
from botend.services.journal_service import ROLE_FLAGS
from botend.services.simc_benchmark_result_snapshot import _load, _lock, _write

SCHEMA = 1
logger = logging.getLogger(__name__)
_thread = None
_thread_lock = threading.Lock()


class JournalSnapshotUnavailable(Exception):
    """未准备或损坏时保留只读边界，不从数据库补查。"""


def snapshot_root():
    return Path(getattr(settings, 'JOURNAL_SNAPSHOT_ROOT', Path(settings.BASE_DIR) / 'var/journal'))


def _digest(body):
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _source():
    from botend.portal.adventure_journal import current_release
    from botend.services.gear_builder import active_season
    release, season = current_release(), active_season()
    if not release or release.status != 'completed':
        raise ValueError('没有已完成的冒险手册发布，保留旧版')
    identity = {'id': release.id, 'build': release.build, 'manifest': release.manifest, 'report': release.report,
                'completed': release.completed_at.isoformat() if release.completed_at else None,
                'season': [season.pk, season.season_key] if season else None,
                'classification': classification_reference()}
    return release, season, _digest(identity)


def project_difficulty(payload, difficulty):
    """后台保留难度内的平面技能，职责筛选在同一份文件上重建可见树。"""
    flat, roles, overview, tooltips = [], [], None, {}
    for section in payload['sections']:
        if difficulty not in section['difficulty_ids']:
            continue
        row = {key: value for key, value in section.items() if key not in ('source_text', 'descriptions', 'dynamic')}
        row['text'] = section['descriptions'].get(str(difficulty), '')
        row['has_dynamic'] = bool(section['dynamic'].get(str(difficulty)))
        row['role_names'] = [label for _, key, label in ROLE_FLAGS if key in row['roles']]
        if row.get('spell_id'):
            tooltips.setdefault(str(row['spell_id']), {'title': row['title'], 'text': row['text']})
        if section['type'] == 3:
            if section['roles']:
                roles.append(row)
            elif overview is None:
                overview = row['text']
        else:
            flat.append(row)
    return {'boss': {'id': payload['id'], 'name': payload['name'],
                     'description': payload.get('description', ''), 'creatures': payload.get('creatures', []),
                     'faction': payload.get('faction', 'both'), 'overview': overview or '', 'available': True},
            'sections': flat, 'roles': roles, 'tooltips': tooltips}


def filter_skills(data, role):
    role = role if role in ('tank', 'healer', 'dps') else ''
    sections = deepcopy([row for row in data['sections'] if not role or not row['roles'] or role in row['roles']])
    shown = {row['id']: row for row in sections}
    for row in sections:
        row['children'] = []
    tree = []
    for row in sections:
        if row['parent'] in shown:
            shown[row['parent']]['children'].append(row)
        else:
            tree.append(row)
    return {**data['boss'], 'skills': tree,
            'roles': [row for row in data['roles'] if not role or role in row['roles']],
            'skill_total': len(sections), 'has_dynamic': any(row['has_dynamic'] for row in sections)}


def loot_version(bosses, difficulty):
    """原地修订首领或掉落时，旧掉落文件不能命中新菜单。"""
    return _digest([{'id': boss.journal_id, 'name': boss.name,
                     'loot': [drop for drop in boss.payload['loot'] if difficulty in drop['difficulty_ids']]}
                    for boss in sorted(bosses, key=lambda row: row.journal_id)
                    if difficulty in boss.payload['difficulty_ids']])


def _save(body):
    body = {'schema': SCHEMA, **body}
    filename = _digest(body) + '.json'
    path = snapshot_root() / filename
    if _load(path) != body:
        _write(path, body)
    return filename


def request_journal_refresh():
    def enqueue():
        try:
            _write(snapshot_root() / 'revision.json', {'revision': uuid.uuid4().hex})
        except Exception:
            logger.exception('冒险手册目录通知失败，等待周期补偿')
    transaction.on_commit(enqueue)


def register_journal_signals():
    def changed(sender, instance, update_fields=None, **kwargs):
        if sender is JournalState and update_fields and not set(update_fields) & {'active_release', 'active_release_id'}:
            return
        if sender is SeasonMeta and update_fields and not set(update_fields) & {'season_key', 'is_active', 'gear_batch_key', 'gear_synced_at'}:
            return
        if sender is JournalRelease and instance.status != 'completed':
            return
        request_journal_refresh()
    for model in (JournalState, JournalRelease, JournalInstance, JournalEncounter, SeasonMeta):
        post_save.connect(changed, sender=model, weak=False, dispatch_uid=f'journal-files-save-{model.__name__}')
        post_delete.connect(changed, sender=model, weak=False, dispatch_uid=f'journal-files-delete-{model.__name__}')


def refresh_journal_snapshot(*, poll=False):
    """预热命令与后台共用，最后切换索引，来源变化或失败时保留上一版。"""
    root = snapshot_root()
    with _lock(root / 'publish.lock', blocking=False) as acquired:
        if not acquired:
            return False
        revision = _load(root / 'revision.json', {})
        checked = _load(root / 'checked.json', {})
        now = time.time()
        if poll:
            if now - _load(root / 'attempt.json', {}).get('at', 0) < 15:
                return False
            if (root / 'index.json').exists() and checked.get('revision') == revision and now - checked.get('at', 0) < 300:
                return False
        _write(root / 'attempt.json', {'at': now})
        try:
            release, season, version = _source()
            previous = _load(root / 'index.json', {})
            if poll and previous.get('source_version') == version and checked.get('revision') == revision and now - checked.get('built_at', 0) < 3600:
                _write(root / 'checked.json', {**checked, 'at': now})
                return False
            from botend.portal.adventure_journal import instance_source, KINDS
            classification = JournalClassification(release, season)
            catalog, files = [], {}
            for instance in release.instances.order_by('-expansion', 'journal_id').prefetch_related('encounters').iterator(chunk_size=8):
                source = instance_source(release, instance.journal_id, season=season)
                identity = {'release_id': release.id, 'instance_id': instance.journal_id, 'source': source}
                bosses = list(instance.encounters.all())
                menus = []
                references = {str(d): {} for d in instance.payload['difficulty_ids']}
                for boss in bosses:
                    payload = boss.payload
                    skill_files = {}
                    for difficulty in payload['difficulty_ids']:
                        if difficulty not in instance.payload['difficulty_ids']:
                            continue
                        skill_files[str(difficulty)] = _save({'coordinate': {**identity, 'boss_id': boss.journal_id, 'difficulty': difficulty},
                                                             **project_difficulty(payload, difficulty)})
                        for drop in payload['loot']:
                            if difficulty in drop['difficulty_ids']:
                                owners = references[str(difficulty)].setdefault(str(drop['item_id']), [])
                                owner = {'id': boss.journal_id, 'name': boss.name}
                                if owner not in owners:
                                    owners.append(owner)
                    menus.append({'id': boss.journal_id, 'name': boss.name,
                                  'difficulty_ids': payload['difficulty_ids'], 'skills': skill_files})
                projected = classification.project(instance.payload)
                projected.update(kind_label=KINDS.get(projected['kind'], '副本'), source=source)
                counts = [number for number in instance.payload.get('boss_counts', {}).values() if number] or [len(bosses)]
                row = {**projected, 'boss_count': len(bosses),
                       'boss_count_label': str(max(counts)) if min(counts) == max(counts) else f'{min(counts)}–{max(counts)}',
                       'url': f'/portal/adventure-journal/{instance.journal_id}/', '_boss_names': [boss.name for boss in bosses]}
                catalog.append(row)
                files[str(instance.journal_id)] = _save({'coordinate': identity, 'instance': projected, 'bosses': menus,
                                                       'references': references,
                                                       'loot_versions': {str(d): loot_version(bosses, d)
                                                                         for d in instance.payload['difficulty_ids']}})
            if release.report.get('instances') and not catalog:
                raise ValueError('副本目录为空，拒绝覆盖已有文件')
            summary = {'id': release.id, 'build': release.build,
                       'updated': release.completed_at.isoformat() if release.completed_at else None,
                       'counts': {key: release.report.get(key, 0) for key in ('instances', 'encounters', 'loot')}}
            body = {'schema': SCHEMA, 'source_version': version, 'release': summary,
                    'art_ids': release.manifest['catalog'].get('art_ids', []),
                    'difficulties': release.manifest['catalog']['difficulties'], 'instances': files,
                    'catalog': _save({'release': summary, 'tiers': classification.tiers,
                                      'season_label': classification.season_label, 'instances': catalog})}
            if revision != _load(root / 'revision.json', {}) or _source()[2] != version:
                return False
            generation = _digest(body)
            if previous.get('generation') != generation or any(previous.get(key) != value for key, value in body.items()):
                _write(root / 'index.json', {**body, 'generation': generation, 'generated_at': timezone.now().isoformat()})
            _write(root / 'checked.json', {'at': now, 'built_at': now, 'revision': revision})
            _write(root / 'failure.json', {})
            return True
        except Exception:
            _write(root / 'failure.json', {'message': '本次手册更新未完成，继续展示上次资料。'})
            raise


@lru_cache(maxsize=64)
def _cached_file(path, identity):
    body = _load(Path(path))
    if not isinstance(body, dict) or body.get('schema') != SCHEMA or _digest(body) + '.json' != Path(path).name:
        raise JournalSnapshotUnavailable('手册资料文件不可用')
    return body


def _content(filename):
    if not isinstance(filename, str) or not re.fullmatch(r'[a-f0-9]{64}\.json', filename):
        raise JournalSnapshotUnavailable('手册文件名无效')
    path = snapshot_root() / filename
    try:
        stat = path.stat()
        return _cached_file(str(path), (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino))
    except OSError as exc:
        raise JournalSnapshotUnavailable('手册资料文件缺失') from exc


def read_index():
    index = _load(snapshot_root() / 'index.json')
    if index is None:
        raise JournalSnapshotUnavailable('手册资料正在准备')
    try:
        body = {key: index[key] for key in ('schema', 'source_version', 'release', 'art_ids', 'difficulties', 'instances', 'catalog')}
        if index['schema'] != SCHEMA or index['generation'] != _digest(body):
            raise ValueError
    except (TypeError, KeyError, ValueError) as exc:
        raise JournalSnapshotUnavailable('手册索引不可用') from exc
    return index


def release_summary(index):
    summary = dict(index['release'])
    summary['updated'] = parse_datetime(summary['updated']) if summary.get('updated') else None
    return summary


def read_catalog(index):
    data = _content(index['catalog'])
    if data['release'] != index['release']:
        raise JournalSnapshotUnavailable('手册目录范围不一致')
    return data


def read_instance(index, instance_id):
    from django.http import Http404
    filename = index['instances'].get(str(instance_id))
    if not filename:
        raise Http404('手册没有此副本')
    data = _content(filename)
    if data['coordinate']['release_id'] != index['release']['id'] or data['coordinate']['instance_id'] != instance_id:
        raise JournalSnapshotUnavailable('手册副本范围不一致')
    return data


def select_boss(instance, difficulty, selected):
    from django.http import Http404
    menus = instance['bosses']
    requested = next((row for row in menus if row['id'] == selected), None)
    if selected and not requested:
        raise Http404('此首领不属于所选副本')
    bosses = [row for row in menus if difficulty in row['difficulty_ids']]
    boss = next((row for row in bosses if row['id'] == selected), None)
    if not boss and requested:
        boss = next((row for row in bosses if row['name'] == requested['name']), None)
    return bosses, boss or (bosses[0] if bosses else None)


def read_skills(index, instance, boss, difficulty):
    data = _content(boss['skills'][str(difficulty)])
    expected = {**instance['coordinate'], 'boss_id': boss['id'], 'difficulty': difficulty}
    if data['coordinate'] != expected or expected['release_id'] != index['release']['id']:
        raise JournalSnapshotUnavailable('手册技能范围不一致')
    return data


def start_journal_snapshot_worker():
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        def run():
            while True:
                close_old_connections()
                try:
                    refresh_journal_snapshot(poll=True)
                except Exception:
                    logger.exception('冒险手册目录维护失败')
                finally:
                    close_old_connections()
                time.sleep(15)
        _thread = threading.Thread(target=run, name='journal-snapshot', daemon=True)
        _thread.start()
