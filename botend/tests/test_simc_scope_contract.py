"""作用域契约必须拒绝冲突，并按分量保留混合状态。"""
import copy
import unittest
from pathlib import Path
from scripts.build_simc_scope_contract import compile_contract, render_contract, display_details
from scripts.simc_scope_resolution import ScopeResolver


class ScopeContractTests(unittest.TestCase):
    def test_project_scope_patch_preserves_deployed_ledger_prefix(self):
        import hashlib
        patch_path = (
            Path(__file__).resolve().parents[2]
            / 'simc_patches/0059-refresh-reviewed-scope-contract-ac0f.patch'
        )
        patch = patch_path.read_text(encoding='utf-8')
        self.assertEqual(
            hashlib.sha256(patch_path.read_bytes()).hexdigest(),
            'aae132fd139f9fa3023ef65d0cf8b3892db373069c971b57ac01dd866fd1f2d9',
        )
        self.assertIn('skill_damage_scope_revision = "ac0f3a3c7ff9e521137c0ca1760d548330c697f3";', patch)
        self.assertIn('skill_damage_scope_build = "12.1.0.69814";', patch)
        self.assertNotIn('skill_damage_scope_revision = "9f6eac065914e82578de398c08201dffc2885124";', patch)

    def test_scope_contract_requires_exact_reviewed_revision_and_build(self):
        # A newer build is a new DBC universe and cannot reuse the frozen review.
        enabled_patches = {p.name for p in (
            Path(__file__).resolve().parents[2] / 'simc_patches'
        ).glob('*.patch')}
        self.assertNotIn('0060-allow-newer-scope-contract-builds.patch', enabled_patches)
        old_contract_patch = (
            Path(__file__).resolve().parents[2]
            / 'simc_patches/0059-refresh-reviewed-scope-contract-ac0f.patch'
        ).read_text(encoding='utf-8')
        self.assertIn('skill_damage_scope_revision = "ac0f3a3c7ff9e521137c0ca1760d548330c697f3";', old_contract_patch)

    def review(self):
        def part(index, decision):
            return {'源法术ID':31884, '效果编号':index, '效果ID':100+index, '处理结论':decision,
                    '范围核验': {'判定路径': 'DBC 部分技能选择器',
                                 '完整DBC集合': [184575] if decision == '保留' else []},
                    'DBC':{'source_spell_id':31884,'effect_index':index,'effect_id':100+index,'class_family':10}}
        return {'源码提交':'a'*40,'客户端版本':'12.1.0.69587','条目':[
            {'类型':'自身Buff','法术ID':31884,'分量':[part(1,'应剔除'),part(12,'保留')]}]}

    def test_mixed_buff_keeps_local_component(self):
        effects, _, buffs = compile_contract(self.review())
        self.assertTrue(effects[(31884,1)][2])
        self.assertFalse(effects[(31884,12)][2])
        self.assertEqual(buffs[31884], [True,True])

    def test_display_fact_records_global_and_local_components_separately(self):
        review = self.review()
        review['条目'][0].update({'职业': '战士', '专精': '狂怒', '名称': '测试',
                                  '范围判定': 'SimC 原生局部技能证据'})
        rendered = render_contract(review, 'b' * 64)
        self.assertIn(r'\"global_components\":[{\"spell_id\":31884,\"effect_index\":1', rendered)
        self.assertIn(r'\"local_components\":[{\"spell_id\":31884,\"effect_index\":12', rendered)
        self.assertIn(r'\"local_skill_bindings\":[{\"spell_id\":31884,\"effect_index\":12,\"effect_id\":112,\"skill_spell_ids\":[184575]', rendered)
        self.assertIn(r'\"lower_skill_policy\":\"exclude_global_keep_explicit_local\"', rendered)
        self.assertIn(r'\"local_scope_evidence\":\"SimC \u539f\u751f\u5c40\u90e8\u6280\u80fd\u8bc1\u636e\"', rendered)

    def test_talent_and_target_state_share_all_self_components(self):
        review=self.review()
        talent=copy.deepcopy(review['条目'][0])
        talent['类型']='天赋'
        talent['分量']=talent['分量'][1:]
        review['条目'].append(talent)
        _,parents,_=compile_contract(review)
        self.assertIn((31884,31884,1),parents)
        self.assertNotIn((31884,31884,12),parents)

    def test_conflicting_component_is_rejected(self):
        review = self.review()
        duplicate = copy.deepcopy(review['条目'][0])
        duplicate['分量'][0]['处理结论'] = '保留'
        review['条目'].append(duplicate)
        with self.assertRaisesRegex(ValueError,'冲突'):
            compile_contract(review)

    def test_unresolved_and_wrong_dbc_identity_are_rejected(self):
        for field, value in [('处理结论','待确认'),('效果ID',999)]:
            review = self.review()
            review['条目'][0]['分量'][0][field] = value
            with self.assertRaises(ValueError):
                compile_contract(review)

    def test_description_cannot_change_contract(self):
        review = self.review()
        original = render_contract(review, 'b'*64)
        review['条目'][0]['描述仅供参考'] = '提高所有技能伤害'
        self.assertEqual(render_contract(review, 'b'*64), original)

    def test_display_contract_carries_numeric_specialization_scope(self):
        review = self.review()
        review['条目'][0].update({'职业': '战士', '专精': '狂怒', '名称':'测试'})
        rendered = render_contract(review, 'b'*64)
        self.assertIn('specializations', rendered)
        self.assertIn('fury', rendered)
        self.assertIn('"fury",', rendered)
        self.assertNotIn('arms', rendered)

    def test_display_contract_narrows_precreated_target_state_to_talent_owner(self):
        review = self.review()
        review['条目'][0].update({'类型': '目标减益', '职业': '法师', '专精': '冰霜、奥术、火焰',
                                  '法术ID': 210824, '名称': '大法师之触'})
        review['条目'][0]['分量'][0]['源法术ID'] = 210824
        review['条目'][0]['分量'][0]['DBC'].update(
            source_spell_id=210824, class_family=3,
        )
        rendered = render_contract(review, 'b' * 64)
        self.assertIn('{3, "arcane"', rendered)
        self.assertNotIn('{3, "fire"', rendered)
        self.assertNotIn('{3, "frost"', rendered)

    def test_conditional_mastery_keeps_buff_switch_and_quantitative_source(self):
        review=self.review()
        row=review['条目'][0]
        row['分量']=row['分量'][:1]
        part=row['分量'][0]
        part['DBC'].update(subtype=108,misc1=0,base_value=0,mastery_scaled=True,mastery_coefficient=0.014)
        part['原生实际应用']=[{'layer':'conditional_action_registry','conditional':True}]
        _,_,buffs=compile_contract(review)
        self.assertEqual(buffs[31884],[True,True])
        detail=display_details(row)[0]
        self.assertEqual(detail['value_kind'],'mastery')
        self.assertEqual(detail['coefficient'],0.014)

    def test_critical_chance_and_damage_components_keep_distinct_units(self):
        row=self.review()['条目'][0]
        for part,subtype,prop,value in zip(row['分量'],[107,108],[7,0],[20,15]):
            part['处理结论']='应剔除'
            part['DBC'].update(subtype=subtype,misc1=prop,base_value=value)
        details=display_details(row)
        self.assertEqual([d['value_kind'] for d in details],['percentage_points','percent'])
        self.assertEqual([d['base_value'] for d in details],[20,15])


