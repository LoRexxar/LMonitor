"""证明装备适用检查发生在任务冻结和 Run 创建之前。"""
from unittest.mock import patch
import json

from django.test import TestCase
from django.contrib.auth.models import User

from botend.models import WowItemSnapshot, WowItemVariantSnapshot, SeasonMeta, SimcTask
from botend.services.simc_equipment_eligibility import EquipmentEligibility
from botend.services.simc_benchmark_config import build_execution_plan, serialize_panel_config, _normalize_candidate_params
from botend.services.simc_benchmark_execution import serialize_incremental_panel_results
from botend.services.simc_task_service import create_task, initialize_task_runs, TaskCreationError
from botend.tests import test_simc_equipment_control as equipment_fixtures


def equipment(item_id, *, inventory=12, subclass=0, primary=(), class_mask=0, eligible=()):
    return WowItemSnapshot.objects.update_or_create(item_id=item_id, defaults={
        'item_class_id': 4, 'item_subclass_id': subclass, 'inventory_type': inventory,
        'allowable_class_mask': class_mask, 'eligible_specs': list(eligible),
        'metadata': {'primary_stat_options': list(primary)}})[0]


def params(item_id, slot='trinket1'):
    return {'candidate_type': 'gear_swap', 'gear_swap': {
        'item_id': item_id, 'slot': slot, 'raw_value': f',id={item_id},ilevel=289'}}


class EquipmentEligibilityTests(TestCase):
    def test_primary_stats_are_specialization_specific_and_neutral_items_remain(self):
        equipment(1, primary=('agility', 'intellect'))
        equipment(2, primary=('strength', 'agility', 'intellect'))
        equipment(3, primary=())
        eligibility = EquipmentEligibility([params(i) for i in (1, 2, 3)])
        self.assertEqual(eligibility.reason(params(1), 'warrior_fury')['code'], 'incompatible')
        self.assertIsNone(eligibility.reason(params(1), 'paladin_holy'))
        self.assertIsNone(eligibility.reason(params(1), 'druid_feral'))
        for item_id in (2, 3):
            self.assertIsNone(eligibility.reason(params(item_id), 'warrior_fury'))
            self.assertIsNone(eligibility.reason(params(item_id), 'mage_fire'))

    def test_cloth_cannot_use_plate_and_cloaks_are_not_filtered_as_cloth_armor(self):
        equipment(1, inventory=5, subclass=4, primary=('strength', 'intellect'))
        equipment(2, inventory=5, subclass=1, primary=('intellect',))
        equipment(3, inventory=16, subclass=1, primary=('strength', 'agility', 'intellect'))
        eligibility = EquipmentEligibility([params(1, 'chest'), params(2, 'chest'), params(3, 'back')])
        self.assertIsNotNone(eligibility.reason(params(1, 'chest'), 'mage_fire'))
        self.assertIsNone(eligibility.reason(params(1, 'chest'), 'paladin_holy'))
        self.assertIsNone(eligibility.reason(params(2, 'chest'), 'mage_fire'))
        self.assertIsNone(eligibility.reason(params(3, 'back'), 'warrior_fury'))
        self.assertEqual(eligibility.reason(params(3, 'chest'), 'warrior_fury')['code'], 'wrong_slot')

    def test_class_masks_weapon_slots_and_missing_metadata(self):
        equipment(1, class_mask=128)
        weapon = equipment(2, inventory=17, subclass=8, primary=('strength',))
        weapon.item_class_id = 2
        weapon.save()
        eligibility = EquipmentEligibility([params(1), params(2, 'off_hand'), params(3)])
        self.assertIsNotNone(eligibility.reason(params(1), 'warrior_fury'))
        self.assertIsNone(eligibility.reason(params(1), 'mage_fire'))
        self.assertIsNone(eligibility.reason(params(2, 'off_hand'), 'warrior_fury'))
        self.assertIsNotNone(eligibility.reason(params(2, 'off_hand'), 'paladin_retribution'))
        self.assertEqual(eligibility.reason(params(3), 'warrior_fury')['code'], 'missing_metadata')

    def test_active_variant_fills_primary_identity_without_using_old_batch(self):
        item = equipment(1)
        item.metadata = {}
        item.save()
        season = SeasonMeta.objects.create(season_key='eligibility', season_name='验收', mplus_zone_id=1, raid_zone_id=2, is_active=True, gear_batch_key='current')
        for batch, stat in (('old', 'strength'), ('current', 'intellect')):
            WowItemVariantSnapshot.objects.create(item=item, season=season, batch_key=batch,
                variant_key=batch, variant_type='drop_equipment', item_level=289, stats_json={stat: 100})
        eligibility = EquipmentEligibility([params(1)])
        self.assertIsNotNone(eligibility.reason(params(1), 'warrior_fury'))
        self.assertIsNone(eligibility.reason(params(1), 'mage_fire'))

    def test_combination_is_rejected_as_a_whole_and_queries_are_batched(self):
        equipment(1, inventory=9, subclass=4, primary=('strength',))
        equipment(2, inventory=16, subclass=1, primary=('intellect',))
        group = {'candidate_type': 'gear_swap', 'gear_swaps': [params(1, 'wrists')['gear_swap'], params(2, 'back')['gear_swap']]}
        with self.assertNumQueries(2):
            eligibility = EquipmentEligibility([group] * 30)
            for _ in range(40):
                self.assertEqual(eligibility.reason(group, 'warrior_fury')['item_id'], 2)

    def test_incomplete_armor_type_and_main_hand_only_weapon_are_not_accepted(self):
        equipment(1, inventory=5, subclass=0, primary=('intellect',))
        weapon = equipment(2, inventory=21, subclass=7, primary=('agility',))
        weapon.item_class_id = 2
        weapon.save()
        eligibility = EquipmentEligibility([params(1, 'chest'), params(2, 'off_hand')])
        self.assertEqual(eligibility.reason(params(1, 'chest'), 'mage_fire')['code'], 'missing_metadata')
        self.assertEqual(eligibility.reason(params(2, 'off_hand'), 'rogue_outlaw')['code'], 'wrong_slot')


