"""Immutable public JS projection; requests never write or resolve source URLs."""
import hashlib
import json
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.http import HttpResponse
from django.utils.cache import get_conditional_response
from django.views.decorators.http import require_safe

from botend.services.site_image_assets import published_image_map


@lru_cache(maxsize=1)
def release_script():
    # Same lifetime as the authoritative service's per-release process cache.
    # Canonical serialization makes manifest reordering a cache hit.
    entries = published_image_map()
    if not entries:
        return '', b''
    payload = json.dumps(entries, sort_keys=True, separators=(',', ':'))
    body = f'window.publishedImageEntries={payload};\n'.encode('ascii')
    return hashlib.sha256(body).hexdigest(), body


def archive_root():
    return Path(settings.BASE_DIR) / 'static' / 'shared' / 'site-image-maps'


@require_safe
def image_map_script(request, version):
    current, body = release_script()
    if current != version:
        # Cold browser opening an older frozen public page after a rollout.
        # The URLconf admits only a SHA-256; never serve different JS at its URL.
        try:
            body = (archive_root() / f'{version}.js').read_bytes()
        except FileNotFoundError:
            body = b''
        if not body or hashlib.sha256(body).hexdigest() != version:
            return HttpResponse(status=404, headers={'Cache-Control': 'no-store'})
    etag = f'"{version}"'
    response = get_conditional_response(request, etag=etag)
    if response is None:
        response = HttpResponse(body if request.method == 'GET' else b'', content_type='text/javascript')
        response['Content-Length'] = str(len(body))
    response['ETag'] = etag
    response['Cache-Control'] = 'public, max-age=31536000, immutable'
    response['X-Content-Type-Options'] = 'nosniff'
    return response
