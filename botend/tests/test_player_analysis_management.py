"""Management contracts exercised through the real ORM, without transport calls."""
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from botend.models import PlayerObservationAnalysis, PlayerSpecTopPlayer, RaiderIOCharacterActivity, SeasonMeta
from botend.services import player_analysis_management as service
from botend.services.player_raiderio_activity import player_identity


class PlayerAnalysisManagementTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='reviewer')
        self.season = SeasonMeta.objects.create(season_key='management', season_name='Management', rio_season='season-mn-2', mplus_zone_id=1, raid_zone_id=2)
        self.player = self.make_player()
        self.manual = {'tags': [{'label': '组队', 'value': '固定队', 'reason': '<b>人工证据</b>', 'evidence': [{'text': '通关', 'url': 'https://raider.io/example'}]}], 'conclusion': '<script>plain text</script>', 'evidence': []}

    def make_player(self, **kwargs):
        values = dict(season_id=self.season.pk, region='eu', realm='Test Realm', character_name='Member0', class_name='Priest', spec_name='Holy', rank=1, profile_url='https://raider.io/characters/eu/test-realm/Member0')
        values.update(kwargs)
        return PlayerSpecTopPlayer.objects.create(**values)

    def create(self, **kwargs):
        payload = dict(player_id=self.player.pk, manual=self.manual, review_status='pending', is_published=False, review_note='')
        payload.update(kwargs)
        return service.create_analysis(payload, self.user)

    def test_create_review_archive_and_restore_ai(self):
        detail = self.create()
        row = PlayerObservationAnalysis.objects.get(pk=detail['id'])
        self.assertEqual(detail['source_kind'], 'manual')
        self.assertEqual(detail['player_id'], self.player.pk)
        self.assertEqual(detail['manual'], self.manual)
        self.assertEqual(row.activity.identity_digest, service.activity_identity_digest(**player_identity(self.player)))
        self.assertEqual(row.result_json, {})
        with self.assertRaises(ValidationError):
            service.update_analysis(row.pk, {'revision': 0, 'is_published': True}, self.user)
        detail = service.update_analysis(row.pk, {'revision': 0, 'review_status': 'approved', 'is_published': True}, self.user)
        self.assertEqual(detail['revision'], 1)
        row.refresh_from_db()
        self.assertEqual(row.reviewed_by_id, self.user.pk)
        self.assertIsNotNone(row.reviewed_at)
        with self.assertRaises(service.AnalysisConflict):
            service.update_analysis(row.pk, {'revision': 0, 'review_note': 'lost update'}, self.user)
        detail = service.archive_analysis(row.pk, {'revision': 1}, self.user)
        self.assertFalse(detail['is_published'])
        self.assertEqual(detail['manual'], self.manual)
        detail = service.update_analysis(row.pk, {'revision': 2, 'manual': None}, self.user)
        self.assertIsNone(detail['manual'])
        self.assertEqual(detail['source_kind'], 'ai')
        self.assertTrue(PlayerObservationAnalysis.objects.filter(pk=row.pk).exists())

    def test_review_is_bound_to_result_as_well_as_evidence(self):
        detail = self.create()
        row = PlayerObservationAnalysis.objects.get(pk=detail['id'])
        row.manual_json = {}
        row.result_json = {'observations': [], 'conclusion_id': 'first'}
        row.evidence_hash = 'a' * 64
        row.save()
        detail = service.update_analysis(row.pk, {'revision': row.revision, 'review_status': 'approved'}, self.user)
        self.assertTrue(detail['review_matches_ai'])
        PlayerObservationAnalysis.objects.filter(pk=row.pk).update(result_json={'observations': [], 'conclusion_id': 'changed'})
        self.assertFalse(service.get_analysis(row.pk)['review_matches_ai'])
        detail = service.update_analysis(row.pk, {'revision': detail['revision'], 'review_status': 'approved'}, self.user)
        self.assertTrue(detail['review_matches_ai'])
        self.assertEqual(len(detail['reviewed_result_hash']), 64)

    def test_cross_spec_conflict_wrong_realm_and_unresolved(self):
        detail = self.create()
        sibling = self.make_player(spec_name='Discipline')
        with self.assertRaises(service.AnalysisConflict) as raised:
            service.create_analysis({'player_id': sibling.pk, 'manual': self.manual}, self.user)
        self.assertEqual(raised.exception.existing_id, detail['id'])
        other = self.make_player(realm='Other', profile_url='https://raider.io/characters/eu/other/Member0')
        different = service.create_analysis({'player_id': other.pk, 'manual': self.manual}, self.user)
        self.assertNotEqual(different['activity_id'], detail['activity_id'])
        unresolved = self.make_player(realm='Unknown', profile_url='')
        with self.assertRaises(ValidationError):
            service.create_analysis({'player_id': unresolved.pk, 'manual': self.manual}, self.user)
        self.assertEqual(PlayerObservationAnalysis.objects.count(), 2)

    def test_validation_atomicity_and_server_owned_fields(self):
        detail = self.create()
        invalid = [
            {'manual': {'tags': [], 'conclusion': '', 'evidence': [{'text': 'bad', 'url': 'javascript:alert(1)'}]}},
            {'manual': {'tags': 'bad'}}, {'manual': []}, {'result_json': {}},
            {'player_id': self.player.pk}, {'reviewed_by': self.user.pk}, {'review_status': 'fake'},
            {'is_published': 'false'}, {'review_status': 'rejected', 'is_published': True},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                service.update_analysis(detail['id'], {'revision': 0, **payload}, self.user)
        for revision in (None, True, -1, '0'):
            with self.subTest(revision=revision), self.assertRaises(ValidationError):
                service.archive_analysis(detail['id'], {'revision': revision}, self.user)
        row = PlayerObservationAnalysis.objects.get(pk=detail['id'])
        self.assertEqual(row.revision, 0)
        self.assertEqual(row.manual_json, self.manual)
        self.assertEqual(row.result_json, {})

    def test_frozen_ai_projection_and_lightweight_list(self):
        activity = RaiderIOCharacterActivity.objects.create(season=self.season, **player_identity(self.player))
        evidence = {'eligible_labels': [{'code': 'historical', 'label': '历史标签', 'reason_options': {'old': '历史理由'}}], 'evidence': [{'id': 'e1', 'text': '冻结事实', 'run_url': 'https://raider.io/old'}], 'conclusion_options': {'old': {'text': '历史结论'}}}
        result = {'observations': [{'code': 'historical', 'confidence': '中', 'reason_id': 'old', 'evidence_ids': ['e1']}], 'conclusion_id': 'old'}
        row = PlayerObservationAnalysis.objects.create(activity=activity, player=self.player, status='ready', evidence_json=evidence, result_json=result)
        detail = service.get_analysis(row.pk)
        self.assertEqual(detail['ai']['conclusion'], '历史结论')
        self.assertEqual(detail['ai']['tags'][0]['reason'], '历史理由')
        self.assertEqual(detail['ai']['evidence'][0]['text'], '冻结事实')
        self.assertEqual(detail['ai']['result_json'], result)
        with CaptureQueriesContext(connection) as captured:
            listing = service.list_analyses({'q': 'Member', 'page_size': '100', 'source': 'ai', 'visibility': 'published'})
        self.assertEqual(listing['total'], 1)
        self.assertEqual(listing['page_size'], 50)
        self.assertNotIn('ai', listing['items'][0])
        selects = [q['sql'] for q in captured if q['sql'].lstrip().upper().startswith('SELECT')]
        self.assertEqual(listing['items'][0]['conclusion'], '历史结论')
        self.assertEqual(listing['items'][0]['tags'][0]['label'], '历史标签')
        for sql in selects:
            self.assertNotIn('gear_json', sql)
            # JSON path extraction is allowed; selecting the full frozen blob is not.
            self.assertNotRegex(sql, r'(?:SELECT\s+|,\s*)["`]wow_player_observation_analysis["`]\.["`]evidence_json["`](?:\s*,|\s+FROM)')
        self.assertLessEqual(len(selects), 3)
        self.assertEqual(service.list_analyses({'q': str(row.pk)})['total'], 1)
        self.assertEqual(service.list_analyses({'q': 'test-realm'})['total'], 1)
        self.assertEqual(service.list_characters({'q': 'Member'})['items'][0]['id'], self.player.pk)
        with self.assertRaises(ValidationError):
            service.list_analyses({'page': 'not-a-number'})
        self.player.delete()
        self.assertIsNone(service.get_analysis(row.pk)['player'])
        self.user.delete()
        self.assertTrue(PlayerObservationAnalysis.objects.filter(pk=row.pk).exists())

    def test_manual_projection_outside_top20_and_whole_record_hide(self):
        from botend.services.player_observation import project_player_observation
        self.player.rank = 99
        self.player.save(update_fields=['rank'])
        detail = self.create()
        self.assertIsNone(project_player_observation(self.player))
        detail = service.update_analysis(detail['id'], {'revision': 0, 'review_status': 'approved', 'is_published': True}, self.user)
        sibling = self.make_player(spec_name='Discipline', rank=50)
        with CaptureQueriesContext(connection) as captured:
            view = project_player_observation(sibling)
        self.assertEqual(view['source_kind'], 'manual')
        self.assertEqual(view['activity_id'], detail['activity_id'])
        self.assertEqual(view['player_id'], self.player.pk)
        self.assertEqual(view['conclusion'], self.manual['conclusion'])
        self.assertEqual(view['display_judgements'][0]['value'], '固定队')
        self.assertFalse(any(sql['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for sql in captured))
        self.assertFalse(any('wow_raiderio_run_roster' in sql['sql'] for sql in captured))
        other = self.make_player(realm='Other', profile_url='https://raider.io/characters/eu/other/Member0', rank=99)
        self.assertIsNone(project_player_observation(other))
        row = PlayerObservationAnalysis.objects.get(pk=detail['id'])
        row.activity.payload_json = {'raid_first_kills': {'status': 'ready'}}
        row.activity.save(update_fields=['payload_json'])
        service.archive_analysis(row.pk, {'revision': detail['revision']}, self.user)
        self.assertIsNone(project_player_observation(sibling))
        # Even direct legacy/inconsistent rows cannot leak rejected content/raid.
        PlayerObservationAnalysis.objects.filter(pk=row.pk).update(is_published=True, review_status='rejected', manual_json={})
        self.assertIsNone(project_player_observation(sibling))

    def test_reapproval_on_manual_change_and_nullable_reviewer(self):
        detail = self.create(review_status='approved', is_published=True)
        changed = {**self.manual, 'conclusion': 'edited'}
        detail = service.update_analysis(detail['id'], {'revision': 0, 'manual': changed}, self.user)
        self.assertEqual(detail['review_status'], 'pending')
        self.assertFalse(detail['is_published'])
        self.user.delete()
        row = PlayerObservationAnalysis.objects.get(pk=detail['id'])
        self.assertIsNone(row.reviewed_by_id)
        self.player.delete()
        self.assertEqual(service.list_analyses({'q': 'test-realm', 'source': 'manual'})['total'], 1)
        self.assertIsNone(service.get_analysis(row.pk)['player_id'])


class PlayerAnalysisGenerationTests(TestCase):
    def setUp(self):
        from botend.tests.test_player_observation import PlayerObservationTests
        PlayerObservationTests.setUp(self)
        self.user = get_user_model().objects.create_user(username='reviewer')

    def engine(self, before_return=None):
        import json
        class Engine:
            def send_message(self, prompt, **kwargs):
                evidence = json.loads(prompt)
                label = evidence['eligible_labels'][0]
                if before_return:
                    before_return()
                return json.dumps({'observations': [{'code': label['code'], 'confidence': '低',
                    'reason_id': next(iter(label['reason_options'])), 'limitation_ids': label['required_limitation_ids'],
                    'evidence_ids': label['evidence_ids']}], 'conclusion_id': 'public_sample'})
        return Engine()

    def test_ai_refresh_cannot_overwrite_concurrent_editorial_write(self):
        from botend.services.player_observation import generate_observation, project_player_observation
        generated = generate_observation(self.player, engine=self.engine())
        self.assertEqual(generated['status'], 'ready')
        row = PlayerObservationAnalysis.objects.get(pk=generated['analysis_id'])
        self.assertEqual(row.player_id, self.player.pk)
        detail = service.update_analysis(row.pk, {'revision': row.revision, 'review_status': 'approved'}, self.user)
        self.assertTrue(detail['review_matches_ai'])
        old_hash = detail['reviewed_evidence_hash']
        self.activity.refresh_from_db()
        self.activity.payload_json['mythic_plus_dungeon_run_counts'] = [{'season_runs_total': 10, 'season_runs_timed': 8}]
        self.activity.save(update_fields=['payload_json'])
        manual = {'tags': [], 'conclusion': '人工结论', 'evidence': []}
        def edit_while_ai_runs():
            service.update_analysis(row.pk, {'revision': detail['revision'], 'manual': manual,
                'review_status': 'approved', 'is_published': False, 'review_note': 'keep'}, self.user)
        self.assertEqual(generate_observation(self.player, force=True, engine=self.engine(edit_while_ai_runs))['status'], 'ready')
        row.refresh_from_db()
        self.assertEqual(row.manual_json, manual)
        self.assertEqual(row.review_status, 'approved')
        self.assertFalse(row.is_published)
        self.assertEqual(row.review_note, 'keep')
        self.assertEqual(row.reviewed_evidence_hash, old_hash)
        self.assertNotEqual(row.evidence_hash, old_hash)
        self.assertFalse(service.get_analysis(row.pk)['review_matches_ai'])
        self.assertIsNone(project_player_observation(self.player))
        with self.assertRaises(service.AnalysisConflict):
            service.update_analysis(row.pk, {'revision': detail['revision'], 'review_status': 'approved'}, self.user)
        service.update_analysis(row.pk, {'revision': row.revision, 'review_note': 'note only'}, self.user)
        row.refresh_from_db()
        self.assertEqual(row.reviewed_evidence_hash, old_hash)
        self.assertFalse(service.get_analysis(row.pk)['review_matches_ai'])
        # Clearing manual restores the original freshness-bound AI projection.
        service.update_analysis(row.pk, {'revision': row.revision, 'manual': None, 'is_published': True}, self.user)
        view = project_player_observation(self.player)
        self.assertEqual(view['source_kind'], 'ai')
        self.assertEqual(view['status'], 'ready')
        self.assertTrue(view['observations'])
