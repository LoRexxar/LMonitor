"""Immutable gzip catalog bodies and conservative, lock-owned maintenance.

Call writers/maintenance only under gear-catalog/worker.lock. Schema 3 remains
readable. Schema 4 has one .json.gz plus small .meta.json rollback descriptors;
bytes is the uncompressed wire length, payload_sha256 hashes those exact bytes.
No database/source-cache access is permitted in this module.
"""
from contextlib import contextmanager
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import uuid
import zlib

from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone
from botend.services.simc_benchmark_result_snapshot import _load, _lock, _write

SCHEMA_VERSION = 4
GRACE_SECONDS = 48 * 3600
GENERATION = re.compile(r'[0-9a-f]{32}')
BODY_NAME = re.compile(r'([0-9a-f]{32})\.(json(?:\.gz)?|meta\.json)')
CHUNK = 1024 * 1024
ERRORS = (OSError, ValueError, TypeError, KeyError, AttributeError, EOFError, zlib.error)


def safe_path(path, *, exists=True):
    """Do not traverse links, special files or multiply-linked cache files."""
    path = Path(path).absolute()
    for parent in reversed((path, *path.parents)):
        if parent.is_symlink():
            raise ValueError(f'unsafe symlink: {parent}')
    if exists:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f'not a private regular cache file: {path}')
    return path


def load_small(path):
    safe_path(path)
    if path.stat().st_size > 65536:
        raise ValueError('oversized catalog descriptor')
    value = _load(path)
    if not isinstance(value, dict):
        raise ValueError('invalid catalog descriptor')
    return value


def index_valid(directory, key, index):
    if not isinstance(index, dict) or index.get('schema_version') not in (3, 4) or index.get('coordinate') != key:
        raise ValueError('invalid schema/coordinate')
    generation = index.get('generation')
    if not isinstance(generation, str) or not GENERATION.fullmatch(generation):
        raise ValueError('invalid generation')
    schema = index['schema_version']
    expected = generation + ('.json.gz' if schema == 4 else '.json')
    if index.get('file') != expected:
        raise ValueError('generation/file mismatch')
    for field in ('bytes', 'gzip_bytes'):
        if type(index.get(field)) is not int or index[field] <= 0:
            raise ValueError(f'invalid {field}')
    for field in ('content_hash', *(['payload_sha256'] if schema == 4 else [])):
        if not re.fullmatch('[0-9a-f]{64}', str(index.get(field, ''))):
            raise ValueError(f'invalid {field}')
    if not isinstance(index.get('source_version'), str) or not index['source_version']:
        raise ValueError('missing source version')
    if type(index.get('built_at')) not in (float, int) or not math.isfinite(index['built_at']):
        raise ValueError('invalid publication time')
    archived = schema == 3 and index.get('storage_encoding') == 'gzip'
    path = safe_path(directory / (expected + '.gz' if archived else expected))
    if path.stat().st_size != index['gzip_bytes' if schema == 4 or archived else 'bytes']:
        raise ValueError('body length mismatch')
    zipped = gzip_path(directory, index)
    if safe_path(zipped).stat().st_size != index['gzip_bytes']:
        raise ValueError('gzip length mismatch')
    return index


def read_index(directory, key):
    try:
        index = load_small(directory / 'index.json')
        # Archive descriptors are not publishable legacy current indexes.
        if index.get('storage_encoding'):
            raise ValueError('archive descriptor cannot be current')
        return index_valid(directory, key, index)
    except ERRORS:
        return None


def gzip_path(directory, index):
    return directory / (index['file'] if index['schema_version'] == 4 else index['file'] + '.gz')


def stream_hash(stream):
    digest, length = hashlib.sha256(), 0
    while chunk := stream.read(CHUNK):
        digest.update(chunk)
        length += len(chunk)
    return length, digest.hexdigest()


