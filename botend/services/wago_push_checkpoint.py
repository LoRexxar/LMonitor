"""Disposable complete-push proofs; never a second published fact store.

The central source collector MUST revalidate the saved proof against Wago before
using its rows. A digest/TTL alone does not establish upstream freshness. Fixed
slots bound disk usage and replace old scopes rather than permanently refusing
new work when the cache fills. Lost/corrupt/oversize proofs are ordinary misses.
"""
import hashlib
import json
import logging
import re
import stat
from pathlib import Path

from django.conf import settings
from .download_checkpoint import atomic_bytes, encoded
from .download_retention import download_lock, safe_path

logger = logging.getLogger(__name__)
SCHEMA = 1
SLOTS = 512
MAX_BYTES = 1024 * 1024
SOURCE = 'https://wago.tools/hotfixes'
TEMP_NAME = re.compile(r'\.([0-9]{3})\.json\.writing-[A-Za-z0-9_-]+')


class PushCheckpoints:
    def __init__(self, *, from_push, to_push, region_id, locale, build_number, max_pages):
        self.root = Path(settings.BASE_DIR) / 'var' / 'wago-hotfix-checkpoints'
        self.scope = dict(schema=SCHEMA, source=SOURCE, from_push=int(from_push),
                          to_push=int(to_push), region_id=int(region_id), locale=str(locale),
                          build_number=None if build_number is None else int(build_number),
                          max_pages=int(max_pages), strategy='conserved-prefix-v1')

    def identity(self, push_id):
        return dict(self.scope, push_id=int(push_id))

    def path(self, push_id):
        path = self.root / f'{int(push_id) % SLOTS:03d}.json'
        if not safe_path(path):
            raise ValueError('linked Wago checkpoint path')
        if path.exists():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('non-regular Wago checkpoint')
        return path

    def load(self, push_id):
        try:
            path = self.path(push_id)
            with path.open('rb') as stream:
                raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                return None
            value = json.loads(raw)
            body = value['body']
            if (body['identity'] != self.identity(push_id) or body['complete'] is not True
                    or value['sha256'] != hashlib.sha256(encoded(body)).hexdigest()):
                return None
            return body['proof']
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def save(self, push_id, proof):
        # An interrupted collector never supplies a proof. Empty complete pushes
        # are valid, but a mock/failed/partial collector cannot create a record.
        if not isinstance(proof, dict) or 'total' not in proof or 'leaves' not in proof:
            return False
        body = dict(identity=self.identity(push_id), complete=True,
                    proof={key: proof[key] for key in ('total', 'leaves')})
        raw = encoded({'body': body, 'sha256': hashlib.sha256(encoded(body)).hexdigest()})
        if len(raw) > MAX_BYTES:
            return False
        try:
            # Reuse the existing durable writer/path guard/lock. No network or
            # business ORM writes occur while this short local lock is held.
            with download_lock(self.root):
                path = self.path(push_id)
                # A killed atomic writer can leave one private temporary file.
                # Under the same lock no live writer owns these names; bound the
                # scan and only reclaim this service's exact generated names.
                for index, entry in enumerate(self.root.iterdir()):
                    if index > SLOTS + 2:
                        raise ValueError('unexpected Wago checkpoint directory size')
                    if TEMP_NAME.fullmatch(entry.name):
                        info = entry.lstat()
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                            raise ValueError('linked Wago checkpoint temporary file')
                        entry.unlink()
                atomic_bytes(path, raw)
            return True
        except (OSError, ValueError):
            logger.warning('Wago push checkpoint unavailable; verified collection remains usable', exc_info=True)
            return False
