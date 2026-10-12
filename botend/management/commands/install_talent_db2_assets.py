"""Install only the archive matching an existing version binding; never repair DB rows."""
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from botend.management.commands.repair_ptr_talent_metadata import (
    _REQUIRED_DUMP_FILES,
    _validate_dump_contract,
)
from botend.models import WowTalentVersion


def _bundle_path(key, build):
    return Path(__file__).resolve().parents[2] / 'data' / f'{key}_talent_db2_{build}.tar.gz'


def _exchange(stage, target):
    """Linux atomic directory swap. Never degrade to a remove-then-rename."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as exc:
        raise CommandError('Linux renameat2 is required for an existing dump') from exc
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    if renameat2(-100, os.fsencode(stage), -100, os.fsencode(target), 2):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _owned_previous_dump(target, key, build):
    """Recognize a complete historical bundle without trusting it as current.

    Its old manifest need not match the new tracked contract. Require its exact
    payload set and self-integrity before moving it; unknown directories stay put.
    Only _validate_dump_contract may authorize the newly published contents.
    """
    try:
        entries = list(target.iterdir())
        if {p.name for p in entries} != _REQUIRED_DUMP_FILES | {'manifest.json'}:
            return False
        if any(p.is_symlink() or not p.is_file() for p in entries):
            return False
        manifest = json.loads((target / 'manifest.json').read_text())
        if not isinstance(manifest, dict) or manifest.get('version_key') != key or manifest.get('build') != build:
            return False
        records = manifest.get('files')
        if not isinstance(records, dict) or set(records) != _REQUIRED_DUMP_FILES:
            return False
        for name, record in records.items():
            if not isinstance(record, dict):
                return False
            payload = (target / name).read_bytes()
            if record.get('bytes') != len(payload) or record.get('sha256') != hashlib.sha256(payload).hexdigest():
                return False
        return True
    except (OSError, ValueError, UnicodeError):
        return False


def _extract_verified(bundle, stage, key, build):
    # Never use extractall: exact flat names and regular members only, even when
    # an archive's manifest looks trusted. Validate duplicates before any writes.
    with tarfile.open(bundle, 'r:gz') as archive:
        members = archive.getmembers()
        names = [m.name for m in members]
        if (len(names) != len(set(names))
                or set(names) != _REQUIRED_DUMP_FILES | {'manifest.json'}
                or any(not m.isreg() for m in members)):
            raise CommandError('archive must contain exactly the regular dump payloads and manifest (no duplicates/links/paths)')
        for member in members:
            with archive.extractfile(member) as source, (stage / member.name).open('xb') as destination:
                shutil.copyfileobj(source, destination)
                destination.flush()
                os.fsync(destination.fileno())
            (stage / member.name).chmod(0o644)
    manifest = _validate_dump_contract(stage, key)
    if manifest['build'] != build:
        raise CommandError('archive build does not match the current version binding')
    stage.chmod(0o755)
    _sync_directory(stage)
    return manifest


class Command(BaseCommand):
    help = 'Atomically install tracked talent DB2 assets for the current binding; no metadata repair or DB writes.'

    def add_arguments(self, parser):
        parser.add_argument('--version-key', default='retail')

    def handle(self, *args, **options):
        key = options['version_key']
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', key or ''):
            raise CommandError('invalid version key')
        version = WowTalentVersion.objects.filter(key=key).first()
        if version is None:
            self.stdout.write(f'skip: no version binding for {key}')
            return
        build, source = version.current_build, version.source_dir
        if not build:
            self.stdout.write(f'skip: {key} has no current build')
            return
        if not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', build):
            raise CommandError('invalid current build')
        bundle = _bundle_path(key, build)
        if not bundle.is_file():
            self.stdout.write(f'skip: no tracked archive for {key} build {build}')
            return
        # The auto-update pipeline owns other source paths. Do not overwrite or
        # rebind them even when they happen to have the same build number.
        base = Path(settings.BASE_DIR).absolute()
        target = base / '.cache' / 'wago_db2_dumps' / build
        supplied = Path(source) if source else None
        if supplied is not None and '..' in supplied.parts:
            raise CommandError('source_dir must not contain parent traversal')
        if supplied is not None and not supplied.is_absolute():
            supplied = base / supplied
        if supplied is None or os.path.abspath(supplied) != str(target):
            self.stdout.write(f'skip: {key} source_dir is not the archive-managed build directory')
            return
        try:
            for path in (target, *target.parents):
                if path.is_symlink():
                    raise CommandError(f'refusing symlink in destination path: {path}')
            target.parent.mkdir(parents=True, exist_ok=True)
            lock_path = target.parent / f'.{build}.install.lock'
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                self._install_locked(version, bundle, target)
        except (OSError, tarfile.TarError, ValueError, UnicodeError) as exc:
            raise CommandError(f'asset installation failed; no non-atomic fallback: {exc}') from exc

    def _install_locked(self, version, bundle, target):
        key, build = version.key, version.current_build
        if target.is_symlink():
            raise CommandError('refusing symlink destination')
        exists = target.exists()
        if exists and not _owned_previous_dump(target, key, build):
            self.stdout.write(f'skip: unknown/non-archive directory left untouched: {target}')
            return
        stage = Path(tempfile.mkdtemp(prefix=f'.{build}.rollback-', dir=target.parent))
        published = False
        try:
            manifest = _extract_verified(bundle, stage, key, build)
            if exists:
                try:
                    current = _validate_dump_contract(target, key)
                except CommandError:
                    current = None
                if current == manifest:
                    self.stdout.write(f'skip: already current: {target}')
                    return
            # Read-only recheck: never reset an automatic updater's version binding.
            if not WowTalentVersion.objects.filter(pk=version.pk, key=key,
                    current_build=build, source_dir=version.source_dir).exists():
                raise CommandError('version binding changed during installation; retry against the current binding')
            if exists:
                _exchange(stage, target)
            else:
                os.replace(stage, target)
            published = True  # stage now holds the previous directory; retain it.
            _sync_directory(target.parent)
            self.stdout.write(f'installed: {target}; rollback: {stage if exists else "none (first install)"}')
        finally:
            if not published:
                shutil.rmtree(stage)
