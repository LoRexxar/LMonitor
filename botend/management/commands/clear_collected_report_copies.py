"""Remove only generated collectstatic output, including stale compressed variants."""
import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from botend.report_static import PRIVATE, PUBLIC


class Command(BaseCommand):
    help = 'Discard stale static report copies before collecting safe aliases.'

    def handle(self, **options):
        if not settings.STATIC_ROOT:
            raise CommandError('Missing STATIC_ROOT')
        root = Path(settings.STATIC_ROOT).resolve()
        base = Path(settings.BASE_DIR).resolve()
        sources = [base / 'static', *(Path(d[-1] if isinstance(d, (tuple, list)) else d)
                                      for d in settings.STATICFILES_DIRS)]
        if any(root.is_relative_to(d.resolve()) or d.resolve().is_relative_to(root) for d in sources):
            raise CommandError('STATIC_ROOT overlaps a source directory')
        targets = [root / name for name in (PUBLIC, *PRIVATE)]
        if any(path.resolve() != path for path in targets):
            raise CommandError('Refusing linked report directory')
        for path in targets:
            if path.is_dir():
                shutil.rmtree(path)
