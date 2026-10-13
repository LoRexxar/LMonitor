"""Exercise real archives and Linux directory exchange, without metadata repair."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from botend.models import WowTalentVersion
from botend.wow.spell_text import SpellTextResolver


BUILD = '12.1.0.69283'
ASSET = Path(__file__).resolve().parents[1] / f'data/retail_talent_db2_{BUILD}.tar.gz'


class InstallTalentDb2AssetsTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings_override = override_settings(BASE_DIR=self.root)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.target = self.root / '.cache/wago_db2_dumps' / BUILD
        self.version, _ = WowTalentVersion.objects.update_or_create(
            key='retail', defaults={'current_build': BUILD, 'source_dir': str(self.target)},
        )

    def install(self):
        out = io.StringIO()
        with CaptureQueriesContext(connection) as queries:
            call_command('install_talent_db2_assets', version_key='retail', stdout=out)
        self.assertFalse(any(q['sql'].lstrip().split()[0].upper() in
                             {'INSERT', 'UPDATE', 'DELETE', 'REPLACE'} for q in queries))
        return out.getvalue()

    def old_dump(self):
        self.target.mkdir(parents=True)
        with tarfile.open(ASSET) as archive:
            for member in archive.getmembers():
                (self.target / member.name).write_bytes(archive.extractfile(member).read())
        # A previous self-consistent bundle without Wild Charge's referenced spell.
        manifest = json.loads((self.target / 'manifest.json').read_text())
        for name in ('Spell_enUS.csv', 'Spell_zhCN.csv'):
            path = self.target / name
            rows = list(csv.DictReader(io.StringIO(path.read_text())))
            out = io.StringIO(newline='')
            writer = csv.DictWriter(out, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(row for row in rows if row['ID'] != '132302')
            payload = out.getvalue().encode()
            path.write_bytes(payload)
            manifest['files'][name] = {'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
        (self.target / 'manifest.json').write_text(json.dumps(manifest))

    def test_old_dump_upgrades_atomically_resolves_wild_charge_and_is_idempotent(self):
        self.old_dump()
        old_inode = self.target.stat().st_ino
        old_manifest = (self.target / 'manifest.json').read_bytes()
        binding = WowTalentVersion.objects.filter(pk=self.version.pk).values().get()
        output = self.install()
        self.assertIn('installed', output)
        self.assertNotEqual(old_inode, self.target.stat().st_ino)
        backups = list(self.target.parent.glob(f'.{BUILD}.rollback-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].stat().st_ino, old_inode)
        self.assertEqual((backups[0] / 'manifest.json').read_bytes(), old_manifest)
        resolver = SpellTextResolver(locale='zhCN', branch='retail', snapshot_build=BUILD, dump_dir=str(self.target))
        text = resolver.resolve('$@spelldesc132302', 102401, known_spell_ids={24858})
        for fragment in ('人形态', '飞向盟友的位置', '熊形态', '持续4', '猎豹形态', '眩晕3', '枭兽形态', '旅行形态', '20码', '水栖形态', '150%，持续5'):
            self.assertIn(fragment, text)
        self.assertNotIn('$', text)
        inode = self.target.stat().st_ino
        self.assertIn('already current', self.install())
        self.assertEqual(self.target.stat().st_ino, inode)
        self.assertEqual(list(self.target.parent.glob(f'.{BUILD}.rollback-*')), backups)
        self.assertEqual(WowTalentVersion.objects.filter(pk=self.version.pk).values().get(), binding)

    def test_first_install(self):
        self.assertIn('installed', self.install())
        self.assertTrue((self.target / 'manifest.json').is_file())
        self.assertEqual(list(self.target.parent.glob(f'.{BUILD}.rollback-*')), [])

    def test_different_build_or_source_is_skipped_without_fallback(self):
        for changes in ({'current_build': '12.1.0.99999'}, {'source_dir': str(self.root / 'auto-update')}, {'current_build': ''}):
            with self.subTest(changes=changes):
                WowTalentVersion.objects.filter(pk=self.version.pk).update(**dict({'current_build': BUILD, 'source_dir': str(self.target)}, **changes))
                self.assertIn('skip', self.install())
                self.assertFalse(self.target.exists())

    def test_unknown_directory_is_not_moved(self):
        self.target.mkdir(parents=True)
        marker = self.target / 'user-data'
        marker.write_text('keep')
        inode = self.target.stat().st_ino
        self.assertIn('skip', self.install())
        self.assertEqual(self.target.stat().st_ino, inode)
        self.assertEqual(marker.read_text(), 'keep')

    def test_symlink_destination_is_rejected(self):
        other = self.root / 'other'
        other.mkdir()
        self.target.parent.mkdir(parents=True)
        self.target.symlink_to(other, target_is_directory=True)
        with self.assertRaises(CommandError):
            self.install()
        self.assertEqual(list(other.iterdir()), [])

    def test_invalid_identifiers_are_rejected_without_writes(self):
        for key in ('../retail', 'retail ', ''):
            with self.subTest(key=key), self.assertRaises(CommandError):
                call_command('install_talent_db2_assets', version_key=key)
        WowTalentVersion.objects.filter(pk=self.version.pk).update(current_build='../69283')
        with self.assertRaises(CommandError):
            self.install()
        self.assertFalse(self.target.parent.exists())

    def test_symlink_parent_is_rejected(self):
        other = self.root / 'other'
        other.mkdir()
        (self.root / '.cache').symlink_to(other, target_is_directory=True)
        with self.assertRaises(CommandError):
            self.install()
        self.assertEqual(list(other.iterdir()), [])

    def test_bad_archives_preserve_old_directory(self):
        self.old_dump()
        old_manifest = (self.target / 'manifest.json').read_bytes()
        inode = self.target.stat().st_ino
        with tarfile.open(ASSET) as archive:
            payloads = [(m.name, archive.extractfile(m).read()) for m in archive.getmembers()]
        # Only archive discovery is redirected; parsing, validation, extraction,
        # and (in the successful test) renameat2 are all real.
        from botend.management.commands import install_talent_db2_assets as installer
        for kind in ('traversal', 'absolute', 'duplicate', 'symlink', 'hardlink', 'directory', 'missing', 'hash', 'wrong-build'):
            with self.subTest(kind=kind):
                bad = self.root / ASSET.name
                with tarfile.open(bad, 'w:gz') as archive:
                    for name, original in payloads:
                        if kind == 'missing' and name == 'SpellMisc.csv':
                            continue
                        payload = original
                        if kind == 'hash' and name == 'SpellMisc.csv':
                            payload += b'corrupt'
                        if kind == 'wrong-build' and name == 'manifest.json':
                            manifest = json.loads(payload)
                            manifest['build'] = '12.1.0.99999'
                            payload = json.dumps(manifest).encode()
                        member = tarfile.TarInfo(name)
                        member.size = len(payload)
                        archive.addfile(member, io.BytesIO(payload))
                    if kind in ('traversal', 'absolute', 'duplicate', 'symlink', 'hardlink', 'directory'):
                        name = {'traversal': '../escape', 'absolute': '/escape', 'duplicate': 'manifest.json'}.get(kind, 'unexpected')
                        member = tarfile.TarInfo(name)
                        member.type = {'symlink': tarfile.SYMTYPE, 'hardlink': tarfile.LNKTYPE, 'directory': tarfile.DIRTYPE}.get(kind, tarfile.REGTYPE)
                        member.linkname = 'manifest.json'
                        archive.addfile(member, io.BytesIO(b''))
                with patch.object(installer, '_bundle_path', return_value=bad):
                    with self.assertRaises(CommandError):
                        self.install()
                self.assertEqual(self.target.stat().st_ino, inode)
                self.assertEqual((self.target / 'manifest.json').read_bytes(), old_manifest)
                self.assertEqual(list(self.target.parent.glob(f'.{BUILD}.rollback-*')), [])
                self.assertFalse((self.target.parent / 'escape').exists())


class TalentAssetDeployContractTests(SimpleTestCase):
    def test_sandbox_release_installs_between_migrate_and_static_without_repair(self):
        # Execute a disposable copy only; stub all service/network commands.
        # Real installation itself is exercised above, not by this shell harness.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / 'deploy.sh'
            script.write_bytes((Path(__file__).resolve().parents[2] / 'deploy.sh').read_bytes())
            commands = root / 'commands'
            commands.mkdir()
            log = root / 'manage.log'
            stubs = {
                'git': 'exit 0',
                'sleep': 'exit 0',
                'pgrep': 'exit 1',
                'curl': 'exit 0',
                'screen': 'if [ "$1" = "-list" ]; then printf "1.lmweb\\n2.lmback\\n3.lmsimc\\n"; fi',
                'python-stub': 'printf "%s\\n" "$*" >> "$MANAGE_LOG"',
            }
            for name, body in stubs.items():
                command = commands / name
                command.write_text('#!/bin/bash\n' + body + '\n')
                command.chmod(0o755)
            environment = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ['PATH'],
                               PYTHON_BIN=str(commands / 'python-stub'), MANAGE_LOG=str(log),
                               DEPLOY_LOCK_FILE=str(root / 'deploy.lock'), LMONITOR_DEPLOY_REEXEC='0')
            result = subprocess.run(['bash', str(script)], cwd=root, env=environment,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            calls = log.read_text().splitlines()
            install = 'manage.py install_talent_db2_assets --version-key retail'
            self.assertEqual(calls.count(install), 1)
            self.assertLess(calls.index('manage.py migrate --no-input'), calls.index(install))
            self.assertLess(calls.index(install), calls.index("manage.py collectstatic --no-input --ignore=simc_results/*"))
            self.assertFalse(any('repair' in command or 'backfill_db2_talent' in command for command in calls))
