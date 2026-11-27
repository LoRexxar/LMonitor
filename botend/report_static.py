"""Generated reports aren't ordinary static assets. Serve only public navigation aliases."""
import posixpath
from pathlib import Path

from django.contrib.staticfiles.finders import FileSystemFinder
from django.core.files.storage import FileSystemStorage

PUBLIC = 'portal/reports'
PRIVATE = ('simc_results', 'wcl_reports', 'wcl_snapshots')
ALIAS = Path(__file__).with_name('report_static_alias.html')


def namespace(name):
    name = posixpath.normpath(name.replace('\\', '/')).strip('/')
    return next((d for d in (*PRIVATE, PUBLIC) if name == d or name.startswith(d + '/')), None)


class _AliasStorage(FileSystemStorage):
    # collectstatic copies this constant document, never the original report bytes.
    def path(self, name):
        return str(ALIAS)


class ReportSafeFinder(FileSystemFinder):
    def find_location(self, root, path, prefix=None):
        group = namespace(path)
        if group in PRIVATE or (group == PUBLIC and not path.lower().endswith('.html')):
            return None
        match = super().find_location(root, path, prefix)
        return str(ALIAS) if match and group == PUBLIC else match

    def list(self, ignore_patterns):
        aliases = _AliasStorage()
        for path, storage in super().list([*ignore_patterns, *PRIVATE]):
            group = namespace(posixpath.join(storage.prefix or '', path))
            if group == PUBLIC and path.lower().endswith('.html'):
                yield path, aliases
            elif not group:
                yield path, storage
