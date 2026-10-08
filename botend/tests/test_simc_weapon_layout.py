"""Weapon legality belongs to frozen planning, not live execution lookups."""
from copy import deepcopy
from django.test import TestCase
from botend.models import WowItemSnapshot, SimcBenchmarkCandidate
from botend.services.simc_benchmark_config import build_execution_plan
from botend.services.simc_task_service import _normalize_candidates
from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
from botend.tests import test_simc_equipment_control as fixtures


class FrozenWeaponLayoutTests(TestCase):
    user_id = 701

    def setUp(self):
        fixtures.EquipmentControlBenchmarkTests.setUp(self)
        self.panel.candidates.all().delete()
        self.weapon = WowItemSnapshot.objects.create(
            item_id=900001, item_class_id=2, item_subclass_id=8, inventory_type=17,
            metadata={'primary_stat_options': ['strength']})
        self.candidate = SimcBenchmarkCandidate.objects.create(
            panel=self.panel, key='weapon', label='Weapon', candidate_type='gear_swap',
            params={'candidate_type': 'gear_swap', 'gear_swap': {
                'slot': 'main_hand', 'item_id': self.weapon.item_id,
                'raw_value': ',id=900001,ilevel=321'}})

    def test_frozen_layout_clears_only_non_titan_grip_twohand_and_keeps_pairs_identical(self):
        for spec_key, inventory, clear in (
            ('deathknight_frost', 17, True), ('warrior_fury', 17, False),
            ('deathknight_frost', 13, False),
        ):
            with self.subTest(spec=spec_key, inventory=inventory):
                spec = self.panel.specs.get()
                spec.spec_key = spec_key
                spec.class_name = 'warrior' if spec_key == 'warrior_fury' else 'deathknight'
                spec.save()
                self.weapon.inventory_type = inventory
                self.weapon.item_subclass_id = 8 if inventory == 17 else 7
                self.weapon.save()
                plan = build_execution_plan(self.panel)
                rows = _normalize_candidates(plan['cases'][0]['candidates'])
                self.assertEqual(len(rows), 3, 'Legal Frost 2h must not be deleted')
                pair = [row['candidate_params'] for row in rows[1:]]
                self.assertEqual('equipment_weapon_layout' in pair[0], clear)
                self.assertEqual(pair[0].get('equipment_weapon_layout'), pair[1].get('equipment_weapon_layout'))
                # Central data changed AFTER freezing: execution must not re-read it.
                WowItemSnapshot.objects.filter(pk=self.weapon.pk).update(inventory_type=13)
                equipment = 'deathknight=x\nspec=frost\nmain_hand=,id=1\noff_hand=,id=2'
                original = deepcopy(pair)
                for params in pair:
                    with self.assertNumQueries(0):
                        request = SimcMonitor.apply_candidate_overrides({'player_equipment': equipment}, params)
                    self.assertIn('main_hand=,id=900001', request['player_equipment'])
                    self.assertIn('off_hand=' + ('' if clear else ',id=2'), request['player_equipment'])
                    if clear:
                        self.assertNotIn('off_hand=,id=2', request['player_equipment'])
                self.assertEqual(pair, original)
                # Old tasks have no frozen evidence, so do not silently reinterpret them.
                legacy = deepcopy(pair[0])
                legacy.pop('equipment_weapon_layout', None)
                from botend.services.simc_benchmark_execution import _candidate_input_identity
                current_identity = _candidate_input_identity({'candidate_params': pair[0]})
                legacy_identity = _candidate_input_identity({'candidate_params': legacy})
                self.assertEqual(current_identity == legacy_identity, not clear)
                self.assertIn('off_hand=,id=2', SimcMonitor.apply_candidate_overrides(
                    {'player_equipment': equipment}, legacy)['player_equipment'])

    def test_paired_weapons_freeze_and_fill_only_missing_base_offhand(self):
        self.weapon.inventory_type = 13
        self.weapon.item_subclass_id = 7
        self.weapon.metadata = {'primary_stat_options': ['agility']}
        self.weapon.save()
        offhand = WowItemSnapshot.objects.create(
            item_id=900002, item_class_id=2, item_subclass_id=7, inventory_type=13,
            metadata={'primary_stat_options': ['agility']})
        self.candidate.params = {'candidate_type': 'gear_swap', 'gear_swaps': [
            {'slot': 'main_hand', 'item_id': 900001, 'raw_value': ',id=900001,ilevel=321'},
            {'slot': 'off_hand', 'item_id': 900002, 'raw_value': ',id=900002,ilevel=321'},
        ]}
        self.candidate.save()
        for spec_key in ('hunter_survival', 'monk_windwalker'):
            with self.subTest(spec=spec_key):
                spec = self.panel.specs.get()
                spec.spec_key = spec_key
                spec.class_name = spec_key.split('_')[0]
                spec.save()
                plan = build_execution_plan(self.panel)
                pair = [row['candidate_params'] for row in
                        _normalize_candidates(plan['cases'][0]['candidates'])[1:]]
                self.assertEqual(len(pair), 2)
                self.assertIn('equipment_weapon_layout', pair[0])
                self.assertEqual(pair[0]['equipment_weapon_layout'], pair[1]['equipment_weapon_layout'])
                # A candidate-section offhand is not a base player slot.
                equipment = 'main_hand=,id=1\n### alternatives\noff_hand=,id=3'
                for params in pair:
                    before = deepcopy(params)
                    with self.assertNumQueries(0):
                        request = SimcMonitor.apply_candidate_overrides(
                            {'player_equipment': equipment}, params)
                    base, candidates = request['player_equipment'].split('###', 1)
                    self.assertIn('main_hand=,id=900001,ilevel=321', base)
                    self.assertIn('off_hand=,id=900002,ilevel=321', base)
                    self.assertIn('off_hand=,id=3', candidates)
                    self.assertEqual(params, before)
                    # Do not reinterpret old frozen inputs with no layout proof.
                    legacy = deepcopy(params)
                    legacy.pop('equipment_weapon_layout')
                    with self.assertRaisesRegex(ValueError, 'off_hand'):
                        SimcMonitor.apply_candidate_overrides({'player_equipment': equipment}, legacy)
                    with self.assertRaisesRegex(ValueError, 'main_hand'):
                        SimcMonitor.apply_candidate_overrides({'player_equipment': 'head=,id=1'}, params)
                    tampered = deepcopy(params)
                    tampered['equipment_weapon_layout']['weapons'][0]['item_id'] = 999999
                    with self.assertRaisesRegex(ValueError, '身份不一致'):
                        SimcMonitor.apply_candidate_overrides({'player_equipment': equipment}, tampered)

    def test_layout_repair_keeps_legacy_results_visible_without_reusing_old_inputs(self):
        from botend.services.simc_benchmark_execution import serialize_incremental_panel_results, _incremental_coordinates
        self.candidate.key = 'ring'
        self.candidate.save(update_fields=['key'])
        spec = self.panel.specs.get()
        spec.spec_key = 'warrior_arms'
        spec.class_name = 'warrior'
        spec.save()
        self._create = fixtures.EquipmentControlBenchmarkTests._create.__get__(self)
        self._run = fixtures.EquipmentControlBenchmarkTests._run.__get__(self)
        execution = fixtures.EquipmentControlBenchmarkTests.finish(self)
        task = execution.cases.get().task
        old = deepcopy(task.mode_params)
        for rows in (old['initial_candidates'], old['request_manifest']['candidates']):
            for row in rows:
                row['candidate_params'].pop('equipment_weapon_layout', None)
        task.mode_params = old
        task.save(update_fields=['mode_params'])
        rows = serialize_incremental_panel_results(self.panel)['coordinates'][0]['candidates']
        self.assertTrue(any(row['key'] == 'ring' for row in rows))
        missing = _incremental_coordinates(self.panel, build_execution_plan(self.panel))
        self.assertTrue(any(row['candidate_key'] == 'ring' for case in missing for row in case['candidates']))

    def test_twohand_plus_explicit_offhand_is_rejected_except_titan_grip(self):
        offhand = WowItemSnapshot.objects.create(item_id=900002, item_class_id=2,
            item_subclass_id=7, inventory_type=13, metadata={'primary_stat_options': ['strength']})
        params = deepcopy(self.candidate.params)
        params['gear_swaps'] = [params.pop('gear_swap'), {
            'slot': 'off_hand', 'item_id': offhand.item_id, 'raw_value': ',id=900002,ilevel=321'}]
        self.candidate.params = params
        self.candidate.save()
        from botend.services.simc_equipment_eligibility import EquipmentEligibility
        eligibility = EquipmentEligibility([params])
        self.assertEqual(eligibility.reason(params, 'deathknight_frost')['code'], 'invalid_weapon_layout')
        # Fury permits 2h in both slots, not a forced offhand deletion.
        offhand.inventory_type = 17
        offhand.item_subclass_id = 8
        offhand.save()
        eligibility = EquipmentEligibility([params])
        self.assertIsNone(eligibility.reason(params, 'warrior_fury'))
        params['equipment_weapon_layout'] = eligibility.weapon_layout(params, 'warrior_fury')
        with self.assertNumQueries(0):
            request = SimcMonitor.apply_candidate_overrides(
                {'player_equipment': 'warrior=x\nmain_hand=,id=1\noff_hand=,id=2'}, params)
        self.assertIn('main_hand=,id=900001', request['player_equipment'])
        self.assertIn('off_hand=,id=900002', request['player_equipment'])
