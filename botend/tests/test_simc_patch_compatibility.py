"""Exercise revision-pinned patch adaptation through the real Git replay."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from unittest import mock

from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from botend.management.commands.update_simc_binary import Command


def sha(data):
    return hashlib.sha256(data).hexdigest()


def delta(old, new):
    return f'diff --git a/runtime.cpp b/runtime.cpp\n--- a/runtime.cpp\n+++ b/runtime.cpp\n@@ -1 +1 @@\n-{old}\n+{new}\n'.encode()


class SimcPatchCompatibilityTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.target = self.source / 'runtime.cpp'
        self.target.write_text('new upstream\n')
        header = self.source / 'engine/dbc/generated/client_data_version.inc'
        header.parent.mkdir(parents=True)
        header.write_text('#define CLIENT_DATA_WOW_VERSION "12.1.5.70575"\n')
        self.git('init', '-q')
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-qm', 'base')
        self.revision = self.git('rev-parse', 'HEAD').strip()
        self.patches = self.root / 'patches'
        self.patches.mkdir()
        self.original = delta('old upstream', 'old upstream + tracing')
        (self.patches / '0001-trace.patch').write_bytes(self.original)
        self.adapter_dir = self.patches / 'compatibility' / self.revision
        self.adapter_dir.mkdir(parents=True)
        self.replacement = delta('new upstream', 'new upstream + tracing')
        (self.adapter_dir / '0001-adapted.patch').write_bytes(self.replacement)
        self.manifest = {
            'schema_version': 1, 'revision': self.revision, 'game_build': '12.1.5.70575',
            'patches': [{'original_name': '0001-trace.patch', 'original_sha256': sha(self.original),
                         'replacement': '0001-adapted.patch', 'replacement_sha256': sha(self.replacement)}],
        }
        self.write_manifest()
        settings = override_settings(SIMC_CONFIG={'simc_patch_dir': str(self.patches)})
        settings.enable()
        self.addCleanup(settings.disable)
        self.command = Command()
        self.command.row = mock.Mock()
        self.command.simc_source_dir = str(self.source)

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.source, text=True)

    def write_manifest(self):
        (self.adapter_dir / 'manifest.json').write_text(json.dumps(self.manifest))

    def test_real_replay_uses_pinned_adapter_and_is_idempotent(self):
        self.assertTrue(self.command._apply_local_patches())
        self.assertEqual(self.target.read_text(), 'new upstream + tracing\n')
        self.assertFalse(self.command._apply_local_patches())
        self.assertEqual((self.patches / '0001-trace.patch').read_bytes(), self.original)
        ledger = json.loads((self.source / '.git/lmonitor-applied-patches.json').read_text())
        self.assertEqual(ledger['patches'], [{'name': '0001-trace.patch', 'sha256': sha(self.replacement)}])

    def test_candidate_identity_includes_effective_replacement(self):
        before = self.command._candidate_paths(self.revision)
        replacement = delta('new upstream', 'new upstream + better tracing')
        (self.adapter_dir / '0001-adapted.patch').write_bytes(replacement)
        self.manifest['patches'][0]['replacement_sha256'] = sha(replacement)
        self.write_manifest()
        self.assertNotEqual(before, self.command._candidate_paths(self.revision))

    def test_revision_scoped_tail_is_replayed_and_hashes_into_candidate(self):
        tail = delta('new upstream + tracing', 'new upstream + tracing + tail')
        (self.adapter_dir / '0002-tail.patch').write_bytes(tail)
        before = self.command._candidate_paths(self.revision)
        self.manifest['append_patches'] = [{'name': '0002-tail.patch',
            'path': '0002-tail.patch', 'sha256': sha(tail)}]
        self.write_manifest()
        self.assertNotEqual(before, self.command._candidate_paths(self.revision))
        self.assertTrue(self.command._apply_local_patches())
        self.assertFalse(self.command._apply_local_patches())
        self.assertEqual(self.target.read_text(), 'new upstream + tracing + tail\n')
        self.assertFalse((self.patches / '0002-tail.patch').exists())

    def test_bad_bindings_fail_before_any_worktree_mutation(self):
        mutations = [('revision', 'f' * 40), ('game_build', '12.1.0.69814'),
                     ('original_sha256', '0' * 64), ('replacement_sha256', '0' * 64),
                     ('replacement', '../outside.patch')]
        original_manifest = json.dumps(self.manifest)
        for key, value in mutations:
            with self.subTest(key=key):
                self.manifest = json.loads(original_manifest)
                target = self.manifest if key in ('revision', 'game_build') else self.manifest['patches'][0]
                target[key] = value
                self.write_manifest()
                with self.assertRaisesRegex(CommandError, '兼容'):
                    self.command._apply_local_patches()
                self.assertEqual(self.target.read_text(), 'new upstream\n')
                self.assertFalse((self.source / '.git/lmonitor-applied-patches.json').exists())

    def test_other_revision_never_selects_adapter(self):
        (self.source / 'next.txt').write_text('next')
        self.git('add', 'next.txt')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-qm', 'next')
        with self.assertRaises(CommandError):
            self.command._apply_local_patches()
        self.assertEqual(self.target.read_text(), 'new upstream\n')
