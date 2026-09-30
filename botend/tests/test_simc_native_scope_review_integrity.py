"""Native scope review must reject incomplete or misidentified audit evidence."""
import copy
import hashlib
import sys
import tempfile
from pathlib import Path
from django.test import SimpleTestCase
from botend.constants.wow import SPEC_IDENTITY_MAP

SCRIPT_DIR = str(Path(__file__).resolve().parents[2] / 'scripts')
sys.path.insert(0, SCRIPT_DIR)
try:
    from audit_simc_global_damage_initialization import SPECS
    from build_simc_native_scope_review import (
        canonical_scope_evidence, validate_native_actor_identity,
        validate_native_audit_manifest,
    )
finally:
    sys.path.remove(SCRIPT_DIR)


class NativeScopeReviewIntegrityTests(SimpleTestCase):
    REVISION = '1' * 40
    BUILD = '12.1.0.69814'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.input_root = Path(temporary.name)
        self.frozen_input_dir = Path('backups/frozen')

    def test_source_evidence_order_does_not_change_review_identity(self):
        first = {'文件': 'engine/a.cpp', '行': 7, '类型': 'a_t'}
        second = {'文件': 'engine/b.cpp', '行': 3, '类型': 'b_t'}
        left = {'判定路径': '原生手写范围', '源码': [first, second]}
        right = {'判定路径': '原生手写范围', '源码': [second, first]}
        self.assertEqual(canonical_scope_evidence(left), canonical_scope_evidence(right))

    def audit(self):
        runs = []
        unsupported = {'monk_mistweaver': 'Mistweaver Monk',
                       'paladin_holy': 'Holy Paladin'}
        for stage in ('base', 'talents'):
            for spec_id, spec in SPECS.items():
                stem = f'{SPEC_IDENTITY_MAP[spec_id][0].lower()}_{spec}'
                path = Path(f'backups/frozen/{stage}/{stem}.simc')
                content = f'{stage}/{stem}\n'.encode()
                frozen = self.input_root / path
                frozen.parent.mkdir(parents=True, exist_ok=True)
                frozen.write_bytes(content)
                row = {'输入': str(path), '输入摘要': hashlib.sha256(content).hexdigest(),
                       '退出码': 0}
                if stem in unsupported:
                    row['退出码'] = 40
                    row['失败原因'] = (
                        ("Trivial: Buff 'touch_of_death_ww' (0) initialized with max_stack < 1 (0). Setting max_stack to 1.\n"
                         if stem == 'monk_mistweaver' else '')
                        + f"Trivial: {unsupported[stem]} for Player 'audit' is not currently supported.\n"
                        + 'Error: No active players in sim!\n'
                    )
                runs.append(row)
        return ({'源码提交': self.REVISION, '客户端版本': self.BUILD,
                 '结果': runs},
                {'simc_revision': self.REVISION, 'game_build': self.BUILD})

    def test_complete_audit_allows_only_explicit_unsupported_specs(self):
        manifest, catalog = self.audit()
        successes, unsupported = validate_native_audit_manifest(
            manifest, catalog, input_root=self.input_root,
            frozen_input_dir=self.frozen_input_dir)
        self.assertEqual(len(successes), 76)
        self.assertEqual(len(unsupported), 4)

    def test_missing_duplicate_or_unexpected_failure_blocks_review(self):
        manifest, catalog = self.audit()
        cases = []
        missing = copy.deepcopy(manifest)
        missing['结果'].pop(0)
        cases.append(missing)
        duplicate = copy.deepcopy(manifest)
        duplicate['结果'].append(copy.deepcopy(duplicate['结果'][0]))
        cases.append(duplicate)
        unexpected = copy.deepcopy(manifest)
        unexpected['结果'][0]['退出码'] = 1
        unexpected['结果'][0]['失败原因'] = 'unexpected exporter failure'
        cases.append(unexpected)
        false_unsupported = copy.deepcopy(manifest)
        row = next(r for r in false_unsupported['结果'] if r['退出码'])
        row['失败原因'] = 'Error: No active players in sim!\n'
        cases.append(false_unsupported)
        fatal_mixed = copy.deepcopy(manifest)
        row = next(r for r in fatal_mixed['结果'] if r['退出码'])
        row['退出码'] = 139
        row['失败原因'] += 'Segmentation fault\n'
        cases.append(fatal_mixed)
        fatal_with_known_code = copy.deepcopy(manifest)
        row = next(r for r in fatal_with_known_code['结果'] if r['退出码'])
        row['失败原因'] += 'Segmentation fault\n'
        cases.append(fatal_with_known_code)
        missing_hash = copy.deepcopy(manifest)
        missing_hash['结果'][0].pop('输入摘要')
        cases.append(missing_hash)
        changed_hash = copy.deepcopy(manifest)
        changed_hash['结果'][0]['输入摘要'] = '0' * 64
        cases.append(changed_hash)
        extra_trivial = copy.deepcopy(manifest)
        row = next(r for r in extra_trivial['结果'] if r['退出码'])
        row['失败原因'] += 'Trivial: unrelated fatal diagnostic\n'
        cases.append(extra_trivial)
        other_directory = copy.deepcopy(manifest)
        original_path = Path(other_directory['结果'][0]['输入'])
        alternative = Path('other/input') / original_path.parent.name / original_path.name
        replacement = self.input_root / alternative
        replacement.parent.mkdir(parents=True, exist_ok=True)
        replacement.write_bytes((self.input_root / original_path).read_bytes())
        other_directory['结果'][0]['输入'] = str(alternative)
        cases.append(other_directory)
        for candidate in cases:
            with self.subTest(kind=len(candidate['结果']), error=candidate['结果'][0]['退出码']):
                with self.assertRaises(ValueError):
                    validate_native_audit_manifest(
                        candidate, catalog, input_root=self.input_root,
                        frozen_input_dir=self.frozen_input_dir)

    def test_catalog_revision_and_build_must_equal_frozen_manifest(self):
        manifest, catalog = self.audit()
        for wrong in ({**catalog, 'game_build': '12.1.0.69875'},
                      {**catalog, 'simc_revision': '0' * 40}):
            with self.subTest(catalog=wrong), self.assertRaises(ValueError):
                validate_native_audit_manifest(
                    manifest, wrong, input_root=self.input_root,
                    frozen_input_dir=self.frozen_input_dir)

    def test_successful_payload_must_contain_the_exact_spec_actor(self):
        manifest, _ = self.audit()
        row = next(r for r in manifest['结果'] if r['输入'].endswith('/warrior_fury.simc'))
        valid = {'actors': [{'class': 'warrior', 'spec': 'Fury Warrior'}]}
        validate_native_actor_identity(row, valid)
        helper = {'class': 'player_simplified', 'spec': 'Unknown',
                  'selected_trait_ids': [], 'bindings': [], 'target_states': [],
                  'source_effects': [], 'damage_actions': [
                      {'spell_id': 0, 'token': 'simple_spell'},
                      {'spell_id': 0, 'token': 'simple_proc'},
                  ]}
        with_helper = {'actors': [valid['actors'][0], helper]}
        self.assertEqual(validate_native_actor_identity(row, with_helper), valid['actors'][0])
        for payload in ({'actors': []},
                        {'actors': [{'class': 'warrior', 'spec': 'Arms Warrior'}]},
                        {'actors': [{'class': 'mage', 'spec': 'Fury Warrior'}]},
                        {'actors': valid['actors'] * 2},
                        {'actors': [valid['actors'][0], {'class': 'mage', 'spec': 'Arcane Mage'}]},
                        {'actors': [valid['actors'][0], {**helper,
                            'source_effects': [{'effect_id': 1}]}]},
                        {'actors': [valid['actors'][0], {**helper,
                            'damage_actions': [{'spell_id': 1, 'token': 'foreign_skill'}]}]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_native_actor_identity(row, payload)