class ReviewedScopeIdentityTests(unittest.TestCase):
    """真实复核签名的完整集合必须先于任何宽松分类通过门禁。"""

    def setUp(self):
        self.assertTrue(hasattr(ScopeResolver, 'enforce_reviewed_scope'),
                        '正式复核目前没有 fail-closed exact-set 门禁')
        import json
        self.audit = json.loads((Path(__file__).resolve().parents[2] /
                                 'scripts/simc_scope_reviewed_1d3897.json').read_text())
        self.resolver = ScopeResolver.__new__(ScopeResolver)
        self.resolver.revision = self.audit['revision']
        self.resolver.reviewed_build = self.audit['build']

    def test_reviewed_scope_requires_exact_build_catalog_gate(self):
        from pathlib import Path
        with self.assertRaisesRegex(ValueError, 'build'):
            ScopeResolver(Path('/missing-simc-source'), self.audit['revision'], {
                'simc_revision': self.audit['revision'], 'game_build': '12.1.0.69875',
            })
        with self.assertRaisesRegex(ValueError, 'revision'):
            ScopeResolver(Path('/missing-simc-source'), self.audit['revision'], {
                'simc_revision': '0' * 40, 'game_build': self.audit['build'],
            })
        dbc, bindings = self.component(341514)
        del self.resolver.reviewed_build
        with self.assertRaisesRegex(ValueError, 'build'):
            self.resolver.enforce_reviewed_scope(dbc, bindings)

    def component(self, effect_id):
        fact = self.audit['effects'][str(effect_id)]
        dbc = {k: copy.deepcopy(fact[k]) for k in (
            'effect_id', 'source_spell_id', 'effect_index', 'class_family',
            'type', 'subtype', 'misc1', 'misc2', 'method', 'flags',
            'base_value', 'mastery_coefficient', 'mastery_scaled')}
        dbc['affected_spells'] = [{'spell_id': sid} for sid in fact['affected']]
        bindings = [{'target_spell_id': sid, '传递到的伤害技能': []}
                    for sid in fact['registered']]
        bindings += [{'target_spell_id': 0, '传递到的伤害技能': [sid]}
                     for sid in fact['damage']]
        for binding in bindings:
            binding.update(layer=fact['layers_fields'][0][0], field=fact['layers_fields'][0][1])
        return dbc, bindings

    def test_reviewed_revision_and_complete_identity_are_mandatory(self):
        effect = 341514  # Fury Recklessness, public mask, not a local damage component.
        dbc, bindings = self.component(effect)
        self.assertEqual(self.resolver.enforce_reviewed_scope(dbc, bindings),
                         '已复核公共掩码')
        for key, value in [('source_spell_id', 1), ('effect_index', 99),
                           ('class_family', 3), ('type', 35), ('subtype', 108),
                           ('misc1', 0), ('misc2', 963), ('method', 'dbc.spells_by_label'),
                           ('flags', [0, 0, 0, 0]), ('base_value', 999),
                           ('mastery_coefficient', 99), ('mastery_scaled', True)]:
            changed = copy.deepcopy(dbc)
            changed[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.resolver.enforce_reviewed_scope(changed, bindings)
        for changed_revision in ('ac0f3a3c7ff9e521137c0ca1760d548330c697f3', '0' * 40):
            self.resolver.revision = changed_revision
            with self.subTest(revision=changed_revision), self.assertRaises(ValueError):
                self.resolver.enforce_reviewed_scope(dbc, bindings)

    def test_missing_added_and_wrong_native_sets_cannot_fall_back(self):
        for effect in (341514, 1015106, 1277870):
            dbc, bindings = self.component(effect)
            for target in ([], dbc['affected_spells'][1:],
                           dbc['affected_spells'] + [{'spell_id': 99999999}]):
                changed = copy.deepcopy(dbc)
                changed['affected_spells'] = target
                with self.subTest(effect=effect, target=target[:1]), self.assertRaises(ValueError):
                    self.resolver.enforce_reviewed_scope(changed, bindings)
            for changed_bindings in ([], bindings[:-1],
                                     bindings + [{'target_spell_id': 99999999,
                                                  '传递到的伤害技能': [99999999],
                                                  'layer': 'wrong', 'field': 'wrong'}],
                                     bindings + [{'target_spell_id': 99999999,
                                                  '传递到的伤害技能': [],
                                                  'layer': self.audit['effects'][str(effect)]['layers_fields'][0][0],
                                                  'field': self.audit['effects'][str(effect)]['layers_fields'][0][1]}],
                                     bindings + [{'target_spell_id': 0,
                                                  '传递到的伤害技能': [99999999],
                                                  'layer': self.audit['effects'][str(effect)]['layers_fields'][0][0],
                                                  'field': self.audit['effects'][str(effect)]['layers_fields'][0][1]}]):
                with self.subTest(effect=effect, bindings=len(changed_bindings)), self.assertRaises(ValueError):
                    self.resolver.enforce_reviewed_scope(dbc, changed_bindings)
            if bindings:
                changed = copy.deepcopy(bindings)
                changed[0]['layer'] = 'wrong'
                with self.assertRaises(ValueError):
                    self.resolver.enforce_reviewed_scope(dbc, changed)
                changed = copy.deepcopy(bindings)
                changed[0]['field'] = 'wrong'
                with self.assertRaises(ValueError):
                    self.resolver.enforce_reviewed_scope(dbc, changed)

    def test_new_or_missing_reviewed_effect_fails_closed(self):
        dbc, bindings = self.component(341514)
        dbc['effect_id'] = 99999999
        with self.assertRaises(ValueError):
            self.resolver.enforce_reviewed_scope(dbc, bindings)
        with self.assertRaises(ValueError):
            self.resolver.resolve(dbc, bindings, '已解析技能应用关系')
        with self.assertRaises(ValueError):
            self.resolver.validate_reviewed_inventory(set(self.audit['effects']) - {'341514'})

    def test_conditional_category_mixed_talent_is_preserved_not_called_pure_local(self):
        unholy, native = self.component(1000040)
        decision, _, evidence = self.resolver.resolve(unholy, native, '已解析技能应用关系')
        self.assertEqual(decision, '保留')
        self.assertEqual(evidence['判定路径'], '已复核混合条件类别保留')
        self.assertEqual(evidence['条件性类别目标'], 327096)
        self.assertEqual(evidence['类别伤害类型'], '守护者')
        changed = copy.deepcopy(native)
        changed[0]['target_spell_id'] = 326984
        with self.assertRaisesRegex(ValueError, '身份/集合漂移'):
            self.resolver.resolve(unholy, changed, '已解析技能应用关系')

    def test_reviewed_identity_cannot_be_reclassified_through_a_different_path(self):
        dbc, bindings = self.component(341514)
        with self.assertRaisesRegex(ValueError, '判定路径漂移'):
            self.resolver.resolve(dbc, bindings, '公共伤害乘区')

    def test_fury_public_component_and_unaffected_local_component_remain_separate(self):
        dbc, bindings = self.component(341514)
        self.resolver.spells = {sid: {'_id': sid, '_class_flags_family': 4,
                                      '_class_flags': [512, 0, 0, 0]}
                                for sid in self.audit['effects']['341514']['affected']}
        decision, _, evidence = self.resolver.resolve(dbc, bindings, '已解析技能应用关系')
        self.assertEqual((decision, evidence['判定路径']), ('应剔除', '已复核公共掩码'))
        local = {'effect_id': 1218720, 'source_spell_id': 1719, 'effect_index': 13,
                 'class_family': 4, 'type': 6, 'subtype': 108, 'misc1': 15, 'misc2': 0,
                 'method': 'dbc.effect_affects_spells', 'flags': [0, 8192, 0, 0],
                 'affected_spells': [{'spell_id': sid} for sid in (85384, 96103, 335098, 335100)]}
        self.resolver.damage = {85384, 96103, 335098, 335100, 23881}
        self.resolver.damage_families = {4: self.resolver.damage}
        decision, _, evidence = self.resolver.resolve(local, [], '已解析技能应用关系')
        self.assertEqual((decision, evidence['判定路径']), ('保留', 'DBC 部分技能选择器'))
