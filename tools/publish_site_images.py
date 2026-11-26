"""Run from the project root: python -m tools.publish_site_images --help."""
import argparse
import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import fcntl
import hashlib
from http.client import HTTPSConnection
from io import BytesIO
import ipaddress
import json
import os
from pathlib import Path
import socket
import sqlite3
import ssl
import sys
import tempfile
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from PIL import Image

from botend.services import site_image_assets as assets

BUCKET = 'wowdail-cn'
MAX_BYTES = 12 * 1024 * 1024
MAX_PIXELS = 20_000_000
FORMATS = {'JPEG': ('jpg', 'image/jpeg'), 'PNG': ('png', 'image/png'), 'WEBP': ('webp', 'image/webp'),
           'GIF': ('gif', 'image/gif'), 'ICO': ('ico', 'image/x-icon')}


class Gate(Exception):
    """Only messages constructed here (never HTTP/SDK exception text) may be logged."""


def sha(data):
    return hashlib.sha256(data).hexdigest()


def fetch(url):
    """One anonymous TLS GET; no proxies, netrc, cookies, redirect or second DNS lookup."""
    u = urlsplit(url)
    if not (assets.source_url(url) == url or (url.startswith(assets.CDN) and u.netloc == 'oss.wowdaily.cn')):
        raise Gate('not-public')
    addresses = socket.getaddrinfo(u.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global or
                            ipaddress.ip_address(a[4][0]).is_multicast for a in addresses):
        raise Gate('non-public-DNS')
    ctx = ssl.create_default_context()
    with closing(HTTPSConnection(u.hostname, 443, timeout=12, context=ctx)) as conn:
        # Supplying the connected socket preserves TLS SNI/hostname checks for the original host.
        af, kind, proto, _, addr = addresses[0]
        with socket.socket(af, kind, proto) as sock:
            sock.settimeout(12)
            sock.connect(addr)
            conn.sock = ctx.wrap_socket(sock, server_hostname=u.hostname)
        conn.request('GET', u.path, headers={'Accept': 'image/*', 'Accept-Encoding': 'identity'})
        with conn.getresponse() as r:
            if r.status != 200:
                raise Gate(f'http-{int(r.status)}')
            if r.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise Gate('encoded-response')
            n = r.getheader('Content-Length')
            if n is not None and not 0 < int(n) <= MAX_BYTES:
                raise Gate('response-length')
            body = r.read(MAX_BYTES + 1)
            if not body or len(body) > MAX_BYTES or (n is not None and len(body) != int(n)):
                raise Gate('body-length')
            return body, r.getheader('Content-Type', '').split(';')[0].strip().lower()


def inspect_image(body, mime):
    if not isinstance(body, bytes) or not 0 < len(body) <= MAX_BYTES:
        raise Gate('image-length')
    with Image.open(BytesIO(body), formats=list(FORMATS)) as img:
        ext, expected = FORMATS[img.format]
        if mime != expected and not (ext == 'ico' and mime == 'image/vnd.microsoft.icon'):
            raise Gate('mime-mismatch')
        if not 0 < img.width * img.height <= MAX_PIXELS:
            raise Gate('pixel-budget')
        img.verify()
    with Image.open(BytesIO(body), formats=list(FORMATS)) as img:
        size = img.size
        if ext == 'ico':
            for entry_size in img.ico.sizes():
                img.ico.getimage(entry_size).load()
        # Decode every frame of editorial animations; never flatten/convert the uploaded bytes.
        pixels = 0
        for frame in range(121):
            try:
                img.seek(frame)
            except EOFError:
                break
            pixels += img.width * img.height
            if frame >= 120 or pixels > 40_000_000 or img.width * img.height > MAX_PIXELS:
                raise Gate('animation-budget')
            img.load()
        return ext, expected, size


def stage(db, url):
    src = assets.source_url(url)
    if not src:
        raise Gate('not-public')
    with closing(sqlite3.connect(db, timeout=30)) as c:
        if c.execute('SELECT 1 FROM stage WHERE url=?', (url,)).fetchone():
            return 'cached'
        data, mime = fetch(src)
        inspect_image(data, mime)
        with c:
            c.execute('INSERT INTO stage VALUES(?,?,?,?)', (url, mime, sha(data), data))
        return 'staged'


