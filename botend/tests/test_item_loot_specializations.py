"""拾取专精真实规则、缺项补采及按分支原位发布回归。"""
from copy import deepcopy
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase

from botend.services.item_loot_specializations import (
    LootSpecializationRules, prepare_specializations, publish_specializations,
)
from botend.services.journal_loot import class_matches


def tables():
    # Wago 12.1.5.70077 的代表性记录：同职业三种职责及智力/力量剑。
    return {'ChrSpecialization': [{'ID': sid, 'ClassID': 2} for sid in (65, 66, 70)], 'GemProperties': [],
        'ItemSpecOverride': [{'ItemID': iid, 'SpecID': sid}
                             for iid, sid in ((280835, 65), (280617, 66), (281215, 70))],
        'ItemSpec': [{'ID': idx, 'ItemType': kind, 'PrimaryStat': primary, 'SecondaryStat': secondary,
                      'SpecializationID': sid, 'MinLevel': 1, 'MaxLevel': 123}
                     for idx, (kind, primary, secondary, sid) in enumerate([
                         (5, 40, 9, 65), (5, 0, 9, 65), (5, 2, 9, 66),
                         (5, 40, 10, 65), (5, 0, 10, 65), (5, 2, 10, 70),
                         (4, 40, 40, 65), (4, 40, 40, 66), (4, 40, 40, 70),
                         (4, 0, 40, 65), (4, 2, 40, 66), (4, 2, 40, 70),
                         (0, 0, 40, 65), (0, 2, 40, 66), (0, 2, 40, 70)])]}


class LootRulesTests(SimpleTestCase):
    def setUp(self):
        self.rules = LootSpecializationRules(tables())

    def test_three_trinkets_follow_explicit_loot_roles(self):
        for iid, sid in ((280835, 65), (280617, 66), (281215, 70)):
            facts = self.rules.resolve({'item_id': iid})
            self.assertEqual(facts['loot_spec_ids'], [sid])
            for selected in (65, 66, 70):
                self.assertEqual(class_matches(facts, 2, selected), selected == sid)

    def test_statless_rules_do_not_make_intellect_sword_a_tank_drop(self):
        sword = {'item_id': 281056, 'class_id': 2, 'subclass_id': 7, 'slot': 13,
                 'class_mask': -1, 'stat_types': [5, 7, 32, 49]}
        self.assertEqual(self.rules.resolve(sword)['loot_spec_ids'], [65])
        sword.update(item_id=280799, subclass_id=8, slot=17, stat_types=[4, 7])
        self.assertEqual(self.rules.resolve(sword)['loot_spec_ids'], [70])

    def test_hybrid_armor_and_statless_ring_are_resolved(self):
        row = {'item_id': 1, 'class_id': 4, 'subclass_id': 4, 'slot': 1,
               'class_mask': -1, 'stat_types': [7, 74]}
        self.assertEqual(self.rules.resolve(row)['loot_spec_ids'], [65, 66, 70])
        row.update(subclass_id=0, slot=11, stat_types=[7, 32, 49])
        self.assertEqual(self.rules.resolve(row)['loot_spec_ids'], [65, 66, 70])

    def test_missing_attributes_are_not_treated_as_statless(self):
        self.assertIsNone(self.rules.resolve({'item_id': 1, 'class_id': 2,
                                             'subclass_id': 7, 'slot': 13}))

    def test_artifact_relic_uses_gem_property_rules(self):
        data = tables()
        data['GemProperties'] = [{'ID': 1, 'Type': 65536}]
        data['ItemSpec'].append({'ID': 99, 'ItemType': 7, 'PrimaryStat': 40,
            'SecondaryStat': 39, 'SpecializationID': 65, 'MinLevel': 1, 'MaxLevel': 123})
        row = {'item_id': 1, 'class_id': 3, 'subclass_id': 11, 'slot': 0,
               'stat_types': [], 'gem_properties': 1}
        self.assertEqual(LootSpecializationRules(data).resolve(row)['loot_spec_ids'], [65])

    def test_class_restriction_and_level_rules_are_respected(self):
        data = tables()
        data['ItemSpec'].append({'ID': 99, 'ItemType': 5, 'PrimaryStat': 0,
            'SecondaryStat': 9, 'SpecializationID': 70, 'MinLevel': 1, 'MaxLevel': 109})
        row = {'item_id': 1, 'class_id': 2, 'subclass_id': 7, 'slot': 13, 'stat_types': [5]}
        self.assertEqual(LootSpecializationRules(data).resolve(row)['loot_spec_ids'], [65])
        row['class_mask'] = 1
        self.assertEqual(self.rules.resolve(row)['loot_spec_ids'], [])

    def test_removed_override_recomputes_rules_instead_of_preserving_stale_restriction(self):
        row = {'item_id': 280835, 'class_id': 4, 'subclass_id': 0, 'slot': 12, 'stat_types': [71]}
        self.assertEqual(self.rules.resolve(row)['loot_spec_ids'], [65])
        data = tables()
        data['ItemSpecOverride'] = []
        self.assertEqual(LootSpecializationRules(data).resolve(row)['loot_spec_ids'], [65, 66, 70])

    @patch('botend.services.item_loot_specializations.current_candidates')
    def test_missing_base_data_is_fetched_by_id(self, candidates):
        candidates.return_value = {281056: {'item_id': 281056}}
        source = Mock(build='12.1.5.70077')
        source.table.side_effect = lambda name, *args, **kwargs: tables()[name]
        client = Mock()
        client.get.return_value.json.side_effect = [
            {'data': [{'ID': 281056, 'ClassID': 2, 'SubclassID': 7}]},
            {'data': [{'ID': 281056, 'InventoryType': 13, 'AllowableClass': -1,
                       'StatModifier_bonusStat_0': 5}]}]
        context = Mock()
        context.__enter__ = Mock(return_value=client)
        context.__exit__ = Mock(return_value=False)
        with patch('botend.services.journal_source.http_session', return_value=context):
            self.assertEqual(prepare_specializations('ptr', source)[281056]['loot_spec_ids'], [65])
        self.assertEqual(client.get.call_count, 2)


