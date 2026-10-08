"""Catalog-only relation loading: real ORM/serializers, joined A/B oracle.

The ordinary fixture is synthetic test data; the exact-build fixture is the
repository's captured Item/ItemSparse evidence, not invented production data.
"""
from collections import Counter
from copy import deepcopy
import json
from unittest.mock import patch

from django.db import connection
from django.db.models import JSONField, QuerySet
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.services import gear_builder as gb
from botend.tests.test_gear_builder import GearBuilderTestDataMixin
from botend.tests import test_wow_item_identity as identity_tests

OLD, NEW = identity_tests.OLD, identity_tests.NEW


def measured_catalog(*, joined=False, **kwargs):
    """Force only the catalog item prefetch back to the original SQL join."""
    prefetch = QuerySet.prefetch_related
    converter = JSONField.from_db_value
    from_db = WowItemSnapshot.from_db
    counts = Counter()
    hydrated = []

    def load_relation(qs, *lookups):
        if joined and qs.model is WowItemVariantSnapshot and lookups == ('item',):
            return qs.select_related('item')
        return prefetch(qs, *lookups)

    def convert(field, value, expression, conn):
        counts[f'{field.model.__name__}.{field.name}'] += 1
        return converter(field, value, expression, conn)

    def load_item(db, fields, values):
        item = from_db(db, fields, values)
        hydrated.append((item, deepcopy({f.attname: getattr(item, f.attname)
                                       for f in item._meta.concrete_fields})))
        return item

    with patch.object(QuerySet, 'prefetch_related', load_relation), \
            patch.object(JSONField, 'from_db_value', convert), \
            patch.object(WowItemSnapshot, 'from_db', side_effect=load_item), \
            CaptureQueriesContext(connection) as queries:
        payload = gb.catalog_items(**kwargs)
    for item, before in hydrated:
        after = {f.attname: getattr(item, f.attname) for f in item._meta.concrete_fields}
        if before != after or hasattr(item, '_resolved_identity'):
            raise AssertionError('Catalog mutated a hydrated shared base item')
    return payload, counts, len(queries), hydrated


class CatalogPrefetchTests(GearBuilderTestDataMixin, TestCase):
    def setUp(self):
        super().setUp()
        # Many variants share one item; identical sort keys straddle page edges.
        for n in range(24):
            self.variant(self.helm, f'shared-{n:02d}')
        self.crafted_item.name = self.helm.name
        self.crafted_item.name_zh = self.helm.name_zh
        self.crafted_item.save(update_fields=['name', 'name_zh'])
        self.variant(self.helm, 'wrong-source', source_json=[{'type': 'delve'}])
        self.variant(self.helm, 'wrong-stat', stats_json={'strength': 10, 'mastery': 20})
        self.variant(self.helm, 'invalid-track', upgrade_track='myth', source_json=[{'type': 'delve'}])
        self.chest = self.item(11001, slot_key='chest', inventory_type=5)
        self.variant(self.chest, 'wrong-slot', compatible_slots=['chest'])
        self.mage = self.item(11002, eligible_specs=['Mage:Fire'])
        self.variant(self.mage, 'wrong-spec')
        self.weapon = self.item(11003, slot_key='main_hand', item_class_id=2,
                                item_subclass_id=8, inventory_type=17)
        self.variant(self.weapon, 'twohand', compatible_slots=['main_hand'])
        self.variant(self.helm, 'old-batch', batch_key='obsolete')
        other = SeasonMeta.objects.create(season_key='other', season_name='other',
            mplus_zone_id=3, raid_zone_id=4, is_active=False)
        self.variant(self.helm, 'other-season', season=other)

    def item(self, item_id, **kwargs):
        fields = dict(name='Fixture', name_zh='Fixture', catalog_type='equipment',
                      slot_key='head', item_class_id=4, item_subclass_id=4,
                      inventory_type=1, quality=4)
        fields.update(kwargs)
        return WowItemSnapshot.objects.create(item_id=item_id, **fields)

    def variant(self, item, key, **kwargs):
        fields = dict(season=self.season, batch_key='test-batch',
            game_build=self.season.game_build, variant_type=WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT,
            item_level=720, compatible_slots=['head'], stats_json={'strength': 10, 'crit': 20},
            source_json=[{'type': 'raid'}])
        fields.update(kwargs)
        return WowItemVariantSnapshot.objects.create(item=item, variant_key=key, **fields)

    def compare(self, **kwargs):
        args = dict(class_name='Warrior', spec_name='Fury', slot='head', page_size=1)
        args.update(kwargs)
        baseline = measured_catalog(joined=True, **args)
        candidate = measured_catalog(**args)
        self.assertEqual(candidate[0], baseline[0])
        return baseline, candidate

    def test_hydrates_each_item_once_with_identical_full_response(self):
        baseline, candidate = self.compare()
        cohort = gb._catalog_queryset(self.season, (
            WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT, WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT))
        variants = cohort.count()
        items = len(set(cohort.values_list('item_id', flat=True)))
        print('CATALOG_PREFETCH', json.dumps(dict(variants=variants, items=items,
            joined_queries=baseline[2], candidate_queries=candidate[2],
            joined_json=dict(baseline[1]), candidate_json=dict(candidate[1]))))
        for field in ('metadata', 'eligible_specs', 'effect_refs'):
            key = f'WowItemSnapshot.{field}'
            self.assertEqual(baseline[1][key], variants)
            self.assertEqual(candidate[1][key], items)
        self.assertEqual(len(candidate[3]), items)
        self.assertEqual(candidate[2], baseline[2] + 1)
        self.assertEqual({k: v for k, v in baseline[1].items() if k.startswith('WowItemVariantSnapshot.')},
                         {k: v for k, v in candidate[1].items() if k.startswith('WowItemVariantSnapshot.')})
        # The common helper remains joined for enhancement consumers.
        self.assertEqual(cohort.query.select_related, {'item': {}})
        self.assertEqual(cohort._prefetch_related_lookups, ())

    def test_same_item_builds_remain_separate_paginated_groups(self):
        self.variant(self.helm, 'other-build', game_build='12.1.0.99998')
        _, full = self.compare(page_size=100)
        self.assertEqual(full[0]['total'], 3)
        helm_rows = [row for row in full[0]['items'] if row['item_id'] == self.helm.item_id]
        self.assertEqual(len(helm_rows), 2)
        self.assertEqual({tuple({v['game_build'] for v in row['variants']}) for row in helm_rows},
                         {('12.1.0.99998',), (self.season.game_build,)})
        pages = [self.compare(page=n)[1][0]['items'][0] for n in (1, 2, 3)]
        self.assertEqual(pages, full[0]['items'])

    def test_filters_scope_ties_and_complete_groups_across_pages(self):
        _, full = self.compare(page_size=100)
        rows = full[0]['items']
        self.assertEqual(full[0]['total'], 2)
        # Same displayed name and top ilvl: original variant-key tie ordering.
        self.assertEqual([row['item_id'] for row in rows], [self.crafted_item.item_id, self.helm.item_id])
        paged = []
        for page in (1, 2, 3):
            _, result = self.compare(page=page)
            self.assertEqual(result[0]['total'], 2)
            paged.extend(result[0]['items'])
        self.assertEqual(paged, rows)
        keys = {v['key'] for row in rows for v in row['variants']}
        self.assertTrue({f'shared-{n:02d}' for n in range(24)} <= keys)
        self.assertFalse(keys & {'old-batch', 'other-season', 'wrong-slot', 'wrong-spec', 'invalid-track'})
        for args, excluded in (({'source_type': 'raid'}, {'wrong-source', 'crafted-q5-720'}),
                               ({'excluded_sources': ['delve']}, {'wrong-source'}),
                               ({'excluded_stats': ['mastery']}, {'wrong-stat'})):
            _, result = self.compare(page_size=100, **args)
            keys = {v['key'] for row in result[0]['items'] for v in row['variants']}
            self.assertFalse(keys & excluded)
            self.assertIn('shared-00', keys)
        for query in (str(self.helm.item_id), self.helm.name_zh, 'no-match'):
            self.compare(query=query)
        _, fury = self.compare(slot='off_hand')
        self.assertEqual([row['item_id'] for row in fury[0]['items']], [self.weapon.item_id])
        _, arms = self.compare(slot='off_hand', spec_name='Arms')
        self.assertEqual(arms[0]['items'], [])


