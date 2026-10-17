"""Real historical-model upgrade, including old-worker writes after migration.

Uses MigrationExecutor on the test database only; no fake migrations/mocked ORM.
The runner must leave DATABASES['default']['TEST']['MIGRATE'] enabled (default).
"""
from datetime import timedelta

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from botend.raiderio_identity import activity_identity_digest


class PlayerAnalysisMigrationTests(TransactionTestCase):
    migrate_from = [('botend', '0248_branch_localized_talent_names')]
    migrate_to = [('botend', '0249_player_analysis_management')]

    def test_upgrade_exact_backfill_preserves_facts_and_old_worker_defaults(self):
        self.assertTrue(connection.settings_dict.get('TEST', {}).get('MIGRATE', True))
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(self.migrate_to))
        old = executor.loader.project_state(self.migrate_from).apps
        Season = old.get_model('botend', 'SeasonMeta')
        Player = old.get_model('botend', 'PlayerSpecTopPlayer')
        Activity = old.get_model('botend', 'RaiderIOCharacterActivity')
        Analysis = old.get_model('botend', 'PlayerObservationAnalysis')
        Realm = old.get_model('botend', 'WowRealmDirectory')
        season = Season.objects.create(season_key='migration', season_name='Old', rio_season='old', mplus_zone_id=1, raid_zone_id=2, is_active=False)
        other_season = Season.objects.create(season_key='other', season_name='Other', rio_season='other', mplus_zone_id=3, raid_zone_id=4)
        Realm.objects.create(region='eu', slug='test-realm', name='Test Realm', localized_name='测试服', locale='en_GB')

        def player(name='Member0', slug='test-realm', **overrides):
            values = dict(season_id=season.pk, region='eu', realm=slug, character_name=name,
                          class_name='Priest', spec_name='Holy', rank=99,
                          profile_url=f'https://raider.io/characters/eu/{slug}/{name}')
            values.update(overrides)
            return Player.objects.create(**values)

        def activity(name='member0', slug='test-realm', **overrides):
            values = dict(season_id=season.pk, region='eu', realm_slug=slug, character_name=name,
                          identity_digest=activity_identity_digest('eu', slug, name))
            values.update(overrides)
            return Activity.objects.create(**values)

        wrong_season = player(season_id=other_season.pk)
        wrong_realm = player(slug='other-realm')
        representative = player()
        player(spec_name='Discipline')  # Same central identity, must not duplicate.
        unicode_representative = player(name='Mémber')
        player(name='Member', spec_name='Discipline')  # Keep distinct under legacy accent-insensitive player uniqueness.
        directory_representative = player(name='Directory', profile_url='', realm='测试服')
        player(name='Unknown', profile_url='', realm='unverified')
        player(name='Broken', profile_url='https://evil.invalid/characters/eu/test-realm/Broken')
        expected = [
            (activity(), representative.pk),
            (activity('Mémber'), unicode_representative.pk),
            (activity('directory'), directory_representative.pk),
            (activity('unknown', 'unverified'), None),
            (activity('broken'), None),
            (activity('member0', 'no-matching-realm'), None),
            (activity('orphan'), None),
        ]
        now = timezone.now()
        original = {}
        expected_ids = {}
        for index, (source, player_id) in enumerate(expected):
            row = Analysis.objects.create(activity=source, evidence_hash=str(index) * 64,
                result_json={'frozen_result': index}, evidence_json={'frozen_evidence': index},
                status='pending', prompt_version=5, generated_at=now,
                last_attempt_at=now, next_attempt_at=now + timedelta(minutes=30),
                lease_token='live-worker-' + str(index), lease_expires_at=now + timedelta(minutes=15), last_error='keep')
            original[row.pk] = Analysis.objects.values().get(pk=row.pk)
            expected_ids[row.pk] = player_id

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        new = executor.loader.project_state(self.migrate_to).apps
        NewAnalysis = new.get_model('botend', 'PlayerObservationAnalysis')
        self.assertEqual(NewAnalysis.objects.count(), len(expected))
        for row in NewAnalysis.objects.all():
            with self.subTest(id=row.pk):
                self.assertEqual(row.player_id, expected_ids[row.pk])
                self.assertEqual({key: getattr(row, key) for key in original[row.pk]}, original[row.pk])
                self.assertEqual(row.manual_json, {})
                self.assertEqual(row.review_status, 'pending')
                self.assertEqual(row.revision, 0)
                self.assertTrue(row.is_published)
                self.assertIsNone(row.reviewed_at)
                self.assertEqual(row.reviewed_evidence_hash, '')
                self.assertEqual(row.reviewed_result_hash, '')
        self.assertNotIn(wrong_season.pk, expected_ids.values())
        self.assertNotIn(wrong_realm.pk, expected_ids.values())
        # Execute SQL using the PRE-UPGRADE model, exactly like a running worker.
        # New non-null columns must have DB defaults, not only Python defaults.
        old_worker = Analysis.objects.create(activity=activity('late-worker'), result_json={'old_worker': True})
        late = NewAnalysis.objects.get(pk=old_worker.pk)
        self.assertEqual(late.manual_json, {})
        self.assertEqual(late.review_status, 'pending')
        self.assertTrue(late.is_published)
        self.assertEqual(late.revision, 0)
        self.assertEqual(late.reviewed_result_hash, '')
        Analysis.objects.filter(pk=old_worker.pk).update(status='ready', result_json={'old_worker': 'updated'})
        late.refresh_from_db()
        self.assertEqual(late.result_json, {'old_worker': 'updated'})
        # Representative deletion removes only the navigation FK, not evidence.
        new.get_model('botend', 'PlayerSpecTopPlayer').objects.filter(pk=representative.pk).delete()
        preserved = NewAnalysis.objects.get(pk=next(pk for pk, value in expected_ids.items() if value == representative.pk))
        self.assertIsNone(preserved.player_id)
        self.assertEqual(preserved.result_json, original[preserved.pk]['result_json'])
