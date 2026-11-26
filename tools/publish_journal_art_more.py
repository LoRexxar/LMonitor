"""One-shot offline CASC backfill: --apply --out NEW_DIR (default: inventory only).

At most 32 current page references/run. First two probes are serial, as is the
remainder (one worker, >=1s between source requests). No retries or redirects.
A denial, throttle, network/5xx, invalid image or OSS error stops the whole host.
Two consecutive 404/410s stop, or no successful pilot after two probes. Re-entry
requires an operator: merge successful receipts and honor Retry-After first.
"""
import argparse
from contextlib import closing
from http.client import HTTPSConnection
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import sys
import time

from PIL import Image
from tools.journal_art_scope import inventory
from tools.publish_site_images import Bucket, Gate, assets, fetch, inspect_image, sha


class SourceStatus(Exception):
    def __init__(self, code, retry_after):
        self.code = code
        self.retry_after = retry_after


def official_art(i):
    if type(i) != int or not 0 < i < 10**10:
        raise Gate('invalid-id')
    cap = 10 * 1024 * 1024
    with closing(HTTPSConnection('wago.tools', timeout=12)) as conn:
        conn.request('GET', f'/api/casc/{i}', headers={'User-Agent': 'LMonitor-AdventureJournal/1.0',
                                                      'Accept-Encoding': 'identity'})
        with conn.getresponse() as response:
            if response.status != 200:
                delay = response.getheader('Retry-After', '')
                raise SourceStatus(response.status, delay[:80] if delay.isascii() else '')
            if (response.getheader('Content-Encoding', 'identity') != 'identity'
                    or int(response.getheader('Content-Length', '0')) > cap):
                raise Gate('source-envelope')
            body = response.read(cap + 1)
            if not 0 < len(body) <= cap:
                raise Gate('source-length')
    from botend.services.journal_media import render_art_v2
    with Image.open(BytesIO(body), formats=('BLP', 'PNG', 'WEBP', 'JPEG')) as img:
        if img.width * img.height > 16_777_216 or getattr(img, 'n_frames', 1) != 1:
            raise Gate('source-dimensions')
    data = render_art_v2(body)
    inspect_image(data, 'image/webp')
    return data, sha(body)


def save(directory, name, doc):
    tmp = directory / ('.' + name)
    tmp.write_text(json.dumps(doc, sort_keys=True) + '\n')
    tmp.replace(directory / name)


def run(scope, out, limit):
    from botend.services import journal_snapshot
    out.mkdir(mode=0o700, exist_ok=False)
    images, events, verified, store = {}, [], set(), None
    targets = sorted(scope['targets'])[:limit]
    stop = None
    misses = 0
    for i in targets:
        ev = {'id': i, 'state': 'started'}
        events.append(ev)
        save(out, 'source.json', {'generation': scope['generation'], 'events': events})
        try:
            if journal_snapshot.read_index()['generation'] != scope['generation']:
                raise Gate('index-changed')
            data, source_sha = official_art(i)
            digest = sha(data)
            key = assets.PREFIX + digest + '.webp'
            if store is None:
                store = Bucket(writable=True)
                store.require_unversioned()
            if key not in verified:
                origin = store.read(key)
                if origin is None:
                    store.create(key, data, 'image/webp')
                    origin = store.read(key)
                if origin != data or fetch(assets.CDN + key) != (data, 'image/webp'):
                    raise Gate('readback-mismatch')
                verified.add(key)
            images[f'/portal/adventure-journal/art/{i}/?v=2'] = [key, digest]
            ev.update(state='published', source_sha256=source_sha)
            misses = 0
        except SourceStatus as exc:
            ev.update(state='source-http', status=exc.code, retry_after=exc.retry_after)
            misses += 1
            if exc.code not in (404, 410) or misses >= 2:
                stop = f'source-http-{exc.code}'
        except Exception as exc:
            ev.update(state='stopped')
            stop = str(exc) if isinstance(exc, Gate) else type(exc).__name__
        save(out, 'strict-oss-journal-more.json', {'schema': 1, 'images': images})
        if stop:
            break
        if i != targets[-1]:
            time.sleep(1)
    if not targets:
        save(out, 'strict-oss-journal-more.json', {'schema': 1, 'images': {}})
    save(out, 'source.json', {'generation': scope['generation'], 'events': events, 'stop': stop,
                              'targets': len(scope['targets']), 'published': len(images)})
    return int(stop is not None)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply', action='store_true')
    p.add_argument('--out', type=Path)
    p.add_argument('--limit', type=int, choices=range(1, 33), default=32)
    args = p.parse_args()
    if args.apply != bool(args.out):
        p.error('--apply requires --out NEW_DIR')
    scope = inventory()
    if not args.apply:
        print(json.dumps(scope, sort_keys=True))
        return 0
    return run(scope, args.out, args.limit)


if __name__ == '__main__':
    os.environ.pop('SSLKEYLOGFILE', None)
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    sys.exit(main())
