"""Publish reviewed Git assets, never working-tree paths or caller-supplied URLs."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import logging
import os
import re
import resource
import subprocess
import sys
from urllib.parse import unquote_to_bytes
from xml.etree import ElementTree as ET

from tools.publish_site_images import Bucket, Gate, MAX_BYTES, assets, fetch, inspect_image, safe_error, sha

# First-party UI shapes and site badge. Pin the bytes, not just the containing directory.
PINS = {
    'static/portal/images/portal-logo.webp': '2ccd8f8314e5f2fbcfc0721f868179644073ef94a5f89658e61f29d982890bad',
    'static/portal/icons/icons.svg': 'b31499fcfc61aa57953704bdf7f4ab48c4b9990cac69adea9d998b516df96771',
    'static/portal/favicons/default.svg': 'b8b37f951b22f3271d34ead5d2f802b8d6332748dfcb8216f85e57c306bd9208',
    'static/dashboard/combat_symbols.svg': '4f57b82895063f1d742ead97e2e3bbb6c961844327dea799574c8bd5475ad450',
}
CSS = 'static/dashboard/css/combat_theme.css'
CSS_DIGESTS = {
    '6dacbc8112b87ed35678fa93fcd78e5c9eac24c34ca28215102989a93ca97250',
    '9e2556c071133b9dbdfcc0296f4d6eda4cf57c6781ef6a13051f4737821e660b',
    '6c1517566ee11aca5c38557f622603f306d08282b5819b21b6d5e174ed4c147f',
    '8074bba1cb3cb458f67957f74f947e9c1a9d13c00cca6bcca5656c9082387c94',
}
GAME = re.compile(r'static/(wow_icons/small|portal/img/consumables)/([a-zA-Z0-9_-]{1,160})\.jpg')
ROOTS = ['static/portal/images', 'static/portal/favicons', 'static/portal/icons', CSS,
         'static/dashboard/combat_symbols.svg', 'static/wow_icons/small', 'static/portal/img/consumables']
HEX = r'[0-9a-f]{64}'
MIMES = {'jpg': 'image/jpeg', 'webp': 'image/webp', 'png': 'image/png', 'gif': 'image/gif',
         'ico': 'image/x-icon', 'svg': 'image/svg+xml'}


def rule(name, digest):
    if name in PINS and PINS[name] == digest:
        return 'pinned'
    if name == 'css-sha256:' + digest and digest in CSS_DIGESTS:
        return 'pinned'
    m = GAME.fullmatch(name)
    if m:
        size = 'small' if m[1] == 'wow_icons/small' else 'medium'
        return f'https://wow.zamimg.com/images/wow/icons/{size}/{m[2]}.jpg'
    return None


def inspect_svg(body):
    # A tiny passive SVG dialect; no arbitrary style/paint servers/entities/PIs/links.
    if len(body) > 131072 or not body.isascii() or b'<!' in body or b'<?' in body:
        raise Gate('unsupported-svg')
    root = ET.fromstring(body)
    ns = '{http://www.w3.org/2000/svg}'
    if root.tag != ns + 'svg':
        raise Gate('not-svg')
    for node in root.iter():
        if node.tag not in {ns+t for t in ('svg', 'symbol', 'path', 'circle')}:
            raise Gate('svg-element')
        for k, v in node.attrib.items():
            if k in ('fill', 'stroke') and re.fullmatch(r'none|currentColor|white|#[0-9A-Fa-f]{6}', v): continue
            if k == 'style' and v == 'display:none': continue
            if k == 'id' and re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*', v): continue
            if k in ('stroke-linejoin', 'stroke-linecap') and v == 'round': continue
            if k == 'd' and re.fullmatch(r'[a-zA-Z0-9., +\-\s]+', v): continue
            if k in ('viewBox', 'stroke-width', 'cx', 'cy', 'r') and re.fullmatch(r'[0-9 .\-]+', v): continue
            raise Gate('svg-attribute')


def discover():
    rev = subprocess.check_output(['git', 'rev-parse', 'HEAD']).decode().strip()
    listing = subprocess.check_output(['git', 'ls-tree', '-rlz', rev, '--', *ROOTS]); candidates = {}; held = Counter()
    for row in filter(None, listing.split(b'\0')):
        metadata, path = row.split(b'\t', 1); mode, kind, oid, size = metadata.split()
        name = path.decode('ascii', errors='replace')
        if mode != b'100644' or kind != b'blob' or int(size) > MAX_BYTES:
            held['non-regular-or-oversize'] += 1; continue
        if name not in PINS and not GAME.fullmatch(name) and name != CSS:
            held['unreviewed-file'] += 1; continue  # do not even open these blobs
        data = subprocess.check_output(['git', 'cat-file', 'blob', oid])
        if name == CSS:
            for raw in re.findall(rb'url\("data:image/svg\+xml,([^"\r\n]{1,8192})"\)', data):
                svg = unquote_to_bytes(raw); digest = sha(svg)
                if digest in CSS_DIGESTS: candidates['css-sha256:' + digest] = svg
                else: held['unreviewed-inline'] += 1
        elif rule(name, sha(data)):
            candidates[name] = data
        else:
            held['changed-review-required'] += 1
    return rev, candidates, dict(held)


def deliver(name, data, store, apply, get=fetch, proofs=None):
    digest = sha(data); source = rule(name, digest)
    if not source: raise Gate('unapproved')
    ext = 'svg' if name.startswith('css-sha256:') else name.rsplit('.', 1)[1]; mime = MIMES[ext]
    size = None
    if ext == 'svg': inspect_svg(data)
    else:
        actual, _, size = inspect_image(data, mime)
        if actual != ext: raise Gate('format')
    key = f'{assets.PREFIX}{digest}.{ext}'; action = 'reused'; stored = None
    if source != 'pinned':
        icon, pixels = assets.canonical_icon(source)
        if size != (pixels, pixels): raise Gate('unexpected-icon-size')
        if store.read(icon) == data: key, stored = icon, data
        elif (proofs or {}).get(name) != digest and get(source) != (data, mime):
            raise Gate('unproven-icon-bytes')
    if stored is None: stored = store.read(key)
    if stored is None:
        if not apply: raise Gate('missing')
        store.create(key, data, mime); stored = store.read(key); action = 'conditional-create'
    if stored != data: raise Gate('origin-mismatch')
    public, ctype = get(assets.CDN + key)
    if public != data or ctype != mime: raise Gate('cdn-mismatch')
    return [key, digest], action


def previous_pair(name, data, previous):
    """Trusted previous producer receipt, exact current bytes and destination grammar only."""
    identity = name if name.startswith('css-sha256:') else '/' + name
    pair = previous.get(identity); digest = sha(data); source = rule(name, digest)
    ext = 'svg' if name.startswith('css-sha256:') else name.rsplit('.', 1)[1]
    allowed = {f'{assets.PREFIX}{digest}.{ext}'}
    if source and source != 'pinned': allowed.add(assets.canonical_icon(source)[0])
    if source and isinstance(pair, list) and len(pair) == 2 and pair[1] == digest and isinstance(pair[0], str) and pair[0] in allowed:
        return pair
    return None


def main(proofs=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--apply', action='store_true'); group.add_argument('--existing', action='store_true')
    parser.add_argument('--previous', type=Path, help='trusted, operator-owned previous receipt; skip unchanged verified entries')
    args = parser.parse_args(); rev, candidates, held = discover()
    if not (args.apply or args.existing):
        print(json.dumps({'revision':rev,'candidates':sorted(candidates),'held':held}, indent=2)); return 0
    if proofs is None:
        proofs = json.loads(Path(__file__).with_name('local_static_icon_proofs.json').read_bytes())
    previous = {}
    if args.previous:
        prior = json.loads(args.previous.read_bytes())
        if prior['schema'] != 1 or not isinstance(prior['images'], dict) or set(prior['images']) != set(prior['receipts']):
            raise Gate('invalid-previous-receipt')
        previous = prior['images']
    unchanged = {n: pair for n,b in candidates.items() if (pair := previous_pair(n, b, previous))}
    store = None if len(unchanged) == len(candidates) else Bucket(writable=args.apply)
    if args.apply and store is not None: store.require_unversioned()
    records, receipts, errors = {}, {}, {}
    def work(item):
        name, data = item
        if name in unchanged: return name, unchanged[name], 'previous', None
        try: return name, *deliver(name, data, store, args.apply, proofs=proofs), None
        except Exception as exc: return name, None, None, safe_error(exc)
    with ThreadPoolExecutor(max_workers=4) as pool:
        for name, pair, action, error in pool.map(work, sorted(candidates.items())):
            identity = name if name.startswith('css-sha256:') else '/' + name
            if error: errors[identity] = error
            else: records[identity], receipts[identity] = pair, action
    print(json.dumps({'schema':1, 'revision':rev, 'images':records, 'receipts':receipts,
                      'held':held, 'errors':errors}, sort_keys=True, indent=2)); return int(bool(errors))


if __name__ == '__main__':
    os.environ.pop('SSLKEYLOGFILE', None); resource.setrlimit(resource.RLIMIT_CORE, (0, 0)); logging.disable(logging.CRITICAL)
    try: sys.exit(main())
    except Exception as exc: sys.exit(safe_error(exc))