class LootPublicationTests(TestCase):
    def test_current_journal_and_item_update_in_place_without_other_branch_changes(self):
        from botend.models import WowItemSnapshot
        from botend.journal_models import JournalRelease, JournalState, JournalInstance, JournalEncounter
        release = JournalRelease.objects.create(build='12.1.0.1', manifest={'ptr_overlays': {'1324': {}}})
        JournalState.objects.create(key='wow-zhCN', active_release=release)
        ptr = JournalInstance.objects.create(release=release, journal_id=1324, name='测试副本', kind='raid')
        retail = JournalInstance.objects.create(release=release, journal_id=1, name='正式服副本', kind='raid')
        payload = {'loot': [{'item_id': 280835, 'name': '测试饰品', 'description': '保留特效描述'}]}
        boss = JournalEncounter.objects.create(instance=ptr, journal_id=1, name='首领', order=1, payload=deepcopy(payload))
        other = JournalEncounter.objects.create(instance=retail, journal_id=2, name='首领', order=1, payload=deepcopy(payload))
        item = WowItemSnapshot.objects.create(item_id=280835, eligible_specs=['Paladin:Retribution'],
            metadata={'branch_display': {'retail': {'loot_spec_ids': [70]}}})
        facts = {280835: {'loot_spec_ids': [65], 'loot_spec_source': 'wago:ItemSpecOverride'}}
        self.assertEqual(publish_specializations('ptr', facts), {'items': 1, 'encounters': 1})
        boss.refresh_from_db(); other.refresh_from_db(); item.refresh_from_db()
        self.assertEqual(boss.payload['loot'][0]['loot_spec_ids'], [65])
        self.assertEqual(boss.payload['loot'][0]['description'], '保留特效描述')
        self.assertEqual(other.payload, payload)
        self.assertEqual(item.eligible_specs, ['Paladin:Retribution'])
        self.assertEqual(item.metadata['branch_display']['retail']['loot_spec_ids'], [70])
        self.assertEqual(item.metadata['branch_display']['ptr']['eligible_specs'], ['Paladin:Holy'])
        self.assertEqual(publish_specializations('ptr', facts), {'items': 0, 'encounters': 0})
        self.assertEqual(JournalRelease.objects.count(), 1)
