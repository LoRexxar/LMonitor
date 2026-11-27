"""Keep this release's immutable map for cold readers of older public snapshots."""
from pathlib import Path
from tempfile import NamedTemporaryFile

from django.core.management.base import BaseCommand

from botend.site_image_map import archive_root, release_script


class Command(BaseCommand):
    help = 'Archive the current manifest projection before packaging a release; retain earlier .js files.'

    def handle(self, **options):
        version, body = release_script()
        if not version:
            return 'No published images.'
        root = archive_root()
        root.mkdir(parents=True, exist_ok=True)
        target = root / f'{version}.js'
        if not target.exists() or target.read_bytes() != body:
            with NamedTemporaryFile(dir=root, delete=False) as out:
                tmp = Path(out.name)
                try:
                    out.write(body)
                    out.flush()
                    tmp.chmod(0o644)
                    tmp.replace(target)
                finally:
                    tmp.unlink(missing_ok=True)
        return str(target)
