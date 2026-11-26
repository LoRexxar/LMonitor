"""Publish only existing v2 renditions referenced by the active journal file index.

Run in a trusted app environment, without django.setup(), DB or source HTTP.
--apply writes create-only OSS objects. stdout is an incremental schema-1 receipt;
stderr gives coverage and the allowlisted IDs still missing a cached rendition.
"""
import argparse
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import os
from pathlib import Path
import stat
import sys

from django.conf import settings
from tools.publish_site_images import Bucket, Gate, MAX_BYTES, assets, fetch, inspect_image, sha


def active_index():
    # Same checksum and root contract as journal_snapshot.read_index; importing that
    # service requires the model registry. Offline publication mustn't start the app.
    root = Path(getattr(settings, 'JOURNAL_SNAPSHOT_ROOT', Path(settings.BASE_DIR) / 'var/journal'))
    doc = json.loads((root / 'index.json').read_bytes())
    body = {k: doc[k] for k in ('schema', 'source_version', 'release', 'art_ids', 'difficulties', 'instances', 'catalog')}
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    if doc['schema'] != 1 or doc['generation'] != hashlib.sha256(canonical).hexdigest():
        raise Gate('invalid-index')
    ids = doc['art_ids']
    if not isinstance(ids, list) or not ids or any(type(i) is not int or not 0 < i < 10**10 for i in ids):
        raise Gate('invalid-art-ids')
    return doc['generation'], sorted(set(ids))


def rendition(directory_fd, file_id):
    # Use precisely cached_art's v2 output, never its network-enabled miss path.
    fd = os.open(f'{file_id}-v2.webp', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BYTES:
            raise Gate('invalid-cache-entry')
        data = stream.read(MAX_BYTES + 1)
    ext, _, (width, height) = inspect_image(data, 'image/webp')
    if ext != 'webp' or width > 900 or height > 600 or len(data) != info.st_size:
        raise Gate('invalid-v2-rendition')
    return data


def run(apply):
    generation, ids = active_index()
    root = Path(settings.BASE_DIR) / '.cache' / 'adventure-journal-art'
    if root.resolve() != Path(settings.BASE_DIR).resolve() / '.cache' / 'adventure-journal-art':
        raise Gate('unexpected-art-root')
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    missing, invalid, groups, receipts = [], [], defaultdict(list), {}
    try:
        for i in ids:
            try:
                digest = sha(rendition(fd, i))
            except FileNotFoundError:
                missing.append(i)
            except Exception:
                invalid.append(i)
            else:
                groups[digest].append(i)
        actions = Counter()
        if apply and groups:
            store = Bucket(writable=True)
            store.require_unversioned()

            def publish(item):
                digest, refs = item
                try:
                    body = rendition(fd, refs[0])
                    if sha(body) != digest:
                        raise Gate('changed-cache')
                    key = assets.PREFIX + digest + '.webp'
                    origin = store.read(key)
                    action = 'reused'
                    if origin is None:
                        store.create(key, body, 'image/webp')
                        origin = store.read(key)
                        action = 'conditional-create'
                    if origin != body or fetch(assets.CDN + key) != (body, 'image/webp'):
                        raise Gate('readback-mismatch')
                    return refs, [key, digest], action
                except Exception as exc:
                    return refs, None, type(exc).__name__

            failed = []
            with ThreadPoolExecutor(max_workers=8) as pool:
                for refs, pair, action in pool.map(publish, groups.items()):
                    actions[action] += 1
                    if pair is None:
                        failed.extend(refs)
                    else:
                        receipts.update({f'/portal/adventure-journal/art/{i}/?v=2': pair for i in refs})
        else:
            failed = []
        if active_index()[0] != generation:
            raise Gate('index-changed')
        print(json.dumps({'generation': generation, 'allowlisted': len(ids), 'cached': sum(map(len, groups.values())),
                          'missing_source': len(missing), 'missing_ids': missing, 'invalid_ids': invalid,
                          'unique_renditions': len(groups), 'objects': actions, 'failed_ids': failed,
                          'verified': len(receipts)}, sort_keys=True), file=sys.stderr)
        print(json.dumps({'schema': 1, 'images': receipts}, sort_keys=True))
        return int(bool(invalid or failed))
    finally:
        os.close(fd)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    os.environ.pop('SSLKEYLOGFILE', None)
    logging.disable(logging.CRITICAL)
    try:
        sys.exit(run(parser.parse_args().apply))
    except Exception as error:
        sys.exit('journal publisher stopped: ' + type(error).__name__)
