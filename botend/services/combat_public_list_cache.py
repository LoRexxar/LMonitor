"""仅保存已关联来源的公开报告目录，按账号、来源和页码隔离。"""
from datetime import timedelta
from hashlib import sha256
from types import SimpleNamespace

from django.core.cache.backends.filebased import FileBasedCache
from django.utils import timezone

from botend.services.combat_log_archive import directory


FRESH = timedelta(minutes=10)


def _cache():
    root = directory(SimpleNamespace(pk="public-report-pages"), create=False)
    return FileBasedCache(str(root), {"TIMEOUT": 86400, "OPTIONS": {"MAX_ENTRIES": 1024}})


def _key(source, page):
    identity = "\0".join((source.kind, source.region, source.realm, source.name))
    digest = sha256(identity.encode()).hexdigest()
    return f"public:v2:{source.user_id}:{source.pk}:{digest}:{page}"


def read(source, page):
    try:
        return _cache().get(_key(source, page))
    except OSError:
        return None


def save(source, page, listing):
    updated = timezone.now()
    try:
        _cache().set(_key(source, page), (updated, listing))
    except OSError:
        pass
    return updated