def sdk_error(exc, status, code):
    import alibabacloud_oss_v2 as oss
    while isinstance(exc, oss.exceptions.OperationError):
        exc = exc.unwrap()
    return isinstance(exc, oss.exceptions.ServiceError) and (exc.status_code, exc.code) == (status, code)


class Bucket:
    def __init__(self, *, writable=False):
        import alibabacloud_oss_v2 as oss
        self.writable = writable
        if writable:
            from django.conf import settings
            cfg = settings.OSS_CONFIG
            if (cfg['bucket_name'], cfg['region'], cfg['base_url'].rstrip('/') + '/') != (BUCKET, 'cn-beijing', assets.CDN):
                raise Gate('unexpected-OSS-config')
            if not all(cfg.get(k) for k in ('access_key_id', 'access_key_secret')):
                raise Gate('incomplete-OSS-config')
            provider = oss.credentials.StaticCredentialsProvider(cfg['access_key_id'], cfg['access_key_secret'])
        else:
            provider = oss.credentials.AnonymousCredentialsProvider()
        conf = oss.config.load_default()
        conf.region, conf.endpoint = 'cn-beijing', 'https://oss-cn-beijing.aliyuncs.com'
        conf.disable_ssl, conf.retry_max_attempts = False, 1
        conf.credentials_provider = provider
        conf.enabled_redirect, conf.insecure_skip_verify = False, False
        conf.connect_timeout, conf.readwrite_timeout = 8, 15
        self.sdk, self.client = oss, oss.Client(conf)

    def require_unversioned(self):
        # The high-level v2 XML decoder does not handle the empty configuration on all versions.
        out = self.client.invoke_operation(self.sdk.OperationInput(op_name='GetBucketVersioning', method='GET',
                                            bucket=BUCKET, parameters={'versioning': ''}))
        root = ET.fromstring(out.http_response.content)
        if (out.status_code != 200 or root.tag not in ('VersioningConfiguration',
                '{http://doc.oss-cn-hangzhou.aliyuncs.com}VersioningConfiguration') or len(root) or (root.text or '').strip()):
            raise Gate('versioning-not-confirmed-disabled')

    def read(self, key):
        try:
            r = self.client.get_object(self.sdk.GetObjectRequest(bucket=BUCKET, key=key))
        except Exception as exc:
            if sdk_error(exc, 404, 'NoSuchKey'):
                return None
            raise
        with r.body:
            if r.status_code != 200 or not 0 < r.content_length <= MAX_BYTES:
                raise Gate('oss-response-length')
            data = r.body.read()
            if len(data) != r.content_length:
                raise Gate('oss-length-mismatch')
            return data

    def create(self, key, data, mime):
        if not self.writable or not key.startswith(assets.PREFIX):
            raise Gate('outside-create-prefix')
        try:
            r = self.client.put_object(self.sdk.PutObjectRequest(bucket=BUCKET, key=key, body=data,
                forbid_overwrite=True, content_md5=base64.b64encode(hashlib.md5(data).digest()).decode('ascii'),
                content_length=len(data), content_type=mime, cache_control='public, max-age=31536000, immutable'))
        except Exception as exc:
            if sdk_error(exc, 409, 'FileAlreadyExists'):
                return
            raise
        if r.status_code != 200:
            raise Gate('oss-create-unconfirmed')


def deliver(db, url, store, apply):
    with closing(sqlite3.connect(db)) as c:
        row = c.execute('SELECT mime,digest,data FROM stage WHERE url=?', (url,)).fetchone()
    if not row:
        raise Gate('stage-missing')
    mime, digest, data = row
    if sha(data) != digest:
        raise Gate('stage-hash-mismatch')
    ext, mime, size = inspect_image(data, mime)
    key = f'{assets.PREFIX}{digest}.{ext}'
    icon = assets.canonical_icon(url)
    if icon and size == (icon[1], icon[1]) and ext == 'jpg' and store.read(icon[0]) == data:
        key = icon[0]  # only reuse the exact canonical bytes and never write a canonical key
    else:
        stored = store.read(key)
        if stored is None:
            if not apply:
                raise Gate('not-in-bucket')
            store.create(key, data, mime)
            stored = store.read(key)  # applies also to a 409 race
        if stored != data:
            raise Gate('oss-bytes-mismatch')
    public, ctype = fetch(assets.CDN + key)
    if public != data or inspect_image(public, ctype)[:2] != (ext, mime):
        raise Gate('fixed-CDN-mismatch')
    return [key, digest]


