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
        self.assertNotIn('conclusion', listing['items'][0])
        self.assertEqual(listing['tag_options'], ['历史标签'])
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

    def test_labels_and_real_season_on_search_detail_list_and_orphans(self):
        # A database PK is not a season ordinal, nor necessarily the active season.
        season = SeasonMeta.objects.create(pk=703, season_key='mn-s2-labels',
            season_name='Mythic+ Season 2', is_active=False, mplus_zone_id=1, raid_zone_id=2)
        self.player.season_id = season.pk
        self.player.save(update_fields=['season_id'])
        sibling = self.make_player(season_id=season.pk, spec_name='Discipline')
        detail = self.create()
        with CaptureQueriesContext(connection) as captured:
            characters = service.list_characters({'q': 'Member', 'season_id': str(season.pk)})
        self.assertEqual(len(captured), 2)
        self.assertEqual([p['id'] for p in characters['items']], [self.player.pk, sibling.pk])
        self.assertEqual([p['spec_label'] for p in characters['items']], ['神圣', '戒律'])
        self.assertTrue(all(p['class_label'] == '牧师' for p in characters['items']))
        listing = service.list_analyses({})
        for player in [detail['player'], *characters['items'], listing['items'][0]['player']]:
            self.assertEqual(player['season_id'], 703)
            self.assertEqual(player['season_name'], 'Mythic+ Season 2')
            self.assertEqual(player['class_name'], 'Priest')
        self.player.delete()
        for item in [service.get_analysis(detail['id']), service.list_analyses({})['items'][0]]:
            self.assertIsNone(item['player'])
            self.assertEqual(item['character']['season_id'], 703)
            self.assertEqual(item['character']['season_name'], 'Mythic+ Season 2')
        unknown = self.make_player(character_name='Unknown', class_name='Unknown', spec_name='Holy', season_id=9999)
        item = service.list_characters({'q': str(unknown.pk)})['items'][0]
        self.assertEqual(item['season_name'], '')
        self.assertEqual(item['spec_label'], 'Holy')  # Do not guess a class from a shared spec name.

    def test_effective_tag_options_filter_before_pagination_and_narrow_payload(self):
        rows = []
        for index, manual in enumerate(({}, {'tags': [{'label': '人工标签', 'value': '人工值',
                'evidence': [{'text': 'large-tag-evidence' * 1000}], 'reason': 'reason' * 1000}],
                'evidence': [{'text': 'large-manual-evidence' * 1000}]},
                {'conclusion': '人工无标签'}, {'tags': [], 'evidence': [{'text': 'only evidence'}]}, {})):
            activity = RaiderIOCharacterActivity.objects.create(season=self.season,
                region='eu', realm_slug='test-realm', character_name=f'Member{index}')
            rows.append(PlayerObservationAnalysis.objects.create(activity=activity, player=self.player,
                status='ready', review_status='approved', is_published=True, manual_json=manual,
                evidence_json={'eligible_labels': [{'code': 'selected', 'label': '冻结标签'},
                    {'code': 'unselected', 'label': '未采用标签'}],
                    'evidence': [{'text': 'large-frozen-evidence' * 1000}],
                    'conclusion_options': {'old': {'text': '不载入综合结论'}}},
                result_json={'observations': [{'code': 'selected', 'confidence': '低'}]}))
        expected_options = sorted(['冻结标签', '人工标签'])
        # Matching records are separated by manual overrides; count and pagination
        # must be computed across all records, not by filtering a fetched page.
        filters = {'q': 'Member', 'status': 'ready', 'review_status': 'approved', 'visibility': 'published', 'page_size': '1'}
        with CaptureQueriesContext(connection) as captured:
            first = service.list_analyses({**filters, 'tag': '冻结标签'})
        self.assertEqual(first['tag_options'], expected_options)
        self.assertEqual((first['total'], first['total_pages']), (2, 2))
        self.assertEqual([item['id'] for item in first['items']], [rows[4].pk])
        second = service.list_analyses({**filters, 'tag': '冻结标签', 'page': '2'})
        self.assertEqual([item['id'] for item in second['items']], [rows[0].pk])
        self.assertEqual(second['tag_options'], expected_options)
        manual = service.list_analyses({'source': 'manual'})
        self.assertEqual(manual['tag_options'], ['人工标签'])
        self.assertEqual([item['tags'] for item in manual['items']], [[], [], [{'label': '人工标签', 'value': '人工值'}]])
        self.assertTrue(all(item['source_kind'] == 'manual' for item in manual['items']))
        self.assertEqual(service.list_analyses({'source': 'ai'})['tag_options'], ['冻结标签'])
        self.assertEqual(service.list_analyses({'visibility': 'hidden'})['tag_options'], [])
        for tag in ('未知标签', '未采用标签', '冻结', '低', ' 冻结标签', '冻结标签 '):
            result = service.list_analyses({**filters, 'tag': tag})
            self.assertEqual(result['total'], 0, tag)
            self.assertEqual(result['items'], [])
            self.assertEqual(result['tag_options'], expected_options)
        for tag in ('x' * 201, None, 1):
            with self.assertRaises(ValidationError):
                service.list_analyses({'tag': tag})
        for item in [*first['items'], *manual['items']]:
            self.assertNotIn('conclusion', item)
            self.assertNotIn('evidence', item)
        for query in captured:
            sql = query['sql']
            for field in ('gear_json', 'talents_json', 'payload_json', 'conclusion_options'):
                self.assertNotIn(field, sql)
            for field in ('manual_json', 'evidence_json'):
                self.assertNotRegex(sql, rf'(?:SELECT\s+|,\s*)["`]wow_player_observation_analysis["`]\.["`]{field}["`](?:\s*,|\s+FROM)')
        # Reviewing still compares the complete small result, not a tag-only hash.
        approved = service.update_analysis(rows[0].pk, {'revision': 0, 'review_status': 'approved'}, self.user)
        self.assertTrue(approved['review_matches_ai'])
        self.assertTrue(service.list_analyses({'q': str(rows[0].pk), 'tag': '冻结标签'})['items'][-1]['review_matches_ai'])

    def test_tag_census_crosses_bounded_batches_with_exact_unicode_labels(self):
        activities = [RaiderIOCharacterActivity(season=self.season, region='eu', realm_slug='test-realm',
            character_name=f'batch{index}', identity_digest=service.activity_identity_digest('eu', 'test-realm', f'batch{index}'))
            for index in range(201)]
        RaiderIOCharacterActivity.objects.bulk_create(activities)
        rows = [PlayerObservationAnalysis(activity=activity, manual_json={
            'tags': [{'label': 'Case标签' if index == 0 else '常见标签', 'value': 'true' if index == 0 else '低'}]})
            for index, activity in enumerate(activities)]
        PlayerObservationAnalysis.objects.bulk_create(rows)
        with CaptureQueriesContext(connection) as captured:
            result = service.list_analyses({'tag': 'Case标签', 'page_size': '1'})
        self.assertEqual(len(captured), 3)  # Two bounded tag batches plus one page; no per-row identity queries.
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['id'], rows[0].pk)
        self.assertEqual(result['items'][0]['tags'], [{'label': 'Case标签', 'value': 'true'}])
        self.assertEqual(result['tag_options'], sorted(['Case标签', '常见标签']))
        self.assertEqual(service.list_analyses({'tag': 'case标签'})['total'], 0)
        self.assertEqual(service.list_analyses({'tag': 'Case标签', 'page': '2'})['items'], [])
        PlayerObservationAnalysis.objects.filter(pk=rows[0].pk).update(manual_json={},
            result_json={'metadata': {'source_kind': 'local_deterministic'}, 'observations': []})
        local = service.list_analyses({'source': 'local_deterministic'})
        self.assertEqual(local['total'], 1)
        self.assertEqual(local['tag_options'], [])
        self.assertEqual(local['items'][0]['source_kind'], 'local_deterministic')

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
                from botend.tests.test_player_observation import observation_response
                if before_return:
                    before_return()
                return json.dumps(observation_response(evidence))
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
