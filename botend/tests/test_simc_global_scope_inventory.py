"""相同字段协议下仍需核对实际公共分量，防止旧二进制生成新数据版本。"""
import copy
from types import SimpleNamespace
from unittest import TestCase

from botend.services.simc_skill_damage import SimcSkillDamageSnapshotService, _validate_global_scope_catalog, _finite_number


class PublicScopeInventoryTests(TestCase):
    def test_hero_scope_uses_selection_nodes_when_hero_spec_field_is_missing(self):
        service, payload = self.fixture(58)
        payload.update(scope_inventory_version=5, probe_context_inventory_version=1,
                       native_registration_inventory_version=1, hero_ownership_inventory_version=1,
                       racial_policy='exclude_all')
        payload['talent_catalog'] += [
            {'trait_entry_id': 201, 'class_id': 12, 'tree_index': 4, 'subtree_id': 126,
             'spell_id': 0, 'spec_ids': [1480], 'starter_spec_ids': [], 'effects': []},
            {'trait_entry_id': 202, 'class_id': 12, 'tree_index': 3, 'subtree_id': 126,
             'spell_id': 200, 'spec_ids': [], 'starter_spec_ids': [], 'effects': []}]
        with self.assertRaisesRegex(ValueError, '英雄节点专精归属与原生选择节点不一致'):
            service._load_global_damage_talent_catalog(payload)
        payload['talent_catalog'][-1]['spec_ids'] = [1480]
        with self.assertRaisesRegex(ValueError, '遗漏公共结果分量'):
            service._load_global_damage_talent_catalog(payload)

    def test_latest_dataset_requires_native_registration_inventory(self):
        service, payload = self.fixture(57)
        payload.update(scope_inventory_version=4, probe_context_inventory_version=1, racial_policy='exclude_all')
        with self.assertRaisesRegex(ValueError, '原生注册来源'):
            service._load_global_damage_talent_catalog(payload)

    def test_new_dataset_requires_explicit_all_racial_exclusion(self):
        service, payload = self.fixture(56)
        payload.update(probe_context_inventory_version=1, scope_inventory_version=3)
        with self.assertRaisesRegex(ValueError, '排除种族能力'):
            service._load_global_damage_talent_catalog(payload)

    def test_actor_contract_rejects_racial_numeric_and_condition_residue(self):
        actor = {'scope_inventory_version': 3, 'racial_policy': 'exclude_all',
                 'excluded_racial_spell_ids': [100], 'normalized_racial_effects': [],
                 'global_scope_candidates': [], 'global_damage_states': [], 'reviewed_global_effects': []}
        _validate_global_scope_catalog(actor)
        actor['actions'] = [{'activation_conditions': [{'spell_id': 100}]}]
        with self.assertRaisesRegex(ValueError, '种族能力仍出现在技能条件'):
            _validate_global_scope_catalog(actor)
        actor['actions'] = []
        actor['normalized_racial_effects'] = [{'spell_id': 100, 'effect_index': 1, 'effect_id': 101,
            'actual_base_value': 0, 'actual_ap_coefficient': 0, 'actual_sp_coefficient': 0,
            'actual_scale_coefficient': 0.1}]
        with self.assertRaisesRegex(ValueError, '种族能力分量未在探测前完整归零'):
            _validate_global_scope_catalog(actor)

    def fixture(self, revision=52):
        snapshot = SimpleNamespace(simc_revision='c' * 40, game_build='12.1.5.70575', schema_revision=revision)
        service = SimcSkillDamageSnapshotService(snapshot, backend=SimpleNamespace())
        payload = {'schema_version': service.EXPORTER_SCHEMA_REVISION,
                   'simc_revision': snapshot.simc_revision, 'game_build': snapshot.game_build,
                   'talents': [], 'talent_catalog': [{'trait_entry_id': 101, 'spell_id': 100,
                       'effects': [{'index': 1, 'type': 6, 'subtype': 290, 'value': 2, 'flags': [0, 0, 0, 0]}]}]}
        if revision >= 58:
            payload['talent_catalog'][0].update(
                class_id=1, tree_index=1, subtree_id=0, spec_ids=[71], starter_spec_ids=[])
        return service, payload

    def test_same_protocol_with_missing_public_effect_is_rejected(self):
        service, payload = self.fixture()
        with self.assertRaisesRegex(ValueError, '遗漏公共结果分量'):
            service._load_global_damage_talent_catalog(payload)
        payload['talent_catalog'][0]['effects'][0]['subtype'] = 71
        payload['talent_catalog'][0]['effects'][0]['type'] = 202
        with self.assertRaisesRegex(ValueError, '遗漏公共结果分量'):
            service._load_global_damage_talent_catalog(payload)

    def test_complete_public_inventory_is_accepted_without_name_or_class_family_whitelist(self):
        service, payload = self.fixture()
        payload['talents'] = [{'trait_entry_id': 101, 'spell_id': 100,
            'evidence': 'dbc_global_damage_talent', 'dbc_base_multiplier': None,
            'remove_talent': True, 'has_rank_scaling': False, 'global_effect_indices': [1],
            'global_components': [{'spell_id': 100, 'effect_indices': [1]}]}]
        self.assertEqual(set(service._load_global_damage_talent_catalog(payload)), {101})
        invalid = copy.deepcopy(payload)
        invalid['talent_catalog'][0]['effects'][0]['flags'] = []
        with self.assertRaisesRegex(ValueError, '证据格式无效'):
            service._load_global_damage_talent_catalog(invalid)

    def test_new_snapshot_requires_inventory_but_legacy_contract_is_preserved(self):
        service, payload = self.fixture()
        payload['talent_catalog'] = []
        with self.assertRaisesRegex(ValueError, '缺少完整原生作用域证据'):
            service._load_global_damage_talent_catalog(payload)
        legacy, payload = self.fixture(51)
        payload['talent_catalog'] = []
        self.assertEqual(legacy._load_global_damage_talent_catalog(payload), {})

    def test_new_snapshot_rejects_old_frozen_inventory_even_with_same_wire_schema(self):
        service, payload = self.fixture(54)
        with self.assertRaisesRegex(ValueError, '完整冻结状态目录版本'):
            service._load_global_damage_talent_catalog(payload)
        payload['scope_inventory_version'] = 2
        with self.assertRaisesRegex(ValueError, '遗漏公共结果分量'):
            service._load_global_damage_talent_catalog(payload)

    def test_pet_auto_attack_and_guardian_categories_cannot_silently_pass_an_empty_inventory(self):
        for carrier, category in ((174, 163), (65, 79), (6, 344), (6, 429), (6, 531), (129, 342)):
            with self.subTest(carrier=carrier, category=category):
                service, payload = self.fixture(54)
                payload['scope_inventory_version'] = 2
                effect = payload['talent_catalog'][0]['effects'][0]
                effect.update(type=carrier, subtype=category)
                with self.assertRaisesRegex(ValueError, '遗漏公共结果分量'):
                    service._load_global_damage_talent_catalog(payload)

    def test_complete_inventory_marker_cannot_hide_missing_or_unexplained_details(self):
        fact = {'scope_evidence': 'dbc_semantic_effect_scope',
                'global_components': [{'spell_id': 100, 'effect_index': 1}]}
        actor = {'scope_inventory_version': 2, 'global_scope_candidates': [],
                 'global_damage_states': [], 'reviewed_global_effects': [fact]}
        with self.assertRaisesRegex(ValueError, '缺少对应效果明细'):
            _validate_global_scope_catalog(actor)
        fact['effect_details'] = [{'source_spell_id': 100, 'effect_index': 1, 'label': '公共效果分量'}]
        with self.assertRaisesRegex(ValueError, '缺少原始效果属性或明确类别'):
            _validate_global_scope_catalog(actor)
        fact['effect_details'][0].update(type=6, subtype=108, misc1=22, misc2=0, label='周期伤害（相应技能类别）')
        _validate_global_scope_catalog(actor)

    def test_new_dataset_requires_execute_context_contract_and_rejects_denormal_values(self):
        service, payload = self.fixture(55)
        payload['scope_inventory_version'] = 2
        with self.assertRaisesRegex(ValueError, '原生执行上下文目录版本'):
            service._load_global_damage_talent_catalog(payload)
        self.assertFalse(_finite_number(9.5143379119735e-311))
        self.assertTrue(_finite_number(0))
        self.assertTrue(_finite_number(1e-280))
