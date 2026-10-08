"""验证真实旧版分类条件、跨赛季归属与页面标签一致。"""
from copy import deepcopy
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase

from botend.journal_models import JournalInstance, JournalRelease
from botend.models import SeasonMeta
from botend.services.journal_classification import JournalClassification
from botend.services.journal_service import compile_journal
from botend.tests import test_adventure_journal as journal_tests


TIERS = [
    {'id': 68, 'name': '经典旧世', 'order': 100},
    {'id': 73, 'name': '大地的裂变', 'order': 400},
    {'id': 516, 'name': '至暗之夜', 'order': 1200},
    {'id': 505, 'name': '本赛季', 'order': 9000},
]


class JournalClassificationTests(SimpleTestCase):
    def context(self, key='mn-s2', build='12.1.0.69587', overrides=None):
        release = SimpleNamespace(build=build, manifest={
            'catalog': {'tiers': TIERS}, 'display_overrides': overrides or {},
        })
        return JournalClassification(release, SimpleNamespace(season_key=key, season_name=key))

    def row(self, iid, **kwargs):
        return {'id': iid, 'kind': 'raid', 'tier_ids': [505, 516], **kwargs}

    def test_legacy_s1_raids_do_not_leak_into_s2(self):
        context = self.context()
        for iid in (1305, 1307, 1308, 1314):
            projected = context.project(self.row(iid))
            self.assertFalse(projected['is_current_season'])
            self.assertEqual(projected['tier_ids'], [516])
        for iid in (1317, 1320, 1030, 1041, 1202, 1304, 1309, 1311, 1313, 1322):
            self.assertTrue(context.project(self.row(iid))['is_current_season'])

    def test_switching_seasons_rechecks_conditions_and_unknown_condition_is_not_current(self):
        context = self.context('mn-s1')
        self.assertTrue(context.project(self.row(1307))['is_current_season'])
        self.assertFalse(context.project(self.row(1320))['is_current_season'])
        unknown = self.row(42, tier_links=[{'tier_id': 505, 'condition_id': 999999}])
        self.assertFalse(context.project(unknown)['is_current_season'])
        self.assertFalse(self.context('mn-s3').project(self.row(1320))['is_current_season'])

    def test_exact_build_fallback_does_not_override_new_links(self):
        row = self.row(1307, tier_links=[{'tier_id': 505, 'condition_id': 156363}])
        self.assertTrue(self.context().project(row)['is_current_season'])
        self.assertNotIn('12.1.0.99999', self.context().legacy_links)

    def test_manual_override_is_season_scoped_and_supports_aliases(self):
        override = {'1324': {'season_key': 'midnight-s2'}}
        self.assertTrue(self.context(overrides=override).project(self.row(1324))['is_current_season'])
        self.assertFalse(self.context('mn-s3', overrides=override).project(self.row(1324))['is_current_season'])

    def test_remade_instance_keeps_both_expansions_without_mutating_snapshot(self):
        row = self.row(63, tier_ids=[68, 73], kind='dungeon')
        before = deepcopy(row)
        projected = self.context().project(row)
        self.assertEqual(projected['tier_names'], ['经典旧世', '大地的裂变'])
        self.assertEqual(projected['tier_ids'], [68, 73])
        self.assertEqual(row, before)

    def test_world_group_and_affix_guide_have_correct_types(self):
        self.assertEqual(self.context().project(self.row(557))['kind'], 'world')
        self.assertEqual(self.context().project(self.row(1319))['kind'], 'affix')

    def test_compilation_preserves_conditions_and_order_for_future_syncs(self):
        tables = journal_tests.fixture()
        tables['JournalTier'].append({'ID': '505', 'Name_lang': '本赛季', 'Expansion': '9000'})
        tables['JournalTierXInstance'].append({
            'ID': '2', 'JournalInstanceID': '10', 'JournalTierID': '505',
            'AvailabilityCondition': '149388', 'OrderIndex': '2',
        })
        rows, _, _ = compile_journal(tables)
        self.assertIn({'tier_id': 505, 'condition_id': 149388, 'order': 2}, rows[0]['tier_links'])


class JournalClassificationViewTests(TestCase):
    def setUp(self):
        from botend.tests.journal_snapshot_fixtures import isolate_journal_snapshots
        isolate_journal_snapshots(self)
        tables = journal_tests.fixture()
        tables['JournalTier'] = [{'ID': str(t['id']), 'Name_lang': t['name'], 'Expansion': str(t['order'])} for t in TIERS]
        tables['JournalInstance'][0]['ID'] = '1307'
        tables['JournalEncounter'][0]['JournalInstanceID'] = '1307'
        tables['JournalTierXInstance'] = [
            {'ID': '1', 'JournalInstanceID': '1307', 'JournalTierID': '516'},
            {'ID': '2', 'JournalInstanceID': '1307', 'JournalTierID': '505', 'AvailabilityCondition': '149388'},
        ]
        self.release = journal_tests.JournalPublicationTests.publish(self, tables)
        self.season = SeasonMeta.objects.create(season_key='mn-s2', season_name='至暗之夜第 2 赛季',
                                               is_active=True, mplus_zone_id=1, raid_zone_id=2)

    def test_old_release_is_corrected_in_list_filter_detail_without_reimport(self):
        instance = JournalInstance.objects.get(release=self.release)
        instance.payload.pop('tier_links')
        instance.save(update_fields=['payload'])
        journal_tests.warm_journal()
        before = deepcopy(instance.payload)
        self.assertEqual(self.client.get('/portal/api/adventure-journal/').json()['instances'], [])
        all_rows = self.client.get('/portal/api/adventure-journal/', {'tier': ''}).json()['instances']
        self.assertEqual(len(all_rows), 1)
        self.assertFalse(all_rows[0]['is_current_season'])
        detail = self.client.get('/portal/api/adventure-journal/1307/').json()['instance']
        self.assertEqual(detail['tier_ids'], all_rows[0]['tier_ids'])
        response = self.client.get('/portal/adventure-journal/', {'tier': ''})
        self.assertNotContains(response, '正式服 12.1.0')
        self.assertNotContains(response, 'journal-source-badge--current')
        instance.refresh_from_db()
        self.assertEqual(instance.payload, before)
        self.assertEqual(JournalRelease.objects.count(), 1)

    def test_current_badge_matches_filter_and_detail(self):
        instance = JournalInstance.objects.get(release=self.release)
        instance.payload['tier_links'][1]['condition_id'] = 156363
        instance.save(update_fields=['payload'])
        journal_tests.warm_journal()
        response = self.client.get('/portal/adventure-journal/')
        self.assertContains(response, 'journal-source-badge--current')
        self.assertContains(response, '至暗之夜第 2 赛季')
        self.assertContains(self.client.get('/portal/adventure-journal/1307/'), 'journal-source-badge--current')

    def test_world_type_filter_uses_corrected_payload_instead_of_stale_model_column(self):
        instance = JournalInstance.objects.get(release=self.release)
        instance.journal_id = 557
        instance.kind = 'raid'
        instance.payload.update(id=557, kind='raid')
        instance.save()
        journal_tests.warm_journal()
        world = self.client.get('/portal/api/adventure-journal/', {'tier': '', 'kind': 'world'}).json()
        self.assertEqual([row['id'] for row in world['instances']], [557])
        raids = self.client.get('/portal/api/adventure-journal/', {'tier': '', 'kind': 'raid'}).json()
        self.assertEqual(raids['instances'], [])