def content_hash(content):
    """Hash legacy JSON bytes, buffering one item (or small metadata value).

    encode() uses the C encoder for each subtree; iterencode() would walk every
    leaf in Python. Keep the default spaces: these bytes identify old snapshots.
    """
    digest = hashlib.sha256()
    encoder = DjangoJSONEncoder(sort_keys=True, ensure_ascii=False, allow_nan=False)
    if not isinstance(content, dict) or not all(isinstance(key, str) for key in content):
        # Preserve the encoder's behavior for non-catalog inputs/JSON keys.
        for part in encoder.iterencode(content):
            digest.update(part.encode())
        return digest.hexdigest()
    digest.update(b'{')
    for position, key in enumerate(sorted(content)):
        if position:
            digest.update(b', ')
        digest.update(encoder.encode(key).encode())
        digest.update(b': ')
        value = content[key]
        if key == 'items' and isinstance(value, list):
            digest.update(b'[')
            for item_position, item in enumerate(value):
                if item_position:
                    digest.update(b', ')
                digest.update(encoder.encode(item).encode())
            digest.update(b']')
        else:
            digest.update(encoder.encode(value).encode())
    digest.update(b'}')
    return digest.hexdigest()


def payload_content(payload):
    return {name: value for name, value in payload.items() if name not in ('success', 'total', 'snapshot')}


def validate_payload(payload, key, generation, schema):
    snapshot = payload.get('snapshot', {})
    if (payload.get('success') is not True or snapshot.get('state') != 'ready'
            or snapshot.get('coordinate') != key or snapshot.get('generation') != generation
            or (schema == 4 and snapshot.get('schema_version') != 4)):
        raise ValueError('body identity mismatch')
    if not isinstance(payload.get('catalog'), dict):
        raise ValueError('invalid catalog content')
    if key.get('kind') == 'enhancements':
        if not isinstance(payload.get('groups'), dict) or not isinstance(payload.get('embellishment_options'), dict):
            raise ValueError('invalid enhancement content')
    elif not isinstance(payload.get('items'), list):
        raise ValueError('invalid equipment content')


def read_payload(directory, index):
    with gzip.open(safe_path(gzip_path(directory, index)), 'rt', encoding='utf-8') as stream:
        return json.load(stream)


def verify(directory, key, index, *, return_payload=False):
    index_valid(directory, key, index)
    with gzip.open(gzip_path(directory, index), 'rb') as stream:
        length, digest = stream_hash(stream)  # drains CRC/trailer before any deletion
    if length != index['bytes']:
        raise ValueError('uncompressed length mismatch')
    if index['schema_version'] == 3 and index.get('storage_encoding') != 'gzip':
        with safe_path(directory / index['file']).open('rb') as stream:
            if stream_hash(stream) != (length, digest):
                raise ValueError('legacy raw/gzip mismatch')
    elif digest != index.get('payload_sha256'):
        raise ValueError('payload hash mismatch')
    payload = read_payload(directory, index)
    validate_payload(payload, key, index['generation'], index['schema_version'])
    if content_hash(payload_content(payload)) != index['content_hash']:
        raise ValueError('content hash mismatch')
    return payload if return_payload else index


def prepare(directory, key, content, total, source_version):
    """Create and fully verify gzip, without publishing or producing a raw copy."""
    safe_path(directory, exists=False)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    generation = uuid.uuid4().hex
    filename = generation + '.json.gz'
    target = directory / filename
    payload = {'success': True, **content, 'total': total, 'snapshot': {
        'state': 'ready', 'generation': generation, 'coordinate': key,
        'schema_version': SCHEMA_VERSION, 'generated_at': timezone.now().isoformat()}}
    fd, temporary = tempfile.mkstemp(prefix='.writing-', dir=directory)
    digest, length = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, 'wb') as stream:
            with gzip.GzipFile(filename='', mode='wb', fileobj=stream, compresslevel=6, mtime=0) as zipped:
                for part in DjangoJSONEncoder(ensure_ascii=False, separators=(',', ':'), allow_nan=False).iterencode(payload):
                    raw = part.encode()
                    digest.update(raw)
                    length += len(raw)
                    zipped.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        index = {'schema_version': SCHEMA_VERSION, 'coordinate': key, 'generation': generation,
                 'file': filename, 'source_version': source_version, 'built_at': time.time(),
                 'bytes': length, 'gzip_bytes': target.stat().st_size,
                 'payload_sha256': digest.hexdigest(), 'content_hash': content_hash(content)}
        verify(directory, key, index)
        return index
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        Path(temporary).unlink(missing_ok=True)


