"""Native scope review must reject incomplete or misidentified audit evidence."""
import copy
import hashlib
import json
from unittest.mock import patch
import sys
import tempfile
from pathlib import Path
from django.test import SimpleTestCase
from botend.constants.wow import SPEC_IDENTITY_MAP

SCRIPT_DIR = str(Path(__file__).resolve().parents[2] / 'scripts')
sys.path.insert(0, SCRIPT_DIR)
try:
    import build_simc_native_scope_review as builder
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

    def test_shared_spell_requires_selected_entry_in_observed_hero_tree(self):
        def trait(entry, tree, spell, subtree, specs):
            return dict(trait_entry_id=entry, tree_index=tree, class_id=12,
                        spell_id=spell, subtree_id=subtree, spec_ids=specs, name='ignored')

        catalog = [trait(11, 3, 50, 101, [577, 1480]), trait(12, 3, 50, 102, [577, 1480]),
                   trait(13, 3, 50, 103, [577, 1480]), trait(14, 1, 51, 0, []),
                   trait(21, 4, 0, 101, [577]), trait(22, 4, 0, 102, [1480]),
                   trait(23, 4, 0, 103, [1480])]
        ownership = builder.HeroEntryOwnership(catalog)
        bindings = [{'source_spell_id': 50, 'effect_id': 60, 'target_spell_id': 70},
                    {'source_spell_id': 51, 'effect_id': 61, 'target_spell_id': 71}]

        def project(spec, selected, **wire):
            return ownership.project(12, spec, {'selected_trait_ids': selected, **wire},
                                     bindings, allow_legacy=spec == 577)[1]

        modern = dict(spec_id=1480, valid_hero_subtree_ids=[103, 102])
        # Legacy observation is still bound to a specific spec and selection Entry.
        havoc = project(577, [21, 11])[50]
        devourer = project(1480, [22, 11, 12], **modern)[50]
        self.assertEqual((havoc['owner_entry_id'], devourer['owner_entry_id']), (11, 12))
        self.assertEqual(havoc['bindings'], devourer['bindings'])
        self.assertEqual(devourer['eligible_entry_ids'], [12])
        self.assertEqual(devourer['observed_selection_entry_ids'], [22])
        self.assertNotIn(51, project(577, [21, 11, 14]))
        for selected in ([22, 11], [12], [22, 13], [22, 23, 12]):
            with self.subTest(selected=selected):
                unowned = project(1480, selected, **modern)[50]
                self.assertIsNone(unowned['owner_entry_id'])
                self.assertEqual(unowned['bindings'], bindings[:1])
        for wire in ({}, {'spec_id': 1480}, {**modern, 'spec_id': 577},
                     {**modern, 'valid_hero_subtree_ids': [101, 102]},
                     {**modern, 'valid_hero_subtree_ids': [102, 103, 103]},
                     {**modern, 'valid_hero_subtree_ids': [102.0, 103]}):
            with self.subTest(wire=wire), self.assertRaises(ValueError):
                project(1480, [22, 12], **wire)
        with self.assertRaises(ValueError):
            project(577, [21, 11], spec_id=577)
        with self.assertRaises(ValueError):
            builder.HeroEntryOwnership(catalog + catalog[:1])

    def test_changed_historical_entry_requires_original_review_and_stays_pending(self):
        resolver = builder.ScopeResolver.__new__(builder.ScopeResolver)
        resolver.revision = builder.REVIEWED_REVISION
        resolver.reviewed_build = resolver.reviewed_document()['build']
        eid = 1285572
        fact = resolver.reviewed_facts()[str(eid)]
        resolver.flag_members = {(fact['class_family'], 2, 2147483648): set(fact['affected'])}
        effect = {**fact, 'effect_id': eid,
                  'affected_spells': [{'spell_id': sid} for sid in fact['affected']]}
        layer, field = fact['layers_fields'][0]
        previous = [{'target_spell_id': sid, 'layer': layer, 'field': field, '传递到的伤害技能': fact['damage']}
                    for sid in fact['registered']]
        with self.assertRaisesRegex(ValueError, 'registered 身份/集合漂移'):
            resolver.enforce_reviewed_scope(effect, [])
        resolver.enforce_reviewed_scope(effect, previous)
        with patch.object(builder, 'structural_scope', return_value=('test', 'empty')):
            _, _, decision, _, evidence = builder.resolve_entry_scope(
                resolver, Path('.'), effect, [], [], historical_native=previous)
            self.assertEqual(decision, '待确认')
            self.assertTrue(evidence['禁止后备分类'])
            with self.assertRaisesRegex(ValueError, 'registered 身份/集合漂移'):
                builder.resolve_entry_scope(resolver, Path('.'), effect, [], [],
                                            historical_native=previous[1:])

    def test_hero_spell_aliases_in_one_subtree_use_entry_specialization(self):
        def trait(entry, tree, spell, specs):
            return dict(trait_entry_id=entry, tree_index=tree, class_id=8,
                        spell_id=spell, subtree_id=41, spec_ids=specs)

        ownership = builder.HeroEntryOwnership([
            trait(117239, 3, 431044, [64]), trait(136441, 3, 431044, [63]),
            trait(81, 3, 80, [64]), trait(82, 3, 80, []),
            trait(91, 3, 90, [64]), trait(92, 3, 90, [64]),
            trait(70, 4, 0, [63, 64]),
        ])

        def project(spec, selected, spell=431044):
            binding = dict(source_spell_id=spell, effect_id=50, target_spell_id=51)
            actor = dict(spec_id=spec, valid_hero_subtree_ids=[41], selected_trait_ids=[70, *selected])
            return ownership.project(8, spec, actor, [binding])[1][spell]

        foreign = project(63, [117239])
        self.assertIsNone(foreign['owner_entry_id'])
        self.assertEqual(len(foreign['bindings']), 1)
        self.assertEqual(project(63, [117239, 136441])['owner_entry_id'], 136441)
        self.assertEqual(project(64, [117239, 136441])['owner_entry_id'], 117239)
        # A unique unrestricted alias takes precedence over a spec-specific alias.
        self.assertIsNone(project(64, [81], spell=80)['owner_entry_id'])
        self.assertEqual(project(64, [81, 82], spell=80)['owner_entry_id'], 82)
        # A rank-zero tuple is not an activation witness and cannot resolve an ambiguous lookup.
        self.assertIsNone(project(64, [91], spell=90)['owner_entry_id'])

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

    def test_migration_pending_cannot_be_overridden_by_handwritten_fallback(self):
        resolver = builder.ScopeResolver.__new__(builder.ScopeResolver)
        resolver.revision = self.REVISION
        effect = {'effect_id': 999999999, 'type': 6, 'subtype': 108,
                  'misc1': 0, 'misc2': 0, 'flags': [512,0,0,0],
                  'class_family': 4, 'method': 'dbc.effect_affects_spells',
                  'affected_spells': [{'spell_id': 1}]}
        with patch.object(builder, 'classify_reads', return_value=('保留', 'fallback', [])) as fallback:
            _, _, decision, _, evidence = builder.resolve_component_scope(
                resolver, self.input_root, effect, [], [])
        self.assertEqual(decision, '待确认')
        self.assertTrue(evidence['禁止后备分类'])
        fallback.assert_not_called()

    def test_draft_is_complete_separate_and_never_formal(self):
        row = {'类型': '天赋', '名称': 'test', '法术ID': 10, '分量': [
            {'效果ID': 20, '处理结论': '待确认', 'DBC': {'effect_id': 20}},
            {'效果ID': 21, '处理结论': '保留', 'DBC': {'effect_id': 21}},
        ]}
        review = {'标题': 'test', '判断依据': 'evidence', '说明': 'coverage',
                  '源码提交': self.REVISION, '条目': [row], '数量': {'天赋': 1}}
        output = self.input_root / 'review'
        output.with_suffix('.json').write_text('frozen formal', encoding='utf-8')
        builder.write_review(review, output, draft=True)
        draft = json.loads(output.with_suffix('.draft.json').read_text(encoding='utf-8'))
        self.assertIs(draft['draft'], True)
        self.assertEqual(draft['review_status'], 'draft')
        self.assertIs(draft['compile_contract_allowed'], False)
        self.assertEqual(draft['条目'], review['条目'])
        self.assertEqual(draft['待确认清单'], [{'法术ID': 10, '效果ID': 20, '名称': 'test'}])
        self.assertEqual(output.with_suffix('.json').read_text(), 'frozen formal')
        self.assertTrue(output.with_suffix('.draft.html').is_file())
        with self.assertRaisesRegex(ValueError, '未完成'):
            builder.write_review(review, output)
        resolved = copy.deepcopy(review)
        resolved['条目'][0]['分量'][0]['处理结论'] = '保留'
        with self.assertRaisesRegex(ValueError, '未复核.*revision'):
            builder.write_review(resolved, output)
        builder.write_review(resolved, self.input_root / 'resolved', draft=True)
        resolved_draft = json.loads((self.input_root / 'resolved.draft.json').read_text())
        self.assertIs(resolved_draft['draft'], True)
        self.assertIs(resolved_draft['compile_contract_allowed'], False)
        resolved['源码提交'] = builder.REVIEWED_REVISION
        builder.write_review(resolved, self.input_root / 'formal')
        self.assertNotIn('draft', json.loads((self.input_root / 'formal.json').read_text()))
        resolved['draft'] = True
        with self.assertRaisesRegex(ValueError, '草稿'):
            builder.write_review(resolved, output)

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
        row = next(r for r in manifest['结果'] if Path(r['输入']).name == 'warrior_fury.simc')
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
