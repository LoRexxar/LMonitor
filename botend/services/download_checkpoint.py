"""Atomic local checkpoints. Unknown/legacy parts are never reclaimed here."""
import hashlib
import json
import os
import tempfile
from pathlib import Path
from uuid import uuid4


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_bytes(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f'.{path.name}.writing-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        fsync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')


def atomic_json(path, value):
    atomic_bytes(path, encoded(value))


def read_json(path):
    try:
        return json.loads(Path(path).read_bytes())
    except (OSError, ValueError, UnicodeError):
        return None


def valid_rows(rows, count):
    if not isinstance(rows, list) or len(rows) != count or not rows:
        return False
    try:
        headers = set(rows[0])
        ids = [int(row['ID']) for row in rows]
        return ('ID' in headers and len(set(ids)) == count and ids == sorted(ids)
                and all(isinstance(row, dict) and set(row) == headers
                        and all(isinstance(v, str) for v in row.values()) for row in rows))
    except (KeyError, TypeError, ValueError):
        return False


class PartDownload:
    def __init__(self, root, identity, total, chunk_size, refresh, *, per_page):
        self.identity = identity
        self.total = total
        expected = dict(identity=identity, total=total, chunk_size=chunk_size,
                        per_page=per_page, schema=2)
        fingerprint = hashlib.sha256(encoded(expected)).hexdigest()
        self.root = Path(root) / fingerprint
        self.path = self.root / 'manifest.json'
        saved = read_json(self.path)
        reusable = (isinstance(saved, dict) and all(saved.get(k) == v for k, v in expected.items())
                    and saved.get('state') in ('downloading', 'complete')
                    and isinstance(saved.get('attempt'), str)
                    and len(saved['attempt']) == 32
                    and all(c in '0123456789abcdef' for c in saved['attempt']))
        if not reusable or (refresh and saved['state'] == 'complete'):
            saved = dict(expected, attempt=uuid4().hex, state='downloading')
            atomic_json(self.path, saved)
        self.manifest = saved
        self.directory = self.root / saved['attempt']

    def load(self, offset, count):
        saved = read_json(self.directory / f'{offset}-{self.total}.json')
        if not isinstance(saved, dict):
            return None
        rows = saved.get('rows')
        if (saved.get('identity') != self.identity or saved.get('offset') != offset
                or saved.get('total') != self.total or not valid_rows(rows, count)
                or saved.get('headers') != sorted(rows[0])
                or saved.get('sha256') != hashlib.sha256(encoded(rows)).hexdigest()):
            return None
        return rows

    def save(self, offset, rows):
        atomic_json(self.directory / f'{offset}-{self.total}.json', {
            'identity': self.identity, 'offset': offset, 'total': self.total,
            'headers': sorted(rows[0]), 'sha256': hashlib.sha256(encoded(rows)).hexdigest(), 'rows': rows,
        })

    def complete(self, metadata):
        atomic_json(self.path, dict(self.manifest, state='complete', csv=metadata))
