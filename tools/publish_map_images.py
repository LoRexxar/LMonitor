"""Operator-only map receipt: pinned public Git rasters; never reads replay or route data.

From the project root, authorized production settings in the existing environment:
  python -B -m tools.publish_map_images [--apply]
Default verifies without writing. Only reviewed MDT-owned PNGs can be created;
floor WebPs may ONLY reuse matching, already-public auto-publisher objects.
"""
import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
from pathlib import PurePosixPath
import resource
import subprocess
import sys

from tools.publish_site_images import Bucket, Gate, assets, fetch, inspect_image, safe_error, sha

REVISION = '2ac4fe238e60b823ebdc3aa34a04f5f3c420ad41'
ROOT = 'static/portal/mythic_planner/vendor'
VERSIONS = ('mdt-6.2.1', 'mdt-6.2.10', 'mdt-6.2.11', 'mdt-6.2.15', 'mdt-6.2.16', 'mdt-6.2.2', 'mdt-6.2.20', 'mdt-6.2.5', 'mdt-6.2.9')
# These Git PNGs decode to the exact RGBA of their corresponding upstream TGAs
# at Nnoggie/MythicDungeonTools 7fd7672d41b3aff933cbfd5b0f508fc4f62e33ae.
# Deliberately exclude the icons.png sheet containing third-party marks.
UI = {
    "mdt-full.png": [
        "854f075f6ff668d388ecf93d189dfcc50acac76b247dac0fd865b56df1fd4a6b",
        "bbfec2b0a10f573207976978edcf598b78ab2ec7c0ecfc275d7e3e87b61dde6d"
    ],
    "ring.png": [
        "05d249e369531f45afd64bd6d481037d286f129854f734f3393af3b0c31c0c77",
        "2cbc23ba497bfe755cc20d469c201135cd6950b80663f16365b5f3ad7b9395fd"
    ],
    "circle-white.png": [
        "0eb4c537f96e75bc70b928a09f09525f38d8fd4a15b30c333a9f67ffb8de01c1",
        "307de4ac43786175fd928455f919c39e7e7239d2ebbadd787e0eaffdab1b51e5"
    ],
    "arrows.png": [
        "f09a83bdfeceddb3ebe4d65d744c3369799185563d65fc77234f747b07e94b6b"
    ],
    "triangle.png": [
        "6c235ee509856346e505a59b7c5f6fbc4424bfebc78f367a94b1d311ef17c3e1",
        "ec43a8ac561cee9c3007db801df10d228da0f8292d03f68eac2fc09220566e01"
    ],
    "line.png": [
        "538dbc7bc6de31910bda151961886991fcaacc5e7b79487216f37a0d93dbcd30"
    ]
}


def git_blob(path):
    return subprocess.check_output(['git', 'show', f'{REVISION}:{path}'])


def inventory():
    groups = defaultdict(list)
    blobs = {}
    for version in VERSIONS:
        base = f'{ROOT}/{version}'
        manifest = json.loads(git_blob(base + '/maps-manifest.json'))
        for row in manifest['maps'] + manifest['ui_assets']:
            if 'dungeon_key' in row:
                path = f"maps/{row['dungeon_key']}/floor-{row['floor_index']}.webp"
            else:
                name = PurePosixPath(row['output']).name
                if name not in UI:
                    continue
                if row['sha256'] not in UI[name]:
                    raise Gate('unreviewed-ui')
                path = f'assets/{name}'
            identity = f'/{base}/{path}'
            data = git_blob(identity[1:]); digest = sha(data)
            if digest != row['sha256']:
                raise Gate('git-manifest-mismatch')
            ext = path.rsplit('.', 1)[1]
            key = (f'mythic-planner/auto/maps/{digest}.webp' if ext == 'webp'
                   else f'{assets.PREFIX}{digest}.png')
            if key not in blobs:
                inspect_image(data, f'image/{ext}'); blobs[key] = data
            groups[key].append(identity)
    return groups, blobs


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply', action='store_true')
    apply = p.parse_args().apply
    groups, blobs = inventory()
    store = Bucket(writable=apply)
    if apply:
        store.require_unversioned()

    def verify(key):
        try:
            data = blobs[key]; mime = 'image/' + key.split('.')[-1]
            origin = store.read(key); action = 'reused'
            if origin is None and apply and key.startswith(assets.PREFIX) and key.endswith('.png'):
                store.create(key, data, mime); action = 'conditional-create'; origin = store.read(key)
            if origin != data:
                raise Gate('origin-missing-or-mismatch')
            if fetch(assets.CDN + key) != (data, mime):
                raise Gate('cdn-mismatch')
            return key, action, None
        except Exception as exc:
            return key, None, safe_error(exc)

    images, actions, errors = {}, {}, {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        for key, action, error in pool.map(verify, sorted(groups)):
            if error:
                errors[key] = error
            else:
                for identity in groups[key]:
                    images[identity] = [key, sha(blobs[key])]
                actions[key] = action
    print(json.dumps({'schema': 1, 'revision': REVISION, 'images': images,
                      'actions_by_object': actions, 'errors': errors}, sort_keys=True, indent=2))
    return int(bool(errors))


if __name__ == '__main__':
    os.environ.pop('SSLKEYLOGFILE', None)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0)); logging.disable(logging.CRITICAL)
    try:
        sys.exit(main())
    except Exception as exc:
        sys.exit(safe_error(exc))
