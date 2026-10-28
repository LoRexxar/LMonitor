"""Raw audit boundary tests; fixtures are not SimC binary acceptance."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import audit_simc_native_scope as audit


class RawScopeAuditTests(unittest.TestCase):
    revision = '30c4b061f2103132a34952d80c963303b82bf368'
    build = '12.1.5.70575'

    def payload(self):
        return {
            'raw_catalog_schema': 1, 'catalog_kind': 'raw_dbc_scope_evidence',
            'draft': True, 'review_status': 'unreviewed',
            'compile_contract_allowed': False, 'normalized': False,
            'simc_revision': self.revision[:7], 'binary_revision': self.revision[:7],
            'game_build': self.build, 'ptr': False,
            'talent_catalog': [{'trait_entry_id': 1, 'class_id': 1, 'spell_id': 2,
                                'name': 'fixture', 'spec_ids': [], 'description': '',
                                'effects': [], 'dbc_scope_effects': []}],
        }

    def test_raw_identity_is_resolved_from_binary_not_cli_labels(self):
        data = self.payload()
        with patch.object(audit.subprocess, 'check_output', return_value=self.revision + '\n') as git:
            view = audit.validate_raw_catalog(data, Path('/source'), self.revision, self.build)
        self.assertEqual(git.call_args.args[0][-1], self.revision[:7] + '^{commit}')
        self.assertEqual(view['simc_revision'], self.revision)
        self.assertEqual(view['binary_revision'], self.revision[:7])
        self.assertIsNone(view['talent_catalog'][0]['global_effect_indices'])
        self.assertNotIn('global_effect_indices', data['talent_catalog'][0])
        self.assertEqual(data, self.payload())

    def test_reject_wrong_identity_reviewed_or_classified_catalog(self):
        mutations = [
            {'simc_revision': '1d3897e', 'binary_revision': '1d3897e'},
            {'game_build': '12.1.0.69814'}, {'normalized': True},
            {'review_status': 'reviewed'}, {'draft': False},
            {'compile_contract_allowed': True}, {'raw_catalog_schema': 26},
            {'binary_revision': ''}, {'ptr': True}, {'talents': []},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation), patch.object(
                    audit.subprocess, 'check_output', return_value=self.revision + '\n'):
                with self.assertRaises(ValueError):
                    audit.validate_raw_catalog({**self.payload(), **mutation}, Path('/source'), self.revision, self.build)
        for key in ('global_effect_indices', 'global_components', 'remove_talent'):
            data = copy.deepcopy(self.payload())
            data['talent_catalog'][0][key] = []
            with self.subTest(key=key), self.assertRaises(ValueError):
                audit.validate_raw_catalog(data, Path('/source'), self.revision, self.build)
        with patch.object(audit.subprocess, 'check_output', return_value='f' * 40 + '\n'):
            with self.assertRaises(ValueError):
                audit.validate_raw_catalog(self.payload(), Path('/source'), self.revision, self.build)

    def test_each_step_preserves_full_logs_failures_and_timeouts(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            with patch.object(audit.subprocess, 'run', return_value=subprocess.CompletedProcess(
                    ['fixture'], 1, b'out\xff', b'contract mismatch\n' + b'x' * 4000)):
                row = audit.run_step(['fixture'], root, 'catalog')
            self.assertEqual(row['退出码'], 1)
            self.assertEqual((root / 'catalog.stderr.log').read_bytes(), b'contract mismatch\n' + b'x' * 4000)
            self.assertEqual(json.loads((root / 'catalog.receipt.json').read_text()), row)
            with patch.object(audit.subprocess, 'run', side_effect=subprocess.TimeoutExpired(
                    ['fixture'], 180, output=b'partial output', stderr=b'partial stderr')):
                row = audit.run_step(['fixture'], root, 'actor')
            self.assertNotEqual(row['退出码'], 0)
            self.assertEqual((root / 'actor.stderr.log').read_bytes(), b'partial stderr')
            self.assertIn('TimeoutExpired', row['执行异常'])

    def test_catalog_only_uses_raw_option_and_retains_failed_manifest(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root / 'source'
            generated = source / 'engine/dbc/generated'
            generated.mkdir(parents=True)
            for name in audit.SOURCE_FILES:
                target = source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('// unit test fixture\n')
            (generated / 'client_data_version.inc').write_text(f'#define CLIENT_DATA_WOW_VERSION "{self.build}"\n')
            binary = root / 'simc'
            binary.write_bytes(b'explicit test fixture, not a binary')
            output = root / 'audit'
            command = ['audit', '--source', str(source), '--binary', str(binary),
                       '--output', str(output), '--catalog-only']
            def run(argv, **kwargs):
                if argv[0] == 'git':
                    return subprocess.CompletedProcess(argv, 0, (self.revision + '\n').encode(), b'')
                self.assertTrue(any(a.startswith('skill_damage_raw_scope_export=') for a in argv))
                self.assertFalse(any(a.startswith(('skill_damage_scope_export=', 'skill_damage_revision=',
                                                  'skill_damage_game_build=')) for a in argv))
                return subprocess.CompletedProcess(argv, 1, b'', b'unknown raw option')
            with patch('sys.argv', command), patch.object(audit.subprocess, 'check_output',
                    return_value=self.revision + '\n'), patch.object(audit.subprocess, 'run', side_effect=run):
                self.assertEqual(audit.main(), 1)
            manifest = json.loads((output / 'manifest.json').read_text())
            self.assertEqual(manifest['状态'], 'failed')
            self.assertEqual(manifest['结果'], [])
            self.assertEqual(manifest['目录导出']['退出码'], 1)
            self.assertEqual((output / 'catalog.stderr.log').read_bytes(), b'unknown raw option')
    def test_success_and_malformed_output_keep_raw_and_receipts(self):
        for mode in ('success', 'invalid_json', 'identity_mismatch'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root:
                root = Path(root)
                source, output = root / 'source', root / 'audit'
                for name in audit.SOURCE_FILES:
                    target = source / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text('// explicit unit fixture\n')
                (source / audit.SOURCE_FILES[-1]).write_text(f'#define CLIENT_DATA_WOW_VERSION "{self.build}"\n')
                binary = root / 'simc'
                binary.write_bytes(b'unit fixture, not SimC')
                payload = self.payload()
                if mode == 'identity_mismatch':
                    payload['game_build'] = '12.1.0.69814'
                raw_text = '{invalid' if mode == 'invalid_json' else json.dumps(payload)
                def run(argv, **kwargs):
                    if argv[0] == 'git':
                        stdout = b'fixture diff' if 'diff' in argv else (self.revision + '\n').encode()
                    else:
                        self.assertEqual(len(argv), 2)
                        key, path = argv[1].split('=', 1)
                        self.assertEqual(key, 'skill_damage_raw_scope_export')
                        Path(path).write_text(raw_text)
                        stdout = b'raw fixture exported'
                    return subprocess.CompletedProcess(argv, 0, stdout, b'fixture stderr')
                command = ['audit', '--source', str(source), '--binary', str(binary),
                           '--output', str(output), '--catalog-only']
                with patch('sys.argv', command), patch.object(audit.subprocess, 'run', side_effect=run):
                    self.assertEqual(audit.main(), 0 if mode == 'success' else 1)
                manifest = json.loads((output / 'manifest.json').read_text())
                self.assertEqual(manifest['状态'], 'catalog_only' if mode == 'success' else 'failed')
                self.assertEqual(manifest['结果'], [])
                self.assertEqual((output / 'raw-dbc-scope-catalog.json').read_text(), raw_text)
                self.assertEqual((output / 'catalog.stderr.log').read_bytes(), b'fixture stderr')
                if mode == 'success':
                    view = json.loads((output / 'global-scope-catalog.json').read_text())
                    self.assertEqual(view['simc_revision'], self.revision)
                    self.assertIsNone(view['talent_catalog'][0]['global_effect_indices'])
                    self.assertFalse(view['normalized'])
                else:
                    self.assertIn('失败原因', manifest)
                    self.assertEqual(manifest['目录导出']['退出码'], 0)
                    self.assertFalse((output / 'global-scope-catalog.json').exists())


class RawScopeManagedPatchTests(unittest.TestCase):
    def test_tail_adds_raw_mode_without_editing_production_gate_or_exporters(self):
        repo = Path(__file__).resolve().parents[2]
        patch_path = repo / 'simc_patches/compatibility/30c4b061f2103132a34952d80c963303b82bf368/0072-raw-scope-audit-catalog.patch'
        text = patch_path.read_text()
        files = [line[6:] for line in text.splitlines() if line.startswith('+++ b/')]
        self.assertEqual(files, ['engine/sc_main.cpp', 'engine/sim/sim.cpp', 'engine/sim/sim.hpp'])
        main = text.split('--- a/engine/sim/sim.cpp')[0]
        self.assertFalse(any(line.startswith('-') and not line.startswith('---') for line in main.splitlines()))
        added = '\n'.join(line[1:] for line in main.splitlines() if line.startswith('+') and not line.startswith('+++'))
        raw, dispatch = added.split('    if ( !skill_damage_raw_scope_export_file.empty() )')
        self.assertIn('git_info::revision()', raw)
        self.assertIn('dbc::client_data_version_str( sim.dbc->ptr )', raw)
        self.assertIn('skill_damage_write_dbc_relation(out,*sim.dbc,effect)', raw)
        self.assertIn('spell_attribute::SX_PASSIVE', raw)
        self.assertEqual(raw.count('bool fe = true;'), 1)
        for forbidden in ('global_effect_indices', 'global_components', 'remove_talent',
                          'component.second', 'skill_damage_declared', 'skill_damage_validate_scope_contract'):
            self.assertNotIn(forbidden, raw)
        for option in ('skill_damage_scope_export_file', 'skill_damage_export_file',
                       'skill_damage_evidence_export_file', 'apl_metadata_export_file'):
            self.assertIn('!' + option + '.empty()', dispatch)
        self.assertIn('throw std::runtime_error', dispatch)


if __name__ == '__main__':
    unittest.main()
