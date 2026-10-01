from copy import deepcopy
from types import SimpleNamespace
from django.test import TestCase
from botend.models import WowItemSnapshot, WowItemVariantSnapshot, SeasonMeta
from botend.services.wow_item_effect_activation_store import (
    merge_item_effect_activation, item_activation_facts, freeze_equipment_activation, META_KEY)
from botend.services.simc_benchmark_config import _freeze_case_candidates


class ActivationStoreTests(TestCase):
    def setUp(self):
        self.item = WowItemSnapshot.objects.create(item_id=9901, metadata={'untouched': {'x': 1}})
        season = SeasonMeta.objects.create(season_key='test-s1', season_name='Test', mplus_zone_id=0, raid_zone_id=0)
        self.variant = WowItemVariantSnapshot.objects.create(item=self.item, season=season,
            batch_key='fixture', variant_type='drop_equipment', game_build='12.1.5.60000',
            item_level=321, variant_key='old', metadata={'do_not_overwrite': True})
        self.fact = {'schema_version': 1, 'item_id': 9901, 'game_build': '12.1.5.70077',
            'required_bonus_ids': [12], 'event_spell_ids': [80, 81],
            'effects': [{'spell_id': 80, 'item_effect_id': 10, 'trigger_type': 1, 'bonus_id': 12}],
            'source': {'provider':'wago_db2', 'evidence':[
                {'table':'ItemEffect','rows':[{'ID':10}]},
                {'table':'SpellEffect','rows':[{'ID':11,'SpellID':80,'EffectTriggerSpell':81}]}]}}
        self.params = {'candidate_type':'gear_swap', 'gear_swap': {'slot':'head', 'item_id':9901,
            'item_level':321, 'is_ptr':True, 'raw_value':',id=9901,ilevel=321,bonus_id=7,gem_id=4'}}

    def test_merge_is_idempotent_and_does_not_relabel_old_variant(self):
        self.assertEqual(merge_item_effect_activation([self.fact], is_ptr=True)['changed_item_ids'],[9901])
        self.assertEqual(merge_item_effect_activation([self.fact], is_ptr=True)['changed_item_ids'],[])
        self.item.refresh_from_db(); self.variant.refresh_from_db()
        self.assertEqual(self.item.metadata['untouched'], {'x':1})
        self.assertEqual(self.variant.game_build, '12.1.5.60000')
        self.assertEqual(self.variant.bonus_ids, [])
        self.assertEqual(self.variant.metadata, {'do_not_overwrite':True})

    def test_freeze_preserves_options_and_original_and_is_branch_bound(self):
        merge_item_effect_activation([self.fact], is_ptr=True)
        facts = item_activation_facts([9901]);before=deepcopy(self.params)
        resolved=freeze_equipment_activation(self.params,facts)
        self.assertEqual(self.params,before)
        self.assertEqual(resolved['gear_swap']['raw_value'],',id=9901,ilevel=321,bonus_id=7/12,gem_id=4')
        target=resolved['equipment_effect_expectation']['targets'][0]
        self.assertEqual(target['driver_spell_ids'],[80]);self.assertEqual(target['event_spell_ids'],[80,81])
        live=deepcopy(self.params);live['gear_swap']['is_ptr']=False
        self.assertNotIn('equipment_effect_expectation',freeze_equipment_activation(live,facts))

    def test_candidates_are_resolved_before_control_copy(self):
        merge_item_effect_activation([self.fact], is_ptr=True)
        candidate=SimpleNamespace(key='head',label='Head',candidate_type='gear_swap',params=self.params,
            icon_url='',source_label='',effect='',display_order=0,applicable_specs=[])
        plan=_freeze_case_candidates('warrior_arms',[candidate])
        normal,control=plan[1:]
        self.assertIn('bonus_id=7/12',normal['candidate_params']['gear_swap']['raw_value'])
        from botend.services.simc_task_service import _normalize_candidates
        frozen = _normalize_candidates([normal])[0]['candidate_params']
        self.assertEqual(frozen['equipment_effect_expectation'],normal['candidate_params']['equipment_effect_expectation'])
        self.assertEqual(normal['candidate_params']['equipment_effect_expectation'],
            control['candidate_params']['equipment_effect_expectation'])