def discard(directory, index):
    """Only our not-yet-published candidate, never the index's current body."""
    # Body validation can fail for a still-published generation. Do not turn
    # that failure into permission to delete it (or an unreadable pointer).
    try:
        current = load_small(directory / 'index.json')
    except FileNotFoundError:
        current = None
    except ERRORS:
        return
    if current is not None:
        if (current.get('generation') == index['generation']
                or current.get('file') == index['file']
                or read_index(directory, index['coordinate']) is None):
            return
    for name in (index['file'], index['generation'] + '.meta.json'):
        path = directory / name
        if path.exists():
            safe_path(path).unlink()


def _restore_index(path, raw):
    """Atomically restore the exact old pointer, including malformed JSON."""
    fd, temporary = tempfile.mkstemp(prefix='.writing-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def publish(directory, key, index, previous, *, write_index=_write):
    """Small rollback descriptors precede the single atomic commit point."""
    try:
        with safe_path(directory / 'index.json').open('rb') as stream:
            previous_document = stream.read(65537)
        if len(previous_document) > 65536:
            raise ValueError('oversized catalog descriptor')
    except FileNotFoundError:
        previous_document = None
    if read_index(directory, key) != previous:
        raise ValueError('current changed before publication')
    if previous:
        try:
            verify(directory, key, previous)
        except ERRORS:
            pass  # a worker may repair an already corrupt published body
        else:
            _write(directory / (previous['generation'] + '.meta.json'), previous)
    _write(directory / (index['generation'] + '.meta.json'), index)
    try:
        write_index(directory / 'index.json', index)
        published = read_index(directory, key)
        if published != index:
            raise ValueError('index readback mismatch')
        verify(directory, key, published)
    except BaseException:
        # Roll back only our own commit, never an independently changed index.
        document = _load(directory / 'index.json', {})
        if isinstance(document, dict) and document.get('generation') == index['generation']:
            if previous_document is None:
                (directory / 'index.json').unlink()
            else:
                _restore_index(directory / 'index.json', previous_document)
        raise


def inventory(directory):
    """Reject the whole scope before writes/deletes if its shape is unknown."""
    safe_path(directory, exists=False)
    generations = set()
    for path in directory.iterdir():
        safe_path(path)
        match = BODY_NAME.fullmatch(path.name)
        if match:
            generations.add(match[1])
        elif path.name not in ('index.json', 'request.json', 'attempt.json'):
            raise ValueError(f'unknown scope entry: {path.name}')
    return generations


def historical_descriptor(directory, key, generation, current):
    """Adopt only intact paired legacy bodies; leave uncommitted gzip alone."""
    meta = directory / (generation + '.meta.json')
    if meta.exists():
        index = load_small(meta)
        if index.get('generation') != generation:
            raise ValueError('rollback descriptor identity mismatch')
        verify(directory, key, index)
        return index
    raw, zipped = directory / (generation + '.json'), directory / (generation + '.json.gz')
    if not raw.exists() or not zipped.exists():
        return None  # never remove an unpublished/orphan body
    with gzip.open(zipped, 'rb') as stream:
        gz_facts = stream_hash(stream)
    with raw.open('rb') as stream:
        if stream_hash(stream) != gz_facts:
            raise ValueError('historical legacy raw/gzip mismatch')
    with gzip.open(zipped, 'rt', encoding='utf-8') as stream:
        payload = json.load(stream)
    validate_payload(payload, key, generation, 3)
    # A legacy body carries no source fingerprint: do not invent one from DB.
    return {'schema_version': 3, 'coordinate': key, 'generation': generation,
            'file': raw.name, 'source_version': 'legacy-unknown',
            'content_hash': content_hash(payload_content(payload)),
            'bytes': gz_facts[0], 'gzip_bytes': zipped.stat().st_size,
            'built_at': max(raw.stat().st_mtime, zipped.stat().st_mtime)}


def maintenance(directory, key, *, apply=False, now=None, limit=None):
    """Current + two noncurrent + 48h union; only converted scopes may reclaim.

Legacy raw removal is representation deduplication, not history expiry: an
intact gzip and a verified descriptor remain, even within the grace window.
Unknown/no-index scopes and unpaired unpublished files are never collected.
"""
    current = read_index(directory, key)
    if not current or current['schema_version'] != 4:
        return {'removed_files': 0, 'removed_bytes': 0, 'pending': 0}
    verify(directory, key, current)
    generations = inventory(directory)
    histories = []
    for generation in generations - {current['generation']}:
        meta = directory / (generation + '.meta.json')
        raw = directory / (generation + '.json')
        zipped = directory / (generation + '.json.gz')
        if meta.exists():
            index = load_small(meta)
            if index.get('generation') != generation:
                raise ValueError('rollback descriptor identity mismatch')
            index_valid(directory, key, index)
            histories.append(index)
        elif raw.exists() and zipped.exists():
            histories.append({'generation': generation,
                              'built_at': max(raw.stat().st_mtime, zipped.stat().st_mtime)})
        # An unpaired, uncommitted body is not a historical generation.
    histories.sort(key=lambda row: (row['built_at'], row['generation']), reverse=True)
    protected = {row['generation'] for row in histories[:2]}
    now = time.time() if now is None else now
    removed_files = removed_bytes = pending = processed = 0
    actions = []
    for index in histories:
        generation = index['generation']
        expire = generation not in protected and now - index['built_at'] >= GRACE_SECONDS
        raw = directory / (generation + '.json')
        redundant = raw.exists()
        if not expire and not redundant:
            if generation in protected:
                historical_descriptor(directory, key, generation, current)
            continue
        if limit is not None and processed >= limit:
            pending += 1
            continue
        processed += 1
        index = historical_descriptor(directory, key, generation, current)
        if index is None:
            raise ValueError('history disappeared during collection')
        if redundant:
            with gzip.open(gzip_path(directory, index), 'rb') as stream:
                expected_raw = stream_hash(stream)
            with safe_path(raw).open('rb') as stream:
                if stream_hash(stream) != expected_raw:
                    raise ValueError('redundant raw/gzip mismatch')
        paths = ([gzip_path(directory, index), raw, directory / (generation + '.meta.json')]
                 if expire else [raw])
        descriptor = None
        if not expire and index['schema_version'] == 3:
            # Preserve schema 3 identity while retaining only its verified gzip.
            descriptor = {**index, 'storage_encoding': 'gzip', 'payload_sha256': expected_raw[1]}
        actions.append((generation, paths, descriptor))
    # Validate the entire bounded batch before its first mutation. A corrupt
    # later history must not cause partial deletion of earlier histories.
    for generation, paths, descriptor in actions:
        if apply:
            if read_index(directory, key) != current:
                raise ValueError('current changed during collection')
            if descriptor is not None:
                _write(directory / (generation + '.meta.json'), descriptor)
        for path in paths:
            if path.exists():
                removed_bytes += safe_path(path).stat().st_size
                removed_files += 1
                if apply:
                    path.unlink()
    return {'removed_files': removed_files, 'removed_bytes': removed_bytes, 'pending': pending}


@contextmanager
def worker_lock(root, timeout=0):
    safe_path(root / 'worker.lock', exists=False)
    deadline = time.monotonic() + timeout
    while True:
        with _lock(root / 'worker.lock', blocking=False) as acquired:
            if acquired:
                yield True
                return
        if time.monotonic() >= deadline:
            yield False
            return
        time.sleep(min(.1, max(0, deadline - time.monotonic())))


def compact_scope(directory, key, *, apply=False):
    from botend.services.gear_catalog_projection import compact_catalog_content
    inventory(directory)
    old = read_index(directory, key)
    if old is None:
        raise ValueError('invalid published index')
    payload = verify(directory, key, old, return_payload=True)
    if old['schema_version'] == 4:
        result = maintenance(directory, key, apply=apply)
        return {'status': 'compliant', **result}
    # Validate all historic pairs before publishing/deleting any representation.
    for generation in inventory(directory) - {old['generation']}:
        historical_descriptor(directory, key, generation, old)
    if not apply:
        return {'status': 'would_convert', 'bytes': old['bytes'], 'gzip_bytes': old['gzip_bytes']}
    content = compact_catalog_content(payload_content(payload))
    new = prepare(directory, key, content, payload['total'], old['source_version'])
    try:
        publish(directory, key, new, old)
        verify(directory, key, read_index(directory, key))
    finally:
        discard(directory, new)
    return {'status': 'converted', 'generation': new['generation'],
            **maintenance(directory, key, apply=True)}
