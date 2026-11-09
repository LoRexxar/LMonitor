"""本地持久缓存：压缩正文与索引同事务发布；业务赛季与任务仍由主数据库管理。"""
from contextlib import contextmanager
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import time
from uuid import uuid4
from zoneinfo import ZoneInfo
import zlib

from django.conf import settings
from django.utils import timezone

from botend.services.simc_benchmark_result_snapshot import _lock

SCHEMA = 1
MAX_BODY_BYTES = 32 * 1024 * 1024
LOCAL_TZ = ZoneInfo('Asia/Shanghai')


class StorageError(RuntimeError):
    pass


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def key_for(coordinate):
    # 保留旧实现的摘要协议，已有分享链接及快照身份不发生迁移偏移。
    return hashlib.sha256(json.dumps(coordinate, sort_keys=True).encode()).hexdigest()


def epoch(value):
    if not value:
        return None
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('缓存时间必须包含时区')
    return result.timestamp()


class SnapshotStore:
    def __init__(self, directory):
        self.root = Path(directory)
        self.path = self.root / 'snapshots.sqlite3'
        self._size_check = None

    def _connect(self, *, create=False):
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        else:
            db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=rw', uri=True, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=30000')
        return db

    def initialized(self):
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return False
        with self.connection() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, SCHEMA):
                raise StorageError('统计缓存数据库版本不兼容，未自动覆盖')
            return version == SCHEMA

    @contextmanager
    def connection(self, *, create=False):
        try:
            db = self._connect(create=create)
        except sqlite3.Error as exc:
            raise StorageError('统计缓存数据库无法打开') from exc
        try:
            yield db
        except sqlite3.Error as exc:
            raise StorageError('统计缓存数据库不可用，已有文件未删除') from exc
        finally:
            db.close()

    def ensure(self):
        if self.initialized():
            return
        self.root.mkdir(parents=True, exist_ok=True)
        with _lock(self.root / 'storage-init.lock'):
            if self.initialized():
                return
            legacy = self._legacy_index(strict=True)
            self._capacity(sum(p.stat().st_size for p in self.root.glob('*.json') if p.is_file()) + 2 * 1024 ** 2)
            with self.connection(create=True) as db:
                # 小事务使用回滚日志，兼容部署中的 SQLite 版本；不依赖 WAL 或网络共享内存。
                db.execute('PRAGMA journal_mode=DELETE')
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
                try:
                    db.execute('''CREATE TABLE IF NOT EXISTS snapshots (
                        key TEXT PRIMARY KEY, scope TEXT NOT NULL, module TEXT NOT NULL,
                        coordinate TEXT NOT NULL, payload BLOB, digest TEXT, raw_bytes INTEGER NOT NULL DEFAULT 0,
                        fetched_at TEXT, upstream_at TEXT, upstream_epoch REAL,
                        attempted_at TEXT, next_at TEXT, error TEXT)''')
                    db.execute('CREATE INDEX IF NOT EXISTS snapshots_scope_module ON snapshots(scope,module)')
                    db.execute('''CREATE TABLE IF NOT EXISTS history (
                        key TEXT NOT NULL, day TEXT NOT NULL, scope TEXT NOT NULL, module TEXT NOT NULL,
                        payload BLOB NOT NULL, digest TEXT NOT NULL, raw_bytes INTEGER NOT NULL,
                        fetched_at TEXT NOT NULL, upstream_at TEXT, PRIMARY KEY(key,day))''')
                    db.execute('CREATE INDEX IF NOT EXISTS history_scope_day ON history(scope,day)')
                    db.execute('CREATE TABLE IF NOT EXISTS maintenance (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                    db.execute('''CREATE TABLE IF NOT EXISTS cycles (
                        id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                        scope TEXT, state TEXT NOT NULL, details TEXT NOT NULL)''')
                    migrated = bad = 0
                    candidates = dict(legacy.get('entries', {}))
                    # 救回旧版在正文替换后、索引落盘前中断留下的孤立正文。
                    for path in self.root.glob('*.json'):
                        if re.fullmatch(r'[0-9a-f]{64}', path.stem):
                            candidates.setdefault(path.stem, {})
                    for key, entry in candidates.items():
                        if not re.fullmatch(r'[0-9a-f]{64}', key) or not isinstance(entry, dict):
                            bad += 1
                            continue
                        body = self._legacy_body(key)
                        c = body.get('coordinate') or entry.get('coordinate')
                        if not isinstance(c, dict) or c.get('module') not in ('overview', 'runs', 'raids', 'heatmaps') or key_for(c) != key:
                            bad += 1
                            continue
                        blob, digest, size = self._pack(body) if body else (None, None, 0)
                        if not body:
                            bad += 1
                            entry = dict(entry, error=entry.get('error') or '旧快照正文缺失或损坏', next_at=timezone.now().isoformat())
                        db.execute('''INSERT OR REPLACE INTO snapshots
                            (key,scope,module,coordinate,payload,digest,raw_bytes,fetched_at,upstream_at,upstream_epoch,attempted_at,next_at,error)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                            (key, c.get('scope', ''), c['module'], encoded(c).decode(), blob, digest, size,
                             body.get('fetched_at'), body.get('upstream_at'), epoch(body.get('upstream_at')),
                             entry.get('attempted_at') or body.get('fetched_at'), entry.get('next_at'), entry.get('error')))
                        if body:
                            self._history(db, key, c, blob, digest, size, body['fetched_at'], body.get('upstream_at'))
                        migrated += 1
                    db.execute('INSERT OR REPLACE INTO maintenance VALUES(?,?)', ('migration', encoded({
                        'imported': migrated, 'invalid': bad, 'at': timezone.now().isoformat(), 'legacy_files_retained': True}).decode()))
                    db.execute(f'PRAGMA user_version={SCHEMA}')
                    db.execute('COMMIT')
                except BaseException:
                    db.execute('ROLLBACK')
                    raise

    def _legacy_index(self, *, strict=False):
        path = self.root / 'index.json'
        if not path.is_file():
            return {'schema': SCHEMA, 'entries': {}}
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data, dict) or data.get('schema') != SCHEMA or not isinstance(data.get('entries'), dict):
                raise ValueError
            return data
        except (OSError, ValueError):
            if strict:
                raise StorageError('旧统计索引损坏，停止自动迁移，原文件保留') from None
            return {'schema': SCHEMA, 'entries': {}}

    def _legacy_body(self, key):
        path = self.root / (key + '.json')
        try:
            if path.stat().st_size > MAX_BODY_BYTES:
                return {}
            body = json.loads(path.read_text(encoding='utf-8'))
            if body.get('schema') != SCHEMA or key_for(body['coordinate']) != key or not body.get('fetched_at') or not isinstance(body.get('data'), dict):
                return {}
            return body
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return {}

    def _pack(self, body):
        raw = encoded(body)
        if len(raw) > MAX_BODY_BYTES:
            raise StorageError('单份统计正文超过缓存大小上限')
        return zlib.compress(raw, 6), hashlib.sha256(raw).hexdigest(), len(raw)

    @staticmethod
    def _unpack(row):
        if row['payload'] is None:
            return {}
        try:
            decoder = zlib.decompressobj()
            raw = decoder.decompress(row['payload'], MAX_BODY_BYTES + 1)
            if (not decoder.eof or decoder.unused_data or len(raw) > MAX_BODY_BYTES or len(raw) != row['raw_bytes']
                    or hashlib.sha256(raw).hexdigest() != row['digest']):
                raise ValueError
            body = json.loads(raw)
            c = body['coordinate']
            if (body.get('schema') != SCHEMA or key_for(c) != row['key'] or not isinstance(body.get('data'), dict)
                    or c.get('scope', '') != row['scope'] or c['module'] != row['module']
                    or body.get('fetched_at') != row['fetched_at'] or body.get('upstream_at') != row['upstream_at']):
                raise ValueError
            if 'coordinate' in row.keys() and json.loads(row['coordinate']) != c:
                raise ValueError
            return body
        except (ValueError, TypeError, KeyError, AttributeError, zlib.error):
            raise StorageError('统计缓存正文校验失败，未返回不可信数据') from None

    @staticmethod
    def _entry(row):
        return {'coordinate': json.loads(row['coordinate']), 'attempted_at': row['attempted_at'],
                'next_at': row['next_at'], 'error': row['error'], 'fetched_at': row['fetched_at']}

    def index(self):
        if not self.initialized():
            return self._legacy_index()
        with self.connection() as db:
            rows = db.execute('SELECT key,coordinate,attempted_at,next_at,error,fetched_at FROM snapshots').fetchall()
        return {'schema': SCHEMA, 'entries': {r['key']: self._entry(r) for r in rows}}

    def read(self, coordinate):
        key = key_for(coordinate)
        if not self.initialized():
            return self._legacy_body(key), self._legacy_index()['entries'].get(key, {})
        with self.connection() as db:
            row = db.execute('SELECT * FROM snapshots WHERE key=?', (key,)).fetchone()
        return (self._unpack(row), self._entry(row)) if row else ({}, {})

    def read_reference(self, reference):
        """精确恢复日报引用；不初始化、不迁移、不按当前赛季或最新正文回退。

        history 本就按上海采集日保留压缩正文，同日覆盖后原 digest 不在即明确失败。
        只读连接也保证调用方无法因一次读取产生空库或 journal 文件。
        """
        try:
            if not isinstance(reference, dict) or reference.get('kind') != 'rio_statistics_snapshot':
                raise ValueError
            c = reference['coordinate']
            if (not isinstance(c, dict) or set(c) != {'module', 'season', 'region', 'scope'}
                    or c['module'] != 'overview' or c['region'] not in ('cn', 'world')
                    or not isinstance(c['season'], str) or not re.fullmatch(r'season-[a-z]+-(?:0|[1-9][0-9]*)', c['season'])
                    or not isinstance(c['scope'], str) or not re.fullmatch(r'[0-9a-f]{64}', c['scope'])):
                raise ValueError
            for field in ('key', 'digest'):
                if not isinstance(reference[field], str) or not re.fullmatch(r'[0-9a-f]{64}', reference[field]):
                    raise ValueError
            if key_for(c) != reference['key']:
                raise ValueError
            for field in ('archive_day', 'data_date'):
                value = reference[field]
                if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                    raise ValueError
            if (type(reference['stale']) is not bool or not isinstance(reference['season_name'], str)
                    or not reference['season_name'].strip()):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise StorageError('日报统计引用身份或格式不合法') from None

        db = None
        try:
            db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=30,
                                 isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute('BEGIN')
            if db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA:
                raise StorageError('日报统计引用数据库版本不可用')
            key, digest, archive_day = (reference[k] for k in ('key', 'digest', 'archive_day'))
            row = db.execute('SELECT * FROM snapshots WHERE key=? AND digest=?', (key, digest)).fetchone()
            if row is None:
                row = db.execute('SELECT * FROM history WHERE key=? AND day=? AND digest=?',
                                 (key, archive_day, digest)).fetchone()
            if row is None:
                raise StorageError('日报统计引用正文已缺失，未回退到新数据')
            body = self._unpack(row)
            if (set(body) != {'schema', 'coordinate', 'fetched_at', 'upstream_at', 'data'}
                    or hashlib.sha256(encoded(body)).hexdigest() != digest
                    or body.get('coordinate') != c or type(body.get('schema')) is not int
                    or body['data'].get('season') != c['season'] or body['data'].get('region') != c['region']
                    or ('coordinate' in row.keys() and json.loads(row['coordinate']) != c)
                    or ('day' in row.keys() and row['day'] != archive_day)):
                raise ValueError
            for field in ('fetched_at', 'upstream_at'):
                stamp = body.get(field)
                if stamp is None and field == 'upstream_at':
                    continue
                if not isinstance(stamp, str) or not stamp or epoch(stamp) is None:
                    raise ValueError
            collected_day = datetime.fromisoformat(body['fetched_at'].replace('Z', '+00:00')).astimezone(LOCAL_TZ).date()
            if collected_day.isoformat() != archive_day:
                raise ValueError
            return body
        except sqlite3.Error:
            raise StorageError('日报统计引用数据库无法读取，未创建或修改缓存') from None
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise StorageError('日报统计引用正文身份或时间校验失败') from None
        finally:
            if db is not None:
                db.close()

    def available(self, scope):
        if not self.initialized():
            return [{'coordinate': v['coordinate'], 'state': 'stale' if v.get('error') else 'ready'}
                    for k, v in self._legacy_index()['entries'].items()
                    if isinstance(v, dict) and v.get('coordinate', {}).get('scope') == scope
                    and (self.root / (k + '.json')).is_file()]
        with self.connection() as db:
            rows = db.execute('SELECT coordinate,error FROM snapshots WHERE scope=? AND payload IS NOT NULL', (scope or '',)).fetchall()
        return [{'coordinate': json.loads(r['coordinate']), 'state': 'stale' if r['error'] else 'ready'} for r in rows]

    def projection_entries(self, scope, *, keys=None):
        """仅供后台投影枚举当前范围，不在公开请求路径使用。"""
        self.ensure()
        rows = []
        selected = None if keys is None else sorted(set(keys))
        with self.connection() as db:
            for start in ([0] if selected is None else range(0, len(selected), 400)):
                batch = [] if selected is None else selected[start:start + 400]
                clause = '' if selected is None else ' AND key IN (' + ','.join('?' for _ in batch) + ')'
                rows.extend(db.execute('''SELECT key,coordinate,attempted_at,next_at,error,fetched_at,digest,
                    payload IS NOT NULL AS has_payload FROM snapshots WHERE scope=?''' + clause, (scope, *batch)).fetchall())
        return {row['key']: {**self._entry(row), 'digest': row['digest'], 'has_payload': bool(row['has_payload'])} for row in rows}

    def _history(self, db, key, c, blob, digest, size, fetched_at, upstream_at):
        if c['module'] not in ('overview', 'raids', 'runs'):
            return
        day = datetime.fromisoformat(fetched_at.replace('Z', '+00:00')).astimezone(LOCAL_TZ).date().isoformat()
        db.execute('''INSERT OR REPLACE INTO history
            (key,day,scope,module,payload,digest,raw_bytes,fetched_at,upstream_at) VALUES(?,?,?,?,?,?,?,?,?)''',
            (key, day, c.get('scope', ''), c['module'], blob, digest, size, fetched_at, upstream_at))

    def _capacity(self, incoming):
        maximum = int(getattr(settings, 'RIO_STATISTICS_MAX_STORAGE_BYTES', 10 * 1024 ** 3))
        minimum_free = int(getattr(settings, 'RIO_STATISTICS_MIN_FREE_BYTES', 256 * 1024 ** 2))
        # 包括七个固定备份槽、底图和旧文件；只限制增长，不自动删除用户文件。
        config = (maximum, minimum_free)
        if self._size_check is None or self._size_check[0] != config or time.monotonic() - self._size_check[1] > 5:
            used = sum(p.stat().st_size for p in self.root.rglob('*') if p.is_file())
            self._size_check = [config, time.monotonic(), used]
        used = self._size_check[2]
        if used + incoming > maximum or shutil.disk_usage(self.root).free < minimum_free + incoming:
            raise StorageError('统计缓存空间不足，已停止写入并保留有效数据')
        self._size_check[2] += incoming

    def publish(self, coordinate, payload, *, fetched_at, upstream_at, next_at, newer_only=False):
        self.ensure()
        body = {'schema': SCHEMA, 'coordinate': coordinate, 'fetched_at': fetched_at, 'upstream_at': upstream_at, 'data': payload}
        blob, digest, size = self._pack(body)
        self._capacity(len(blob) * 2 + 65536)
        key = key_for(coordinate)
        with self.connection() as db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            try:
                old = db.execute('SELECT * FROM snapshots WHERE key=?', (key,)).fetchone()
                stamp = epoch(upstream_at)
                if newer_only and old and old['upstream_epoch'] is not None and (stamp is None or stamp <= old['upstream_epoch']):
                    repair = False
                    if stamp == old['upstream_epoch']:
                        try:
                            self._unpack(old)
                        except StorageError:
                            repair = True
                    if not repair:
                        db.execute('ROLLBACK')
                        return False
                db.execute('''INSERT OR REPLACE INTO snapshots
                    (key,scope,module,coordinate,payload,digest,raw_bytes,fetched_at,upstream_at,upstream_epoch,attempted_at,next_at,error)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL)''',
                    (key, coordinate.get('scope', ''), coordinate['module'], encoded(coordinate).decode(), blob, digest, size,
                     fetched_at, upstream_at, stamp, fetched_at, next_at))
                self._history(db, key, coordinate, blob, digest, size, fetched_at, upstream_at)
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        return True

    def failure(self, coordinate, message, *, attempted_at, next_at):
        self.ensure()
        with self.connection() as db:
            db.execute('''INSERT INTO snapshots(key,scope,module,coordinate,attempted_at,next_at,error)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                attempted_at=excluded.attempted_at,next_at=excluded.next_at,error=excluded.error''',
                (key_for(coordinate), coordinate.get('scope', ''), coordinate['module'], encoded(coordinate).decode(), attempted_at, next_at, message))

    def cycle(self, details, *, cycle_id=None, final=False):
        self.ensure()
        now = timezone.now().isoformat()
        cycle_id = cycle_id or uuid4().hex
        with self.connection() as db:
            db.execute('''INSERT INTO cycles(id,started_at,finished_at,scope,state,details) VALUES(?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET finished_at=excluded.finished_at,scope=excluded.scope,
                state=excluded.state,details=excluded.details''',
                (cycle_id, now, now if final else None, details.get('scope'), details.get('state', 'running'), encoded(details).decode()))
        return cycle_id

    def recover_cycles(self):
        """调用方已持采集锁；此前仍运行的记录只能来自中断的旧进程。"""
        self.ensure()
        now = timezone.now().isoformat()
        with self.connection() as db:
            for row in db.execute("SELECT id,details FROM cycles WHERE state='running'").fetchall():
                details = json.loads(row['details'])
                details.update(state='interrupted', recovered_at=now)
                db.execute('UPDATE cycles SET state=?,finished_at=?,details=? WHERE id=?',
                           ('interrupted', now, encoded(details).decode(), row['id']))

    def backup(self, *, include_audit=False):
        self.ensure()
        day = timezone.now().astimezone(LOCAL_TZ).date()
        folder = self.root / 'backups'; folder.mkdir(exist_ok=True)
        destination = folder / f'slot-{day.weekday()}.sqlite3'
        with _lock(self.root / 'backup.lock'):
            report = self.audit(deep=True, reuse_verified=True)
            if report['state'] != 'ok':
                raise StorageError('缓存正文校验异常，未覆盖备份')
            previous = report['maintenance'].get('backup', {})
            if (previous.get('day') == day.isoformat() and previous.get('path') == destination.name
                    and previous.get('data_revision') == report['data_revision'] and destination.is_file()
                    and previous.get('file_digest') == self._file_digest(destination)):
                value = dict(previous, reused=True)
                return {**value, 'audit': report} if include_audit else value
            self._capacity(self.path.stat().st_size + 65536)
            with self.connection() as source:
                if source.execute('PRAGMA quick_check(1)').fetchone()[0] != 'ok':
                    raise StorageError('缓存完整性异常，未覆盖备份')
                target = sqlite3.connect(str(destination), timeout=30)
                try:
                    source.backup(target, pages=256)
                finally:
                    target.close()
                value = {'day': day.isoformat(), 'path': destination.name, 'at': timezone.now().isoformat(),
                         'data_revision': report['data_revision'], 'file_digest': self._file_digest(destination),
                         'reused': False}
                source.execute('INSERT OR REPLACE INTO maintenance VALUES(?,?)', ('backup', encoded(value).decode()))
        return {**value, 'audit': report} if include_audit else value

    @staticmethod
    def _file_digest(path):
        result = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                result.update(chunk)
        return result.hexdigest()

    def repair_from_backups(self, report):
        """只替换损坏记录，使用校验通过的固定槽副本；保留真实原采集时间。"""
        backups = sorted((self.root / 'backups').glob('slot-*.sqlite3'), key=lambda p: p.stat().st_mtime, reverse=True)
        targets = [('snapshots', key, None) for key in report.get('invalid_payloads', [])]
        targets += [('history', *value.rsplit('@', 1)) for value in report.get('invalid_history', [])]
        repaired = 0
        for table, key, day in targets:
            where, params = ('key=?', (key,)) if day is None else ('key=? AND day=?', (key, day))
            candidate = None
            for path in backups:
                try:
                    source = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
                    source.row_factory = sqlite3.Row
                    try:
                        row = source.execute(f'SELECT * FROM {table} WHERE {where}', params).fetchone()
                        if row:
                            self._unpack(row)
                            candidate = dict(row)
                    finally:
                        source.close()
                except (sqlite3.Error, StorageError):
                    continue
                if candidate:
                    break
            if not candidate:
                continue
            with self.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                try:
                    current = db.execute(f'SELECT * FROM {table} WHERE {where}', params).fetchone()
                    try:
                        if current:
                            self._unpack(current)
                            db.execute('ROLLBACK')
                            continue
                    except StorageError:
                        pass
                    if table == 'snapshots':
                        candidate.update(error='已从备份恢复，等待来源更新', attempted_at=timezone.now().isoformat(), next_at=timezone.now().isoformat())
                    columns = list(candidate)
                    db.execute(f"INSERT OR REPLACE INTO {table} ({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", tuple(candidate.values()))
                    db.execute('COMMIT')
                    repaired += 1
                except BaseException:
                    db.execute('ROLLBACK')
                    raise
        return repaired

    def progress(self):
        """仅取最近批次，不扫描快照、正文或文件目录。"""
        if not self.initialized():
            return {'state': 'uninitialized', 'message': '尚无可读取的批次记录'}
        with self.connection() as db:
            row = db.execute('SELECT * FROM cycles ORDER BY started_at DESC LIMIT 1').fetchone()
        if not row:
            return {'state': 'unknown', 'message': '尚无批次记录'}
        result = dict(row)
        result['details'] = json.loads(result['details'])
        return result

    def audit(self, *, deep=False, reuse_verified=False):
        if not self.initialized():
            return {'state': 'legacy', 'database': str(self.path), 'entries': len(self._legacy_index()['entries'])}
        with self.connection() as db:
            integrity = db.execute('PRAGMA quick_check(1)').fetchone()[0]
            count, raw, packed = db.execute('SELECT COUNT(*),COALESCE(SUM(raw_bytes),0),COALESCE(SUM(length(payload)),0) FROM snapshots').fetchone()
            history = db.execute('SELECT COUNT(*) FROM history').fetchone()[0]
            last = db.execute('SELECT * FROM cycles ORDER BY started_at DESC LIMIT 1').fetchone()
            nightly = db.execute('SELECT * FROM cycles WHERE details LIKE ? ORDER BY started_at DESC LIMIT 1',
                                 ('%"kind":"nightly"%',)).fetchone()
            maintenance = {r['key']: json.loads(r['value']) for r in db.execute('SELECT * FROM maintenance')}
            invalid = []
            invalid_history = []
            verified = reused = 0
            revision = hashlib.sha256()
            if deep:
                # 同日证明仅省去解压和 JSON 校验；每次仍重新散列正文及全部行字段。
                # 日期、正文或元数据变化均重新完整验证，命令行深检默认不复用证明。
                day = timezone.now().astimezone(LOCAL_TZ).date().isoformat()
                proofs, changes = {}, []
                if reuse_verified:
                    db.execute('''CREATE TABLE IF NOT EXISTS payload_validation (
                        identity TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, verified_day TEXT NOT NULL)''')
                    db.execute('BEGIN IMMEDIATE')
                    proofs = {r['identity']: r['fingerprint'] for r in db.execute(
                        'SELECT identity,fingerprint FROM payload_validation WHERE verified_day=?', (day,))}
                try:
                    for table, invalid_rows in (('snapshots', invalid), ('history', invalid_history)):
                        for row in db.execute(f'SELECT * FROM {table} ORDER BY key' + (',day' if table == 'history' else '')):
                            values = dict(row)
                            values['payload'] = hashlib.sha256(row['payload']).hexdigest() if row['payload'] is not None else None
                            # 校验协议有变化时提升版本，避免新版本沿用旧规则的证明。
                            fingerprint = hashlib.sha256(encoded(['payload-validation-v1', values])).hexdigest()
                            identity = table + ':' + row['key'] + ('@' + row['day'] if table == 'history' else '')
                            revision.update(encoded([identity, fingerprint]))
                            if row['payload'] is None:
                                continue
                            if proofs.get(identity) == fingerprint:
                                reused += 1
                                continue
                            try:
                                self._unpack(row)
                                verified += 1
                                changes.append((identity, fingerprint, day))
                            except StorageError:
                                invalid_rows.append(row['key'] + ('@' + row['day'] if table == 'history' else ''))
                    if reuse_verified:
                        db.executemany('INSERT OR REPLACE INTO payload_validation VALUES(?,?,?)', changes)
                        db.execute('COMMIT')
                except BaseException:
                    if reuse_verified:
                        db.execute('ROLLBACK')
                    raise
        return {'state': 'ok' if integrity == 'ok' and not invalid and not invalid_history else 'error', 'integrity': integrity,
                'database': str(self.path), 'entries': count, 'history_entries': history,
                'raw_bytes': raw, 'compressed_bytes': packed, 'invalid_payloads': invalid, 'invalid_history': invalid_history,
                'last_cycle': dict(last) if last else None, 'last_nightly': dict(nightly) if nightly else None,
                'maintenance': maintenance, 'verified_payloads': verified, 'reused_payloads': reused,
                'data_revision': revision.hexdigest() if deep else None}
