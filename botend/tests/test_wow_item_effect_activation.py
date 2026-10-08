"""Focused DB2 contracts; synthetic cases are not live collection evidence."""
import copy
import html
import json
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

from botend.services.wow_item_effect_activation import (
    ActivationSourceError, ItemEffectActivationCollector,
)

BUILD = '12.1.5.70077'
ROOT = 'https://wago.tools/db2'
GUARDS = (
    'ItemContext', 'ChildItemLevelSelectorID', 'ChildItemBonusListGroupID',
    'IblGroupPointsModSetID', 'MinMythicPlusLevel', 'MaxMythicPlusLevel',
    'ItemCreationContextGroupID', 'Flags',
)


def node(row_id, tree, *, bonus=0, child=0, **guards):
    return dict(ID=row_id, ParentItemBonusTreeID=tree,
                ChildItemBonusTreeID=child, ChildItemBonusListID=bonus,
                **dict.fromkeys(GUARDS, 0), **guards) if not guards else {
                    **node(row_id, tree, bonus=bonus, child=child), **guards}


def effect(row_id=40, spell=50, **extra):
    return {'ID': row_id, 'SpellID': spell, 'TriggerType': 1,
            'PlayerConditionID': 0, 'ChrSpecializationID': 0, **extra}


class FakeSource:
    def __init__(self, records=None, mutate=None, per_page=25):
        self.records = records or {}
        self.mutate = mutate
        self.per_page = per_page
        self.calls = []

    def _get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        parts = urlsplit(url)
        table = parts.path.split('/')[-1]
        query = parse_qs(parts.query)
        field = next(k[7:-1] for k in query if k.startswith('filter['))
        value = query[f'filter[{field}]'][0]
        assert value.startswith('exact:') and 'in:' not in url
        page = int(query.get('page', ['1'])[0])
        all_rows = copy.deepcopy(self.records.get((table, field, int(value[6:])), []))
        total = len(all_rows)
        last = max(1, (total + self.per_page - 1) // self.per_page)
        rows = all_rows[(page - 1) * self.per_page:page * self.per_page]
        def page_url(number):
            return f'{ROOT}/{table}?' + urlencode({
                'build': BUILD, 'locale': 'enUS', f'filter[{field}]': value, 'page': number})
        props = {
            'currentTable': table, 'currentVersion': BUILD,
            'filters': {'build': BUILD, 'locale': 'enUS', 'filter': {field: value}},
            'data': {'current_page': page, 'data': rows, 'total': total,
                     'last_page': last, 'per_page': self.per_page,
                     'from': (page - 1) * self.per_page + 1 if rows else None,
                     'to': (page - 1) * self.per_page + len(rows) if rows else None,
                     'path': f'{ROOT}/{table}',
                     'first_page_url': page_url(1), 'last_page_url': page_url(last),
                     'next_page_url': page_url(page + 1) if page < last else None,
                     'prev_page_url': page_url(page - 1) if page > 1 else None},
        }
        if self.mutate:
            self.mutate(props)
        return SimpleNamespace(url=url, text='<div data-page="' + html.escape(
            json.dumps({'props': props}), quote=True) + '"></div>')


def fixture():
    return {
        ('ItemXBonusTree', 'ItemID', 10): [{'ID': 1, 'ItemID': 10, 'ItemBonusTreeID': 20}],
        ('ItemBonusTreeNode', 'ParentItemBonusTreeID', 20): [node(2, 20, bonus=30)],
        ('ItemBonus', 'ParentItemBonusListID', 30): [
            {'ID': 3, 'ParentItemBonusListID': 30, 'Type': 23, 'Value_0': 40,
             'Value_1': 0, 'Value_2': 0, 'Value_3': 0}],
        ('ItemEffect', 'ID', 40): [effect()],
    }


class ItemEffectActivationTests(unittest.TestCase):
    def collect(self, source=None, **limits):
        return ItemEffectActivationCollector(source=source or FakeSource(fixture()), **limits).collect(
            item_id=10, game_build=BUILD)

    def test_unconditional_bonus_and_direct_effect_are_fact_schema(self):
        records = fixture()
        records[('ItemXItemEffect', 'ItemID', 10)] = [
            {'ID': 4, 'ItemID': 10, 'ItemEffectID': 41, 'OrderIndex': 1}]
        records[('ItemEffect', 'ID', 41)] = [effect(41, 51, TriggerType=0)]
        source = FakeSource(records)
        result = self.collect(source)
        self.assertEqual(result['schema_version'], 1)
        self.assertEqual(result['item_id'], 10)
        self.assertEqual(result['game_build'], BUILD)
        self.assertEqual(result['required_bonus_ids'], [30])
        self.assertEqual(result['effects'], [
            {'item_effect_id': 40, 'spell_id': 50, 'trigger_type': 1, 'bonus_id': 30},
            {'item_effect_id': 41, 'spell_id': 51, 'trigger_type': 0}])
        self.assertEqual(result['unresolved_paths'], [])
        self.assertEqual(len(result['source']['evidence']), len(source.calls))
        self.assertTrue(any(e['rows'] == [effect()] for e in result['source']['evidence']))
        self.assertTrue(all(not kwargs for _, kwargs in source.calls))

    def test_conditional_selector_group_flags_and_unknown_nodes_do_not_expand(self):
        for field in (*GUARDS, 'NewConditionID'):
            with self.subTest(field=field):
                records = fixture()
                records[('ItemBonusTreeNode', 'ParentItemBonusTreeID', 20)] = [
                    node(2, 20, bonus=30, child=21, **{field: 1})]
                source = FakeSource(records)
                result = self.collect(source)
                self.assertEqual(result['required_bonus_ids'], [])
                self.assertEqual(result['effects'], [])
                self.assertEqual(result['unresolved_paths'][0]['row'][field], 1)
                self.assertFalse(any('/ItemBonus?' in url for url, _ in source.calls))
                self.assertFalse(any('exact%3A21' in url for url, _ in source.calls))

    def test_missing_guard_and_effect_condition_fail_closed(self):
        records = fixture()
        del records[('ItemBonusTreeNode', 'ParentItemBonusTreeID', 20)][0]['Flags']
        self.assertEqual(self.collect(FakeSource(records))['effects'], [])
        for field in ('PlayerConditionID', 'ChrSpecializationID'):
            records = fixture()
            records[('ItemEffect', 'ID', 40)][0][field] = 1
            result = self.collect(FakeSource(records))
            self.assertEqual(result['required_bonus_ids'], [])
            self.assertEqual(result['effects'], [])
            self.assertTrue(result['unresolved_paths'])

    def test_multi_level_tree_cycles_and_duplicate_roots_are_bounded(self):
        records = fixture()
        records[('ItemXBonusTree', 'ItemID', 10)].append(
            {'ID': 5, 'ItemID': 10, 'ItemBonusTreeID': 20})
        records[('ItemBonusTreeNode', 'ParentItemBonusTreeID', 20)] = [node(2, 20, child=21)]
        records[('ItemBonusTreeNode', 'ParentItemBonusTreeID', 21)] = [
            node(6, 21, bonus=30, child=20)]
        source = FakeSource(records)
        result = self.collect(source)
        self.assertEqual(result['required_bonus_ids'], [30])
        self.assertEqual(result['unresolved_paths'][0]['reason'], 'cycle')
        self.assertEqual(sum('/ItemBonusTreeNode?' in url for url, _ in source.calls), 2)

    def test_limits_do_not_accept_partial_query_results(self):
        for limits, reason in [({'max_requests': 1}, 'request_limit'),
                               ({'max_rows': 1}, 'row_limit')]:
            with self.subTest(limits=limits):
                source = FakeSource(fixture())
                result = self.collect(source, **limits)
                self.assertEqual(result['effects'], [])
                self.assertTrue(any(p['reason'] == reason for p in result['unresolved_paths']))
                self.assertLessEqual(len(source.calls), limits.get('max_requests', 64))
                self.assertLessEqual(result['source']['row_count'], limits.get('max_rows', 1000))
        records = fixture()
        records[('ItemBonusTreeNode', 'ParentItemBonusTreeID', 20)] = [node(2, 20, child=21)]
        records[('ItemBonusTreeNode', 'ParentItemBonusTreeID', 21)] = [node(6, 21, bonus=30)]
        result = self.collect(FakeSource(records), max_depth=0)
        self.assertEqual(result['effects'], [])
        self.assertTrue(any(p['reason'] == 'depth_limit' for p in result['unresolved_paths']))
        records = fixture()
        records[('ItemBonusTreeNode', 'ParentItemBonusTreeID', 20)].append(node(6, 20, bonus=31))
        result = self.collect(FakeSource(records, per_page=1), max_pages=1)
        self.assertEqual(result['effects'], [])
        self.assertTrue(any(p['reason'] == 'page_limit' for p in result['unresolved_paths']))

    def test_pagination_is_complete_and_deduplicates_shared_effect_query(self):
        records = fixture()
        records[('ItemBonusTreeNode', 'ParentItemBonusTreeID', 20)].append(node(6, 20, bonus=30))
        source = FakeSource(records, per_page=1)
        result = self.collect(source)
        self.assertEqual(result['required_bonus_ids'], [30])
        self.assertEqual(len(result['effects']), 1)
        self.assertTrue(any('page=2' in url for url, _ in source.calls))
        self.assertEqual(sum('/ItemEffect?' in url for url, _ in source.calls), 1)

    def test_strict_source_identity_and_pagination_reject_forged_responses(self):
        mutations = {
            'build': lambda p: p.update(currentVersion='12.1.5.69594'),
            'table': lambda p: p.update(currentTable='ItemSparse'),
            'locale': lambda p: p['filters'].update(locale='zhCN'),
            'filter': lambda p: p['filters']['filter'].update(ItemID='in:10'),
            'extra_filter': lambda p: p['filters']['filter'].update(ID='exact:1'),
            'row_identity': lambda p: p['data']['data'][0].update(ItemID=11),
            'row_id': lambda p: p['data']['data'][0].update(ID=True),
            'total': lambda p: p['data'].update(total=2),
            'last_page': lambda p: p['data'].update(last_page=2),
            'page': lambda p: p['data'].update(current_page=2),
            'from': lambda p: p['data'].update({'from': 2}),
            'url': lambda p: p['data'].update(first_page_url='https://evil.test/db2/ItemXBonusTree'),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), self.assertRaises(ActivationSourceError):
                self.collect(FakeSource(fixture(), mutate=mutate))

    def test_total_changes_and_duplicate_rows_between_pages_rejected(self):
        records = fixture()
        records[('ItemXBonusTree', 'ItemID', 10)].append(
            {'ID': 1, 'ItemID': 10, 'ItemBonusTreeID': 21})
        with self.assertRaises(ActivationSourceError):
            self.collect(FakeSource(records, per_page=1))
        records[('ItemXBonusTree', 'ItemID', 10)][1]['ID'] = 5
        def mutate(props):
            if props['data']['current_page'] == 2:
                props['data']['total'] += 1
        with self.assertRaises(ActivationSourceError):
            self.collect(FakeSource(records, per_page=1, mutate=mutate))

    def test_missing_references_are_evidenced_not_invented(self):
        records = fixture()
        records[('ItemEffect', 'ID', 40)] = []
        result = self.collect(FakeSource(records))
        self.assertEqual(result['effects'], [])
        self.assertEqual(result['required_bonus_ids'], [])
        self.assertTrue(any(p['reason'] == 'missing_item_effect' for p in result['unresolved_paths']))

    def test_event_spell_ids_follow_only_exact_build_trigger_relations(self):
        records = fixture()
        records[('SpellEffect', 'SpellID', 50)] = [
            {'ID': 100, 'SpellID': 50, 'EffectTriggerSpell': 51}]
        records[('SpellEffect', 'SpellID', 51)] = [
            {'ID': 101, 'SpellID': 51, 'EffectTriggerSpell': 52}]
        records[('SpellEffect', 'SpellID', 52)] = [
            {'ID': 102, 'SpellID': 52, 'EffectTriggerSpell': 0}]
        result = self.collect(FakeSource(records))
        self.assertEqual(result['event_spell_ids'], [50, 51, 52])

    def test_invalid_inputs_do_not_make_requests(self):
        for kwargs in ({'item_id': True, 'game_build': BUILD},
                       {'item_id': 10, 'game_build': '70077'},
                       {'item_id': 10, 'game_build': BUILD, 'locale': 'enUS&x=1'}):
            source = FakeSource(fixture())
            with self.assertRaises(ActivationSourceError):
                ItemEffectActivationCollector(source=source).collect(**kwargs)
            self.assertEqual(source.calls, [])


if __name__ == '__main__':
    unittest.main()