class CatalogPrefetchExactBuildTests(TestCase):
    def setUp(self):
        base = deepcopy(identity_tests.FIXTURE['catalog']['items'][0])
        self.item = WowItemSnapshot.objects.create(**base)
        rows = identity_tests.FIXTURE['catalog']['variants']
        self.season = SeasonMeta.objects.create(id=rows[0]['season_id'],
            season_key='prefetch-identity', season_name='identity', mplus_zone_id=1,
            raid_zone_id=2, is_active=True, gear_batch_key=rows[0]['batch_key'])
        for row in rows:
            WowItemVariantSnapshot.objects.create(**deepcopy(row))
        self.item.metadata['item_identity_by_build'] = {
            build: {'is_ptr': True, 'fact': identity_tests.fact(build)} for build in (OLD, NEW)}
        self.item.save(update_fields=['metadata'])
        # Same captured item, two independently captured exact identities.
        ids = list(WowItemVariantSnapshot.objects.order_by('pk').values_list('pk', flat=True))
        WowItemVariantSnapshot.objects.filter(pk__in=ids[:9]).update(game_build=OLD)
        WowItemVariantSnapshot.objects.filter(pk__in=ids[9:]).update(game_build=NEW)

    def test_shared_base_is_not_mutated_by_two_build_projections(self):
        before = list(WowItemSnapshot.objects.values())
        for slot, build, name in (('chest', OLD, "Voidweaver's Vestments"),
                                  ('legs', NEW, "Voidweaver's Leggings")):
            args = dict(class_name='Mage', spec_name='Fire', slot=slot, page_size=1)
            baseline = measured_catalog(joined=True, **args)
            candidate = measured_catalog(**args)
            self.assertEqual(candidate[0], baseline[0])
            self.assertEqual(candidate[0]['total'], 1)
            row = candidate[0]['items'][0]
            self.assertEqual(row['name_en'], name)
            self.assertEqual(row['slot'], slot)
            self.assertEqual(len(row['variants']), 9)
            self.assertEqual({v['game_build'] for v in row['variants']}, {build})
            for variant in row['variants']:
                self.assertEqual(variant['item_identity']['game_build'], build)
                self.assertIs(variant['item_identity']['is_ptr'], True)
                self.assertEqual(variant['compatible_slots'], [slot])
            self.assertEqual(candidate[1]['WowItemSnapshot.metadata'], 1)
        self.assertEqual(list(WowItemSnapshot.objects.values()), before)
