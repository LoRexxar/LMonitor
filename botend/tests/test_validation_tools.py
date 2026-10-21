"""Exercise audit CLIs with local inputs: completeness and failed-proof writes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class AuditCommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='audit-command-test-')
        self.addCleanup(directory.cleanup)
        self.work = Path(directory.name)

    def command(self, script, *arguments):
        return subprocess.run(
            [sys.executable, str(script), *map(str, arguments)],
            cwd=ROOT, env={**os.environ, 'PYTHONPATH': str(ROOT)},
            capture_output=True, text=True, timeout=15,
        )

    def coverage(self, before_actors, after_actors):
        identity = {'simc_revision': 'local-test-revision', 'game_build': 'local-test-build'}
        before = self.work / 'before.json'
        preview = self.work / 'preview.single.json'
        before.write_text(json.dumps({'identity': identity, 'actors': before_actors}))
        preview.write_text(json.dumps({'identity': identity, 'actors': after_actors}))
        output = self.work / 'coverage.json'
        process = self.command(
            ROOT / 'scripts/validate_simc_skill_coverage.py',
            '--before', before, '--preview', self.work / 'preview.json', '--output', output,
        )
        return process, output

    def test_coverage_accepts_direct_merged_and_resolved_identities(self):
        before = [{'class': 'Mage', 'specialization': 'Fire',
                   'skills': [['direct', 1, 'Direct'], ['merged', 2, 'Merged'], ['resolved', 0, 'Resolved']]}]
        after = [{'class': 'Mage', 'specialization': 'Fire', 'actions': [
            {'token': 'direct', 'spell_id': 1, 'components': []},
            {'token': 'combined', 'spell_id': 3, 'components': [
                {'token': 'merged', 'spell_id': 2}, {'token': 'resolved', 'spell_id': 4}]}]}]
        process, output = self.coverage(before, after)
        self.assertEqual(process.returncode, 0, process.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report['旧技能身份数'], 3)
        self.assertEqual(report['遗漏'], [])
        self.assertEqual([row['token'] for row in report['已合并进其他技能']], ['merged'])
        self.assertEqual(report['已补全法术身份'][0]['当前法术ID'], [4])

    def test_coverage_does_not_silently_drop_a_missing_actor(self):
        before = [{'class': 'Mage', 'specialization': 'Fire', 'skills': [['direct', 1, 'Direct']]}]
        process, output = self.coverage(before, [])
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(json.loads(output.read_text())['遗漏'][0]['token'], 'direct')

    def test_coverage_rejects_duplicate_actor_identity_on_either_side(self):
        before = {'class': 'Mage', 'specialization': 'Fire', 'skills': [['direct', 1, 'Direct']]}
        after = {'class': 'Mage', 'specialization': 'Fire', 'actions': [
            {'token': 'direct', 'spell_id': 1, 'components': []}]}
        for side in ('before', 'after'):
            with self.subTest(side=side):
                process, _ = self.coverage([before] * (2 if side == 'before' else 1),
                                           [after] * (2 if side == 'after' else 1))
                self.assertNotEqual(process.returncode, 0)

    def test_failed_item_proof_does_not_overwrite_a_repository_fixture(self):
        # Run the unchanged CLI in an isolated tree: an old implementation must
        # demonstrate its destructive write here, never in the real repository.
        isolated = self.work / 'project'
        script = isolated / 'scripts/audit_wow_item_difficulty.py'
        script.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / 'scripts/audit_wow_item_difficulty.py', script)
        fixture = isolated / 'botend/tests/fixtures/wow_item_difficulty_item250243_69587.json'
        fixture.parent.mkdir(parents=True)
        sentinel = b'{"last_good_test_sentinel": true}\n'
        fixture.write_bytes(sentinel)
        audit_dir = self.work / 'incomplete-evidence'
        audit_dir.mkdir()
        process = self.command(script, '--audit-dir', audit_dir)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(fixture.read_bytes(), sentinel)
