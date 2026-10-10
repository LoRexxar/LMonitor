import json

from django.test import TestCase

from botend.models import GearBuilderOwnedItem
from botend.services.gear_builder import EQUIPMENT_SLOTS, GearBuilderError
from botend.tests.test_gear_assistant_priorities import GearAssistantDataMixin


class GearAssistantExactTwoTests(GearAssistantDataMixin, TestCase):
    def sourced(self, slot, source, **kwargs):
        row = self.variant(slot, **kwargs)
        row.source_json = [{'type': source}]
        row.save(update_fields=['source_json'])
        return row

    def crafts(self, slots=('head', 'chest', 'legs', 'wrists', 'back')):
        material = self.reagent()
        material.stats_json = {}  # proc-only embellishments must be usable
        material.save(update_fields=['stats_json'])
        return {slot: self.sourced(slot, 'crafted', crafted=True) for slot in slots}, material

    def assert_exact(self, plan):
        self.assertEqual(set(plan['equipment']), {slot for slot, _ in EQUIPMENT_SLOTS})
        self.assertEqual(sum(bool(row['variant']['is_intrinsic_embellishment']) + bool(row['embellishment'])
                             for row in plan['equipment'].values()), 2)
        self.assertEqual(plan['embellishment_count'], 2)
        self.assertLessEqual(plan['delve_myth_count'], 2)
        self.assertEqual(plan['delve_myth_count'], sum(row['delve_myth_cost'] for row in plan['equipment'].values()))
        for row in plan['equipment'].values():
            self.assertIn(row['acquisition_source_type'], {source['type'] for source in row['variant']['sources']})
            self.assertEqual(row['delve_myth_cost'], int(row['acquisition_source_type'] == 'delve' and row['variant']['track'] == 'myth'))

    def test_proc_only_materials_drive_selection_and_prefer_wrists_back(self):
        crafts, material = self.crafts()
        for plan in self.optimize()['plans']:
            self.assert_exact(plan)
            self.assertEqual({slot for slot, row in plan['equipment'].items() if row['embellishment']},
                             {'wrists', 'back'})
            self.assertEqual(plan['effect_count'], 2)
            for slot in ('wrists', 'back'):
                self.assertEqual(plan['equipment'][slot]['variant']['id'], crafts[slot].id)
                self.assertEqual(plan['equipment'][slot]['embellishment']['variant']['id'], material.id)

    def test_missing_second_eligible_embellishment_is_explicit_failure(self):
        self.crafts(('wrists',))
        with self.assertRaisesRegex(GearBuilderError, '美化.*2'):
            self.optimize()

    def test_fixed_plain_drops_are_not_replaced_to_satisfy_two(self):
        self.crafts()
        fixed = {slot: {'variant': {'id': row.id}} for slot, row in self.base.items()}
        with self.assertRaisesRegex(GearBuilderError, '美化.*2'):
            self.optimize(equipment=fixed)

    def test_fixed_crafts_can_receive_missing_attachment_without_replacement(self):
        crafts, _material = self.crafts(('head', 'chest'))
        fixed = {slot: {'variant': {'id': row.id}} for slot, row in self.base.items()}
        fixed.update({slot: {'variant': {'id': row.id}} for slot, row in crafts.items()})
        for plan in self.optimize(equipment=fixed)['plans']:
            self.assert_exact(plan)
            for slot in fixed:
                self.assertEqual(plan['equipment'][slot]['variant']['id'], fixed[slot]['variant']['id'])

    def test_source_preference_is_exclusive_validated_and_ignored_in_nonraid(self):
        self.crafts(('wrists', 'back'))
        raid = self.sourced('head', 'raid', stats={'crit': 800})
        self.client.force_login(self.user)
        for invalid in (['raid', 'mythic_plus'], 'both', '', None, True, {}):
            response = self.client.post('/portal/api/gear-assistant/optimize/', data=json.dumps({
                'target': self.target, 'source_preference': invalid,
            }), content_type='application/json')
            self.assertEqual(response.status_code, 400, response.content)
            self.assertIn('来源偏好', response.json()['error'])
        for pref in ('none', 'raid', 'mythic_plus'):
            result = self.optimize(source_preference=pref)
            self.assertEqual(result['source_preference'], pref)
            for plan in result['plans']:
                self.assert_exact(plan)
                expected = 'none' if plan['key'] == 'dungeon' else pref
                self.assertEqual(plan['source_preference'], expected)
                self.assertEqual(plan['source_preference_label'], '不应用来源偏好' if plan['key'] == 'dungeon'
                                 else {'none': '不偏好来源', 'raid': '优先团本', 'mythic_plus': '优先大秘境'}[pref])
                selected = plan['equipment']['head']['variant']['id']
                self.assertEqual(selected, raid.id if expected == 'raid' else self.base['head'].id)
        self.assertEqual(self.optimize()['source_preference'], 'none')

    def test_small_slot_priority_beats_green_match_and_attachment_stats_count_once(self):
        crafts, reagent = self.crafts()
        for slot in ('wrists', 'back'):
            crafts[slot].stats_json = {'crit': 500}
            crafts[slot].save(update_fields=['stats_json'])
        reagent.stats_json = {'crit': 10}
        reagent.save(update_fields=['stats_json'])
        for plan in self.optimize()['plans']:
            self.assert_exact(plan)
            self.assertEqual({slot for slot, row in plan['equipment'].items() if row['embellishment']}, {'wrists', 'back'})
            self.assertEqual(plan['stats']['crit'], 1020)

    def test_nonraid_keeps_lower_nonraid_variant_of_raid_item(self):
        self.crafts(('wrists', 'back'))
        self.sourced('head', 'raid', item=self.base['head'].item, level=800)
        plans = {row['key']: row for row in self.optimize()['plans']}
        self.assertEqual(plans['all']['equipment']['head']['itemLevel'], 800)
        self.assertEqual(plans['dungeon']['equipment']['head']['variant']['id'], self.base['head'].id)

    def test_nonraid_mixed_raid_delve_route_consumes_cap(self):
        self.crafts(('wrists', 'back'))
        for slot in ('head', 'chest', 'legs'):
            self.base[slot].delete()
            row = self.delve(slot)
            row.source_json.append({'type': 'raid'})
            row.save(update_fields=['source_json'])
        with self.assertRaisesRegex(GearBuilderError, '不含团本.*地下堡神话.*2'):
            self.optimize()

    def test_unobtainable_raid_or_generic_vault_does_not_waive_delve_cap(self):
        self.crafts(('wrists', 'back'))
        for slot in ('head', 'chest', 'legs'):
            row = self.delve(slot, effects=[{'description_zh': '装备：攻击触发火焰。'}])
            row.source_json += [{'type': 'raid', 'instance_id': 999, 'encounter_id': 888},
                                {'type': 'great_vault'}]
            row.save(update_fields=['source_json'])
        for plan in self.optimize(allow_mythic_last_two=False)['plans']:
            self.assert_exact(plan)
            self.assertEqual(plan['delve_myth_count'], 2)

    def test_benchmark_provenance_is_not_an_acquisition_route(self):
        self.crafts(('wrists', 'back'))
        benchmark = self.sourced('head', 'benchmark', level=800,
                                effects=[{'description_zh': '装备：攻击触发火焰。'}])
        for plan in self.optimize()['plans']:
            self.assertEqual(plan['equipment']['head']['variant']['id'], self.base['head'].id)
        # A real acquisition route is sufficient; benchmark provenance is ignored.
        benchmark.source_json.append({'type': 'mythic_plus'})
        benchmark.save(update_fields=['source_json'])
        for plan in self.optimize()['plans']:
            self.assertEqual(plan['equipment']['head']['variant']['id'], benchmark.id)
            self.assertEqual(plan['equipment']['head']['acquisition_source_type'], 'mythic_plus')
        # User-owned/locked physical items do not need a new acquisition route.
        benchmark.source_json = [{'type': 'benchmark'}]
        benchmark.save(update_fields=['source_json'])
        GearBuilderOwnedItem.objects.create(user=self.user, item_id=benchmark.item.item_id,
                                           variant=benchmark, slot_key='head')
        plans = {p['key']: p for p in self.optimize()['plans']}
        self.assertEqual(plans['prefer_owned']['equipment']['head']['variant']['id'], benchmark.id)
        self.assertEqual(plans['all']['equipment']['head']['variant']['id'], self.base['head'].id)
        for plan in self.optimize(equipment={'head': {'variant': {'id': benchmark.id}}})['plans']:
            self.assertEqual(plan['equipment']['head']['variant']['id'], benchmark.id)

    def test_generic_sources_cannot_launder_raid_into_nonraid(self):
        self.crafts(('wrists', 'back'))
        for generic in ('great_vault', 'bonus_roll', 'unknown', 'benchmark'):
            with self.subTest(source=generic):
                high = self.sourced('head', 'raid', item=self.base['head'].item, level=800,
                                    effects=[{'description_zh': '装备：攻击触发火焰。'}])
                high.source_json.append({'type': generic})
                high.save(update_fields=['source_json'])
                plans = {row['key']: row for row in self.optimize()['plans']}
                high_id = high.id
                high.delete()
                self.assertEqual(plans['all']['equipment']['head']['variant']['id'], high_id)
                self.assertEqual(plans['dungeon']['equipment']['head']['variant']['id'], self.base['head'].id)

    def test_selected_delve_trinket_route_rechecks_legality_before_dedup(self):
        self.crafts(('wrists', 'back'))
        myth = self.delve('trinket1', level=800, effects=[{'description_zh': '装备：攻击触发火焰。'}])
        myth.item.inventory_type = 12
        myth.item.save(update_fields=['inventory_type'])
        myth.source_json.append({'type': 'great_vault'})
        myth.save(update_fields=['source_json'])
        hero = self.delve('trinket1', 'hero', item=myth.item, level=720,
                          effects=[{'description_zh': '装备：攻击触发火焰。'}])
        for allow in (False, True):
            with self.subTest(allow_mythic_last_two=allow):
                for plan in self.optimize(allow_mythic_last_two=allow)['plans']:
                    self.assertEqual(plan['equipment']['trinket1']['variant']['id'], hero.id)
                    self.assert_exact(plan)

    def test_mixed_raid_delve_trinket_keeps_raid_but_nonraid_uses_hero(self):
        self.crafts(('wrists', 'back'))
        myth = self.delve('trinket1', level=800, effects=[{'description_zh': '装备：攻击触发火焰。'}])
        myth.item.inventory_type = 12
        myth.item.save(update_fields=['inventory_type'])
        myth.source_json.append({'type': 'raid'})
        myth.save(update_fields=['source_json'])
        hero = self.delve('trinket1', 'hero', item=myth.item, level=720,
                          effects=[{'description_zh': '装备：攻击触发火焰。'}])
        plans = {row['key']: row for row in self.optimize()['plans']}
        for mode in ('all', 'prefer_owned'):
            self.assertEqual(plans[mode]['equipment']['trinket1']['variant']['id'], myth.id)
            self.assertEqual(plans[mode]['equipment']['trinket1']['acquisition_source_type'], 'raid')
        self.assertEqual(plans['dungeon']['equipment']['trinket1']['variant']['id'], hero.id)
        # M-last-two filtering must still remove the unverified raid route.
        for plan in self.optimize(allow_mythic_last_two=False)['plans']:
            self.assertEqual(plan['equipment']['trinket1']['variant']['id'], hero.id)

    def test_selected_delve_route_requires_myth_unlock_evidence(self):
        self.crafts(('wrists', 'back'))
        myth = self.delve('head', level=800, effects=[{'description_zh': '装备：攻击触发火焰。'}])
        myth.metadata = {}
        myth.source_json.append({'type': 'great_vault'})
        myth.save(update_fields=['metadata', 'source_json'])
        hero = self.delve('head', 'hero', item=myth.item, level=720,
                          effects=[{'description_zh': '装备：攻击触发火焰。'}])
        for plan in self.optimize(allow_mythic_last_two=False)['plans']:
            self.assertEqual(plan['equipment']['head']['variant']['id'], hero.id)

    def test_champion_cannot_supply_third_mandatory_delve_recommendation(self):
        self.crafts(('wrists', 'back'))
        for slot in ('head', 'chest', 'legs'):
            self.base[slot].delete()
            myth = self.delve(slot, level=740)
            self.delve(slot, 'champion', item=myth.item, level=700)
        with self.assertRaisesRegex(GearBuilderError, '地下堡神话.*2'):
            self.optimize()

    def test_illegal_delve_track_does_not_hide_same_item_hero(self):
        self.crafts(('wrists', 'back'))
        self.base['head'].delete()
        champion = self.delve('head', 'champion', level=800)
        hero = self.delve('head', 'hero', item=champion.item, level=720)
        for plan in self.optimize()['plans']:
            self.assertEqual(plan['equipment']['head']['variant']['id'], hero.id)
            self.assert_exact(plan)

    def test_owned_and_locked_champion_delve_is_not_silently_replaced(self):
        self.crafts(('wrists', 'back'))
        champion = self.delve('head', 'champion')
        for plan in self.optimize(equipment={'head': {'variant': {'id': champion.id}}})['plans']:
            self.assertEqual(plan['equipment']['head']['variant']['id'], champion.id)
        GearBuilderOwnedItem.objects.create(user=self.user, item_id=champion.item.item_id,
                                           variant=champion, slot_key='head')
        plans = {row['key']: row for row in self.optimize()['plans']}
        self.assertEqual(plans['prefer_owned']['equipment']['head']['variant']['id'], champion.id)
        self.assertEqual(plans['all']['equipment']['head']['variant']['id'], self.base['head'].id)

    def test_joint_capacity_survives_over_beam_width_and_later_mandatory_items(self):
        from botend.services.gear_assistant import _beam_plan
        heads = [self.delve('head', intrinsic=True, effects=[{'description_zh': '装备：触发火焰。'}])
                 for _ in range(601)]
        for slot in ('finger1', 'finger2'):
            self.base[slot].is_intrinsic_embellishment = True
            self.base[slot].save(update_fields=['is_intrinsic_embellishment'])
        for slot in ('legs', 'feet'):
            self.base[slot].delete()
            self.base[slot] = self.delve(slot)
        plan = _beam_plan('all', [*heads, *self.base.values()], {}, {}, 'Warrior', 'Fury', self.target, self.conversion)
        self.assertEqual(plan['equipment']['head']['variant'].id, self.base['head'].id)
        self.assertEqual(plan['embellishment_count'], 2)
        self.assertEqual(plan['delve_myth_count'], 2)

    def test_source_preference_before_owned_still_preserves_owned_quantity(self):
        self.crafts(('wrists', 'back'))
        raid = self.sourced('head', 'raid')
        GearBuilderOwnedItem.objects.create(user=self.user, item_id=self.base['head'].item.item_id,
                                           variant=self.base['head'], slot_key='head')
        plans = {row['key']: row for row in self.optimize(source_preference='raid')['plans']}
        self.assertEqual(plans['prefer_owned']['equipment']['head']['variant']['id'], raid.id)
        ring = self.base['finger1']
        ring.compatible_slots = ['finger1', 'finger2']
        ring.save(update_fields=['compatible_slots'])
        GearBuilderOwnedItem.objects.create(user=self.user, item_id=ring.item.item_id,
                                           variant=ring, slot_key='finger1', quantity=1,
                                           fingerprint=f'source-preference-ring-{ring.id}')
        plans = {row['key']: row for row in self.optimize()['plans']}
        self.assertEqual(plans['prefer_owned']['owned_count'], 2)  # head + one of the ring slots

    def test_effect_priority_precedes_source_preference(self):
        self.crafts(('wrists', 'back'))
        self.sourced('head', 'raid')
        effect = self.sourced('head', 'mythic_plus', stats={'crit': 900},
                              effects=[{'description_zh': '装备：攻击触发火焰。'}])
        for plan in self.optimize(source_preference='raid')['plans']:
            self.assertEqual(plan['equipment']['head']['variant']['id'], effect.id)

    def test_nonraid_includes_plain_crafted_delve_and_other_sources_but_not_raid(self):
        self.crafts(('wrists', 'back'))
        for slot, source in (('head', 'delve'), ('chest', 'world'), ('legs', 'crafted')):
            self.base[slot].delete()
            if source == 'delve':
                self.delve(slot, 'hero')
            else:
                self.sourced(slot, source, crafted=source == 'crafted')
        raid = self.sourced('neck', 'raid', effects=[{'description_zh': '装备：触发火焰。'}])
        plans = {row['key']: row for row in self.optimize(source_preference='raid')['plans']}
        self.assertEqual(plans['all']['equipment']['neck']['variant']['id'], raid.id)
        self.assertEqual(plans['dungeon']['name'], '不含团本装备')
        self.assertEqual(plans['dungeon']['equipment']['neck']['variant']['id'], self.base['neck'].id)
        self.assert_exact(plans['dungeon'])

    def test_nonraid_missing_list_names_the_selected_legal_source(self):
        self.crafts(('wrists', 'back'))
        head = self.base['head']
        head.source_json = [{'type': 'raid', 'instance_zh': '测试团本'},
                            {'type': 'mythic_plus', 'instance_zh': '测试地下城'}]
        head.save(update_fields=['source_json'])
        plan = next(row for row in self.optimize()['plans'] if row['key'] == 'dungeon')
        missing = next(row for row in plan['missing_items'] if row['slot'] == 'head')
        self.assertIn('测试地下城', missing['source'])
        self.assertNotIn('测试团本', missing['source'])

    def test_unknown_recipe_cannot_supply_mandatory_second_attachment(self):
        crafts, _material = self.crafts(('wrists', 'back'))
        crafts['back'].item.metadata = {}
        crafts['back'].item.save(update_fields=['metadata'])
        with self.assertRaisesRegex(GearBuilderError, '美化.*2'):
            self.optimize()

    def test_intrinsic_plus_locked_attachment_count_exactly_two(self):
        crafts, reagent = self.crafts(('head',))
        intrinsic = self.sourced('wrists', 'crafted', crafted=True, intrinsic=True)
        fixed = {'head': {'variant': {'id': crafts['head'].id},
                          'embellishment': {'variant': {'id': reagent.id}}},
                 'wrists': {'variant': {'id': intrinsic.id}}}
        for plan in self.optimize(equipment=fixed)['plans']:
            self.assert_exact(plan)
            self.assertIsNone(plan['equipment']['wrists']['embellishment'])

    def delve(self, slot, track='myth', **kwargs):
        row = self.sourced(slot, 'delve', **kwargs)
        row.upgrade_track = track
        row.metadata = {'delve_myth': True}
        row.save(update_fields=['upgrade_track', 'metadata'])
        return row

    def test_myth_delve_cap_preserves_same_item_hero_fallback(self):
        self.crafts(('wrists', 'back'))
        for slot in ('head', 'chest', 'legs'):
            self.base[slot].delete()
            myth = self.delve(slot, level=740)
            self.delve(slot, 'hero', item=myth.item, level=720)
        for plan in self.optimize()['plans']:
            rows = [plan['equipment'][slot]['variant'] for slot in ('head', 'chest', 'legs')]
            self.assertEqual(sorted(row['track'] for row in rows), ['hero', 'myth', 'myth'])
            self.assert_exact(plan)

    def test_locked_myth_delve_cap_counts_slots_and_rejects_three(self):
        self.crafts(('wrists', 'back'))
        fixed = {}
        for slot in ('head', 'chest', 'legs'):
            row = self.delve(slot)
            fixed[slot] = {'variant': {'id': row.id}}
        with self.assertRaisesRegex(GearBuilderError, '地下堡.*2'):
            self.optimize(equipment=fixed)
        del fixed['legs']
        for plan in self.optimize(equipment=fixed)['plans']:
            self.assertEqual(plan['equipment']['legs']['variant']['id'], self.base['legs'].id)

    def test_hero_delve_is_deprioritized_but_alternative_mplus_source_avoids_cap(self):
        self.crafts(('wrists', 'back'))
        self.delve('head', 'hero', level=760)
        for plan in self.optimize()['plans']:
            self.assertEqual(plan['equipment']['head']['variant']['id'], self.base['head'].id)
        fixed = {}
        for slot in ('head', 'chest', 'legs'):
            row = self.delve(slot)
            row.source_json.append({'type': 'mythic_plus'})
            row.save(update_fields=['source_json'])
            fixed[slot] = {'variant': {'id': row.id}}
        for plan in self.optimize(equipment=fixed)['plans']:
            self.assert_exact(plan)

    def test_distinct_myth_delve_rings_consume_two_slots(self):
        self.crafts(('wrists', 'back'))
        ring = self.delve('finger1')
        ring.compatible_slots = ['finger1', 'finger2']
        ring.save(update_fields=['compatible_slots'])
        head = self.delve('head')
        other_ring = self.delve('finger2')
        fixed = {'finger1': {'variant': {'id': ring.id}}, 'finger2': {'variant': {'id': other_ring.id}}}
        fixed['head'] = {'variant': {'id': head.id}}
        with self.assertRaisesRegex(GearBuilderError, '地下堡.*2'):
            self.optimize(equipment=fixed)