def inputs(files):
    urls = set()
    for i, path in enumerate(files, 1):
        with path.open(encoding='utf-8') as f:
            for n, line in enumerate(f, 1):
                line = line.rstrip('\r\n')
                if not line or line.startswith('#'):
                    continue
                if not assets.source_url(line):
                    raise Gate(f'rejected input {i}, line {n}')
                urls.add(line)
    return sorted(urls)


@contextmanager
def locked(directory):
    fd = os.open(directory, os.O_DIRECTORY | os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield fd
    finally:
        os.close(fd)


def save_manifest(records, dest, dir_fd):
    with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as f:
        tmp = Path(f.name)
    try:
        with tmp.open('w', encoding='utf-8') as f:
            json.dump({'schema': 1, 'images': records}, f, sort_keys=True, indent=2)
            f.write('\n')
            f.flush()
            os.fchmod(f.fileno(), 0o644)
            os.fsync(f.fileno())
        os.replace(tmp, dest)
        os.fsync(dir_fd)
    finally:
        tmp.unlink(missing_ok=True)


def safe_error(exc):
    return str(exc) if isinstance(exc, Gate) else type(exc).__name__


def main(argv=None):
    p = argparse.ArgumentParser(description='Incrementally stage and verify trusted public images. Default: offline plan.')
    p.add_argument('files', nargs='+', type=Path, help='UTF-8, one exact URL per line')
    g = p.add_mutually_exclusive_group()
    g.add_argument('--stage', action='store_true', help='anonymous source GET into trusted private local state only')
    g.add_argument('--existing', action='store_true', help='staged bytes → existing OSS + fixed CDN → local manifest; no PUT')
    g.add_argument('--apply', action='store_true', help='as --existing, but allow conditional creation in site-images/v1/')
    p.add_argument('--state', type=Path, help='dedicated trusted operator-owned directory (0700); reuse it to resume')
    p.add_argument('--manifest', type=Path, default=assets.MANIFEST, help='existing manifest; defaults to shipped JSON')
    p.add_argument('--jobs', type=int, choices=range(1, 9), default=3)
    args = p.parse_args(argv)
    urls = inputs(args.files)  # reject entire invalid manifest before any side effects
    if not urls:
        raise Gate('empty-input')
    records = assets.read_records(args.manifest)
    todo = [u for u in urls if u not in records]
    print(f'input={len(urls)} already={len(urls)-len(todo)} pending={len(todo)}', flush=True)
    if not todo or not (args.stage or args.apply or args.existing):
        return 0
    if not args.state:
        raise Gate('private-state-required')
    args.state.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = args.state.lstat()
    if args.state.is_symlink() or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise Gate('unsafe-state-directory')
    db = args.state / 'site-images.sqlite3'
    with locked(args.state):
        if args.stage:
            with closing(sqlite3.connect(db)) as c:
                c.execute('CREATE TABLE IF NOT EXISTS stage(url TEXT PRIMARY KEY,mime TEXT,digest TEXT,data BLOB)')
        elif not db.is_file():
            raise Gate('no-stage-database')
        store = None if args.stage else Bucket(writable=args.apply)
        if args.apply:
            store.require_unversioned()

        def work(url):
            try:
                result = stage(db, url) if args.stage else deliver(db, url, store, args.apply)
                return result, None
            except Exception as exc:
                return None, safe_error(exc)

        counts = Counter()
        # Directory locks survive manifest inode replacement. Reload under lock to merge other batches.
        with locked(args.manifest.parent) as fd, ThreadPoolExecutor(max_workers=args.jobs) as pool:
            records = assets.read_records(args.manifest)
            todo = [u for u in urls if u not in records]
            for url, (result, err) in zip(todo, pool.map(work, todo)):
                if err:
                    counts['failed'] += 1
                    print(f'{err}: {url}', file=sys.stderr, flush=True)  # validated public URL only
                elif args.stage:
                    counts[result] += 1
                else:
                    records[url] = result
                    save_manifest(records, args.manifest, fd)
                    counts['verified'] += 1
        print(dict(counts), flush=True)
        return int(bool(counts['failed']))


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        sys.exit(safe_error(exc))  # do not leak signed request endpoints, headers or raw inputs
