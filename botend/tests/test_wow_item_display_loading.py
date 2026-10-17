"""Real-ORM regressions for shared item reads; no replacement serializers/loaders."""
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
import sys

from django.db import IntegrityError, connection, transaction
from django.db.models import JSONField
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.services.wow_item_display import (
    item_display_metadata, load_item_tooltip_metadata, refresh_aggregate_equipment,
)
from botend.services.wow_item_effects import compile_effects


@contextmanager
def observe_item_reads():
    """Observe real JSON decoding and serializer arguments without replacing either."""
    decoded = Counter()
    projections = []
    previous = sys.getprofile()

    def observe(frame, event, arg):
        if event == 'call':
            if frame.f_code is JSONField.from_db_value.__code__:
                field = frame.f_locals['self']
                if field.model is WowItemSnapshot:
                    decoded[field.name] += 1
            elif frame.f_code is item_display_metadata.__code__:
                projections.append((frame.f_locals['snapshot'], frame.f_locals['variant']))
        if previous:
            previous(frame, event, arg)

    sys.setprofile(observe)
    try:
        yield decoded, projections
    finally:
        sys.setprofile(previous)


class ItemDisplayLoadingTests(TestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(
            season_key='display-loading', season_name='读取去重',
            mplus_zone_id=1, raid_zone_id=2, gear_batch_key='current', is_active=True,
        )
        # The public ItemID is deliberately different from the ORM primary key.
        self.item = WowItemSnapshot.objects.create(
            item_id=900001, name='Loading test', name_zh='读取测试',
            catalog_type='equipment', slot_key='head', quality=4,
            description_zh='头部\n+999 智力\n装备：旧效果',
            metadata={'raw_evidence': '保留完整证据' * 2000,
                      'branch_display': {'ptr': {'name_zh': '测试服装备'},
                                         'beta': {'name_zh': '内测服装备'}}},
        )

    def variant(self, key, level, **changes):
        values = dict(
            item=self.item, season=self.season, batch_key='current', variant_key=key,
            variant_type='drop_equipment', item_level=level, data_branch='retail',
            game_build='12.1.0.1', bonus_ids=[100], stats_json={'intellect': level},
            effects_json=[], source_json=[{'type': 'raid', 'instance': '测试来源'}],
        )
        values.update(changes)
        return WowItemVariantSnapshot.objects.create(**values)

    def legacy_projection(self, variant, *, item_id=None, item_level=0, branch='retail',
                          primary_stat='', strict=False):
        """Old independent joined item instances, with the real compiler/serializer."""
        item_id = self.item.item_id if item_id is None else item_id
        snapshot = WowItemSnapshot.objects.filter(item_id=item_id).first()
        rows = list(WowItemVariantSnapshot.objects.filter(
            item__item_id=item_id, season=self.season, batch_key='current',
        ).select_related('item'))
        if snapshot and (not isinstance(snapshot.effects, dict)
                         or snapshot.effects.get('schema') != 1):
            snapshot.effects = compile_effects(rows)[0]
        selected = next((row for row in rows if variant and row.pk == variant.pk), None)
        return item_display_metadata(
            item_id, snapshot, variant=selected, item_level=item_level,
            primary_stat=primary_stat, game_build=selected.game_build if selected else '',
            is_ptr=(branch == 'ptr') if branch != 'beta' else None,
            data_branch=branch, strict_difficulty=strict,
        )

    def test_variants_reuse_one_full_item_decode_without_join_or_lazy_reads(self):
        variants = [self.variant(f'level-{level}', level) for level in (250, 260, 270, 280, 290)]
        requests = [dict(item_id=self.item.item_id, item_level=v.item_level)
                    for v in reversed(variants)] * 2
        with CaptureQueriesContext(connection) as queries, observe_item_reads() as (decoded, projections):
            actual = load_item_tooltip_metadata(requests)
        self.assertEqual(len(queries), 3)
        with self.subTest(contract='single central metadata decode'):
            self.assertEqual(decoded['metadata'], 1)
        with self.subTest(contract='no variant join including model default ordering'):
            sql = queries[-1]['sql']
            self.assertIn('wow_item_variant_snapshot', sql)
            self.assertNotIn('JOIN', sql.upper())
            self.assertNotIn('wow_item_snapshot', sql)
        with self.subTest(contract='shared central instance'):
            for snapshot, variant in projections:
                self.assertIs(snapshot, projections[0][0])
                self.assertIs(variant.item, snapshot)
        expected = [self.legacy_projection(v, item_level=v.item_level)
                    for v in reversed(variants)] * 2
        self.assertEqual(actual, expected)

    def test_selection_order_branches_bonuses_missing_and_complete_are_unchanged(self):
        low = self.variant('low', 250)
        # Lexical ordering differs from PK tie-breaking on purpose.
        high = self.variant('z-high', 300, bonus_ids=[101], effects_json=['装备：提高30急速。'])
        tie = self.variant('a-high', 300, bonus_ids=[102],
                           metadata={'primary_stat_values': {'strength': 30, 'agility': 30}})
        self.variant('highest-incomplete', 310)
        ptr = self.variant('ptr', 320, data_branch='ptr')
        beta = self.variant('beta', 330, data_branch='beta')
        self.variant('stale-batch', 999, batch_key='old')
        other_season = SeasonMeta.objects.create(season_key='inactive', season_name='旧赛季',
            mplus_zone_id=3, raid_zone_id=4, is_active=False, gear_batch_key='current')
        self.variant('inactive-season', 999, season=other_season)
        cases = [
            ({'item_level': 300}, high, 'retail', ''),
            ({'item_level': 300, 'bonus_ids': '102;102'}, tie, 'retail', ''),
            ({'item_level': 300, 'bonus_ids': [102], 'primary_stat': 'agility'}, tie, 'retail', 'agility'),
            ({'allow_default_variant': True, 'default_variant_order': 'lowest'}, low, 'retail', ''),
            ({'allow_default_variant': True, 'require_complete_variant': True}, high, 'retail', ''),
            ({'bonus_ids': [101]}, high, 'retail', ''),
            ({'is_ptr': True, 'allow_default_variant': True}, ptr, 'ptr', ''),
            ({'data_branch': 'beta', 'allow_default_variant': True}, beta, 'beta', ''),
            ({'item_level': 999}, None, 'retail', ''),
            ({}, None, 'retail', ''),
            ({'item_id': 900099, 'item_level': 300}, None, 'retail', ''),
        ]
        requests = [dict(item_id=self.item.item_id, **changes) if 'item_id' not in changes else changes
                    for changes, _, _, _ in cases]
        actual = load_item_tooltip_metadata(requests)
        for request, shown, (_, selected, branch, primary) in zip(requests, actual, cases):
            with self.subTest(request=request):
                self.assertEqual(shown, self.legacy_projection(
                    selected, item_id=request['item_id'], item_level=request.get('item_level', 0),
                    branch=branch, primary_stat=primary,
                ))

    def test_compiled_effects_share_only_call_local_state_and_preserve_projection(self):
        bilingual = self.variant('bilingual', 250, effects_json=[{
            'spell_id': 900, 'description': 'Gain 100 Haste for 10 seconds.',
            'description_zh': '装备：获得100急速，持续10秒。',
        }])
        english = self.variant('english', 300, effects_json=[{
            'spell_id': 900, 'description': 'Gain 200 Haste for 10 seconds.',
        }])
        ptr = self.variant('ptr', 300, data_branch='ptr', effects_json=[{
            'spell_id': 900, 'description': 'Gain 200 Haste for 10 seconds.',
        }])
        requests = [dict(item_id=self.item.item_id, item_level=300),
                    dict(item_id=self.item.item_id, item_level=300, data_branch='ptr'),
                    dict(item_id=self.item.item_id),
                    dict(item_id=self.item.item_id, item_level=300)]
        # Exercise both legacy in-memory compilation and an already published document.
        for persisted in (False, True):
            with self.subTest(persisted=persisted):
                if persisted:
                    self.item.effects = compile_effects([bilingual, english, ptr])[0]
                    self.item.save(update_fields=['effects'])
                before_items = list(WowItemSnapshot.objects.values())
                before_variants = list(WowItemVariantSnapshot.objects.values())
                expected = [self.legacy_projection(english, item_level=300),
                            self.legacy_projection(ptr, item_level=300, branch='ptr'),
                            self.legacy_projection(None),
                            self.legacy_projection(english, item_level=300)]
                with CaptureQueriesContext(connection) as queries, observe_item_reads() as (_, calls):
                    shown = load_item_tooltip_metadata(requests)
                self.assertEqual(shown, expected)
                self.assertEqual(shown[0]['effects'], ['装备：获得200急速，持续10秒。'])
                self.assertEqual(shown[1]['effects'], ['Gain 200 Haste for 10 seconds.'])
                self.assertIn('数值待确认', shown[2]['effects'][0])
                self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
                self.assertEqual(list(WowItemSnapshot.objects.values()), before_items)
                self.assertEqual(list(WowItemVariantSnapshot.objects.values()), before_variants)
                for snapshot, variant in calls:
                    self.assertEqual(snapshot.effects['schema'], 1)
                    if variant:
                        self.assertIs(variant.item, snapshot)
                        self.assertIs(variant.item.effects, snapshot.effects)
                # No global instance cache: a subsequent call observes a real DB update.
                old_name = self.item.name_zh
                WowItemSnapshot.objects.filter(pk=self.item.pk).update(name_zh='本次新名称')
                self.assertEqual(load_item_tooltip_metadata(requests[:1])[0]['name_zh'], '本次新名称')
                WowItemSnapshot.objects.filter(pk=self.item.pk).update(name_zh=old_name)

    def test_strict_difficulty_and_unique_branch_inference_remain_isolated(self):
        ptr = self.variant('ordinary-ptr', 320, data_branch='ptr')
        drop = dict(schema=1, provider='wago', difficulty_id=1, item_context=0,
                    instance_id=10, rule_build='12.1.0.1', quality=3, bonus_ids=[100])
        strict = self.variant('journal-difficulty:retail', 250,
                              metadata={'difficulty_drop': drop, 'data_branch': 'retail'})
        ordinary = dict(item_id=self.item.item_id, allow_default_variant=True)
        request = dict(item_id=self.item.item_id, data_branch='retail', difficulty_id=1,
                       item_context=0, instance_id=10, difficulty_rule_build='12.1.0.1')
        shown = load_item_tooltip_metadata([ordinary, request, dict(request, difficulty_id=2),
                                            dict(request, data_branch=''), dict(request, data_branch='ptr')])
        self.assertEqual(shown[0], self.legacy_projection(ptr, branch='ptr'))
        expected = self.legacy_projection(strict, strict=True)
        expected.update(base_quality=4, difficulty_status='resolved', difficulty_quality=3, quality=3)
        self.assertEqual(shown[1], expected)
        for missing in shown[2:]:
            self.assertEqual(missing['difficulty_status'], 'unresolved')
            self.assertIsNone(missing['variant_id'])
            self.assertIsNone(missing['quality'])
            self.assertIsNone(missing['item_level'])
            self.assertEqual(missing['stats'], {})
            self.assertFalse(missing['tooltip_complete'])
        self.variant('journal-conflict', 260, metadata=strict.metadata)
        self.assertEqual(load_item_tooltip_metadata([request])[0]['difficulty_status'], 'unresolved')

    def test_refresh_name_probe_excludes_metadata_and_preserves_non_equipment(self):
        self.variant('lowest', 250)
        blank = WowItemSnapshot.objects.create(item_id=900002, name='English only', name_zh='',
                                               metadata={'raw_evidence': '不要读取' * 2000})
        whitespace = WowItemSnapshot.objects.create(item_id=900003, name_zh=' ',
                                                    metadata={'raw_evidence': '保留空白名真值'})
        payload = {'boss': {'id': self.item.item_id, 'name_zh': '首领，不是装备'},
                   'gear_popularity': [
                       {'itemID': self.item.item_id, 'itemLevel': 250, 'name_zh': '旧名称',
                        'count': 7, 'percent': 25, 'bonusIDs': [100], 'custom': {'note': '不变'}},
                       {'item_id': blank.item_id, 'name_zh': '空中文不覆盖', 'tooltip': '旧提示'},
                       {'item_id': whitespace.item_id, 'name_zh': '空白中文仍为真'},
                       {'item_id': 900099, 'name_zh': '缺失不覆盖'}],
                   'talents': [{'id': self.item.item_id, 'name_zh': '天赋不变'}]}
        expected = deepcopy(payload)
        for index, item_id in ((0, self.item.item_id), (2, whitespace.item_id)):
            display = load_item_tooltip_metadata([dict(
                item_id=item_id, item_level=250 if index == 0 else None,
                bonus_ids=[100] if index == 0 else None, allow_default_variant=True,
                default_variant_order='lowest', class_name='mage', spec_name='frost',
            )])[0]
            for key in ('name_zh', 'display_name', 'description_zh', 'display_description', 'tooltip'):
                expected['gear_popularity'][index][key] = display[key]
        with CaptureQueriesContext(connection) as queries, observe_item_reads() as (decoded, _):
            result = refresh_aggregate_equipment(payload, class_name='mage', spec_name='frost')
        self.assertIs(result, payload)
        self.assertEqual(result, expected)
        with self.subTest(contract='name eligibility only transfers identity and name'):
            self.assertNotIn('metadata', queries[0]['sql'])
            self.assertIn('item_id', queries[0]['sql'])
            self.assertIn('name_zh', queries[0]['sql'])
        with self.subTest(contract='only selected full items decode metadata'):
            self.assertEqual(decoded['metadata'], 2)
        self.assertEqual(len(queries), 4)

    def test_item_id_is_unique_and_empty_or_no_catalog_requests_stay_supported(self):
        self.assertTrue(WowItemSnapshot._meta.get_field('item_id').unique)
        self.assertNotEqual(self.item.pk, self.item.item_id)
        with self.assertRaises(IntegrityError), transaction.atomic():
            WowItemSnapshot.objects.create(item_id=self.item.item_id)
        with self.assertNumQueries(0):
            self.assertEqual(load_item_tooltip_metadata([]), [])
        self.season.is_active = False
        self.season.save(update_fields=['is_active'])
        shown = load_item_tooltip_metadata([self.item.item_id, 900099, 'invalid'])
        self.assertEqual([row['item_id'] for row in shown], [self.item.item_id, 900099, None])
        self.assertTrue(all(row['variant_id'] is None for row in shown))
        self.assertEqual(shown[0]['name_zh'], self.item.name_zh)
