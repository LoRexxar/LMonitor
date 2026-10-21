"""Explicit, test-only MDT bootstrap; no Django setup, discovery or network by default."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
COMMIT = '7fd7672d41b3aff933cbfd5b0f508fc4f62e33ae'
ARCHIVE_SHA256 = '890e7784e0cce38598c745607784b2c10438116337770f13ef5f61d3ac1eae38'
PACKAGE_SHA256 = 'd75d69427e32021eee13df592c2a2e35d41501caa4eaa509258d5c73a711078d'
SUPPLEMENT_SHA256 = 'c3c2f074ce30303c035a17ba28c5815ce646efb957eeb58845fae2229a209236'
URL = f'https://codeload.github.com/Nnoggie/MythicDungeonTools/zip/{COMMIT}'
SUPPLEMENT = Path('LMonitor/ability_overrides.json')
DEFAULT_TARGET = ROOT / 'botend/data/mythic_planner/vendor/mythic-dungeon-tools-6.2.20'
MAX_BYTES = 64 * 1024 * 1024


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def safe_target(value):
    path = Path(os.path.abspath(value))
    if '..' in Path(value).parts or path == Path('/'):
        raise ValueError('Unsafe target path')
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f'Symlink target/ancestor refused: {part}')
    return path


def extract(archive, target):
    """Validate every member before writing; never use extractall."""
    prefix = f'MythicDungeonTools-{COMMIT}'
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        if len(entries) > 10000 or sum(e.file_size for e in entries) > MAX_BYTES:
            raise ValueError('Archive exceeds fixed-source limits')
        seen = set()
        checked = []
        for entry in entries:
            name = entry.filename
            parts = PurePosixPath(name).parts
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if (not parts or parts[0] != prefix or name.startswith('/')
                    or '\\' in name or '\x00' in name
                    or any(p in ('', '.', '..') or ':' in p for p in name.rstrip('/').split('/'))
                    or mode not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or (mode == stat.S_IFDIR and not entry.is_dir())
                    or (mode == stat.S_IFREG and entry.is_dir())
                    or entry.flag_bits & 1):
                raise ValueError(f'Unsafe ZIP entry: {name!r}')
            relative = Path(*parts[1:])
            if relative in seen or (len(parts) == 1 and not entry.is_dir()):
                raise ValueError(f'Duplicate/invalid ZIP entry: {name!r}')
            seen.add(relative)
            checked.append((entry, relative))
        if sum(not e.is_dir() for e in entries) != 2524:
            raise ValueError('Unexpected upstream file count')
        for entry, relative in checked:
            output = target / relative
            if entry.is_dir():
                output.mkdir(parents=True, exist_ok=True)
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                with source.open(entry) as src, output.open('xb') as dst:
                    shutil.copyfileobj(src, dst)


def restore_supplement(source):
    # Same relation selection/current-NPC guard as auto_update.prepare_candidate;
    # use its existing pure converter, NOT the updater (which needs Django/DB).
    sys.path.insert(0, str(ROOT))
    from botend.mythic_planner.mdt_converter import build_payload

    package = ROOT / 'botend/data/mythic_planner/mdt_6_2_20.json'
    if digest(package) != PACKAGE_SHA256:
        raise ValueError('Tracked fixed-release package SHA256 mismatch')
    baseline = json.loads(package.read_text(encoding='utf-8'))
    raw = build_payload(source, source_tag='6.2.20', source_commit=COMMIT)
    current = {(d['key'], e['npc_id']) for d in raw['dungeons'] for e in d['enemies']}
    dungeons = {}
    for dungeon in baseline['dungeons']:
        for enemy in dungeon['enemies']:
            spells = [a['spell_id'] for a in enemy['abilities']
                      if a['metadata'].get('source') == 'LMonitorAbilitySupplement']
            if spells and (dungeon['key'], enemy['npc_id']) in current:
                dungeons.setdefault(dungeon['key'], {'enemies': {}})['enemies'][str(enemy['npc_id'])] = spells
    relations = sum(len(spells) for d in dungeons.values() for spells in d['enemies'].values())
    if len(dungeons) != 3 or relations != 283:
        raise ValueError('Fixed supplement must contain 3 dungeons / 283 relations')
    metadata = baseline['data_version']['metadata']['ability_supplement']
    payload = {'schema_version': 1, 'target': metadata['target'],
               'sources': metadata['sources'], 'notes_zh': metadata['notes_zh'],
               'dungeons': dungeons}
    output = source / SUPPLEMENT
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    if digest(output) != SUPPLEMENT_SHA256:
        raise ValueError('Restored supplement SHA256 mismatch')


def manifest(root):
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f'Not a regular directory: {root}')
    result = {}
    for path in sorted(root.rglob('*')):
        mode = path.lstat().st_mode
        key = path.relative_to(root).as_posix()
        if stat.S_ISREG(mode):
            result[key] = digest(path)
        elif stat.S_ISDIR(mode):
            result[key + '/'] = None
        else:
            raise ValueError(f'Non-regular installed entry: {path}')
    return result


def install(archive, target):
    if digest(archive) != ARCHIVE_SHA256:
        raise ValueError('Archive SHA256 mismatch; target untouched')
    target = safe_target(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.mdt-test-stage-', dir=target.parent) as directory:
        candidate = Path(directory) / 'source'
        candidate.mkdir()
        extract(archive, candidate)
        restore_supplement(candidate)
        expected = manifest(candidate)
        if target.exists():
            if manifest(target) != expected:
                raise ValueError('Existing unknown/modified target refused; nothing overwritten')
            status = 'already-installed'
        else:
            # Linux atomic no-replace rename also protects an empty directory
            # appearing between the existence check and publication.
            rename = getattr(ctypes.CDLL(None, use_errno=True), 'renameat2', None)
            if rename is None:
                raise ValueError('Atomic no-replace installation requires Linux renameat2')
            if rename(-100, os.fsencode(candidate), -100, os.fsencode(target), 1):
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error), str(target))
            status = 'installed'
        if manifest(target) != expected:
            raise ValueError('Installed tree verification failed')
    return {'status': status, 'target': str(target), 'upstream_files': 2524,
            'supplement_sha256': SUPPLEMENT_SHA256, 'dungeons': 3, 'relations': 283}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--archive', type=Path, help='Local official fixed-commit ZIP (offline)')
    source.add_argument('--download', action='store_true', help='Explicitly allow official HTTPS download')
    parser.add_argument('--target', type=Path, default=DEFAULT_TARGET,
                        help='Install directory (default: this worktree fixed test vendor path)')
    args = parser.parse_args()
    try:
        if args.download:
            with tempfile.TemporaryDirectory(prefix='mdt-test-download-') as directory:
                archive = Path(directory) / 'upstream.zip'
                with urllib.request.urlopen(URL, timeout=60) as response, archive.open('wb') as output:
                    total = 0
                    while block := response.read(1024 * 1024):
                        total += len(block)
                        if total > MAX_BYTES:
                            raise ValueError('Download exceeds fixed-source limit')
                        output.write(block)
                result = install(archive, args.target)
        else:
            result = install(args.archive, args.target)
        print(json.dumps(result, ensure_ascii=False))
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        parser.exit(1, f'MDT test bootstrap refused: {exc}\n')


if __name__ == '__main__':
    main()