class EquipmentEligibilityTaskTests(TestCase):
    user_id = 701
    _create = equipment_fixtures.EquipmentControlBenchmarkTests._create
    _run = equipment_fixtures.EquipmentControlBenchmarkTests._run
    finish = equipment_fixtures.EquipmentControlBenchmarkTests.finish

    def setUp(self):
        equipment_fixtures.EquipmentControlBenchmarkTests.setUp(self)
        equipment(123, primary=('agility',))
        equipment(456, inventory=11)

    def test_legacy_gear_params_without_type_filter_plan_preview_and_task_runs(self):
        trinket = self.panel.candidates.get(key='trinket')
        trinket.params.pop('candidate_type')
        trinket.save(update_fields=['params'])
        equipment(9, inventory=16, subclass=1, primary=('intellect',), class_mask=128)
        self.ring.params = _normalize_candidate_params('gear_swap',
            'back=,id=9,ilevel=289\nfinger1=,id=456,ilevel=289')
        self.ring.params.pop('candidate_type')
        self.ring.save(update_fields=['params'])

        plan = build_execution_plan(self.panel)
        self.assertEqual([row['candidate_key'] for row in plan['cases'][0]['candidates']], ['baseline'])
        self.assertEqual((plan['case_count'], plan['run_count']), (1, 1))
        self.assertEqual({row['candidate_key'] for row in plan['excluded_candidates']}, {'trinket', 'ring'})
        self.assertTrue(all(row['excluded_specs'] for row in serialize_panel_config(self.panel)['candidates']))
        from botend.services.simc_benchmark_targeting import coordinate_key, rerun_preview
        preview = rerun_preview(plan, {'coordinate_keys': [coordinate_key(plan['cases'][0])]})
        self.assertEqual((preview['case_count'], preview['run_count']), (1, 1))
        execution = self._create()
        task = execution.cases.get().task
        initialize_task_runs(task)
        self.assertEqual(list(task.simulation_runs.values_list('candidate_key', flat=True)), ['baseline'])
        eligibility = EquipmentEligibility([])
        for candidate_type in ('base', 'option_toggle', 'talent_override'):
            self.assertIsNone(eligibility.reason({'candidate_type': candidate_type}, 'warrior_fury'))

    def test_benchmark_filters_before_freezing_controls_and_task_runs(self):
        plan = build_execution_plan(self.panel)
        keys = [row['candidate_key'] for row in plan['cases'][0]['candidates']]
        self.assertNotIn('trinket', keys)
        self.assertEqual(plan['run_count'], 3)
        self.assertEqual(plan['excluded_candidates'][0]['candidate_key'], 'trinket')
        execution = self._create()
        task = execution.cases.get().task
        initialize_task_runs(task)
        self.assertNotIn('trinket', list(task.simulation_runs.values_list('candidate_key', flat=True)))
        self.assertEqual(task.simulation_runs.count(), 3)
        self.assertTrue(next(row for row in serialize_panel_config(self.panel)['candidates']
                             if row['key'] == 'trinket')['excluded_specs'])

    def test_incompatible_combination_generates_neither_candidate_nor_control(self):
        equipment(9, inventory=5, subclass=1, primary=('intellect',))
        self.ring.params = _normalize_candidate_params('gear_swap',
            'chest=,id=9,ilevel=289\nfinger1=,id=456,ilevel=289')
        self.ring.save()
        plan = build_execution_plan(self.panel)
        self.assertEqual([row['candidate_key'] for row in plan['cases'][0]['candidates']], ['baseline'])
        self.assertEqual(len(plan['excluded_candidates']), 2)

    def test_existing_invalid_results_are_absent_from_current_projection_and_not_rescheduled(self):
        item = WowItemSnapshot.objects.get(item_id=123)
        item.metadata = {'primary_stat_options': ['strength']}
        item.save()
        self.finish()
        item.metadata = {'primary_stat_options': ['agility']}
        item.save()
        rows = serialize_incremental_panel_results(self.panel)['coordinates'][0]['candidates']
        self.assertNotIn('trinket', [row['key'] for row in rows])
        self.assertNotIn('trinket', [row['candidate_key'] for row in build_execution_plan(self.panel)['cases'][0]['candidates']])

    def create_comparison(self, candidates):
        with patch('botend.services.simc_task_service.current_validation_identity', return_value=('a' * 40, '12.0.1')):
            return create_task(user_id=self.user_id, name='装备适用验收', profile_id=self.profile.pk,
                template_id=self.template.pk, apl_id=self.apl.pk, talent_string_id=self.talent.pk,
                backend_id=self.backend.pk, mode='comparison', candidates=candidates)

    def test_regular_comparison_filters_before_initial_runs_and_exposes_reason(self):
        candidates = [{'candidate_key': key, 'candidate_params': value} for key, value in (
            ('base', {'candidate_type': 'base', 'is_base': True}),
            ('wrong', params(123)), ('right', params(456, 'finger1')))]
        task = self.create_comparison(candidates)
        self.assertEqual([row['candidate_key'] for row in task.mode_params['initial_candidates']], ['base', 'right'])
        self.assertEqual(task.mode_params['excluded_candidates'][0]['candidate_key'], 'wrong')
        initialize_task_runs(task)
        self.assertEqual(set(task.simulation_runs.values_list('candidate_key', flat=True)), {'base', 'right'})

    def test_no_eligible_equipment_does_not_create_a_baseline_only_regular_task(self):
        before = SimcTask.objects.count()
        with self.assertRaisesMessage(TaskCreationError, '没有适用于当前职业专精'):
            self.create_comparison([{'candidate_params': params(123)}])
        self.assertEqual(SimcTask.objects.count(), before)

    def test_comparison_api_reports_only_accepted_candidates_and_skipping_reason(self):
        user = User.objects.create_user(id=self.user_id, username='装备验收', is_staff=True, is_superuser=True)
        self.client.force_login(user)
        self.profile.player_config_mode = 'manual_equipment'
        self.profile.player_equipment = 'warrior=验收\nspec=fury\ntrinket1=,id=1\nfinger1=,id=2'
        self.profile.save()
        with patch('botend.services.simc_task_service.current_validation_identity', return_value=('a' * 40, '12.0.1')):
            response = self.client.post('/api/simc-task/comparison/', data=json.dumps({
                'kind': 'gear_candidates', 'name': '装备适用接口验收', 'include_base': False,
                'simc_profile_id': self.profile.pk, 'base_template_id': self.template.pk,
                'selected_apl_id': self.apl.pk, 'talent_string_id': self.talent.pk,
                'backend_id': self.backend.pk,
                'candidates': [{'slot': slot, 'item_id': item_id, 'source': 'manual',
                                'name': str(item_id), 'raw_value': f',id={item_id},ilevel=289'}
                               for item_id, slot in ((123, 'trinket1'), (456, 'finger1'))],
            }), content_type='application/json')
        body = response.json()
        self.assertTrue(body['success'], body)
        self.assertEqual(body['data']['accepted'], 1)
        self.assertEqual(body['data']['excluded_candidates'][0]['item_id'], 123)
        task = SimcTask.objects.get(pk=body['data']['task_id'])
        self.assertEqual(task.mode_params['initial_candidates'][0]['candidate_params']['gear_swap']['item_id'], 456)
