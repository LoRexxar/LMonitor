"""Literal HTTP budgets for opt-in progressive discovery."""
from unittest.mock import patch
from django.test import TestCase
from botend.tests import test_player_observation_sources as source_fixtures


class ProgressiveSourcesTests(TestCase):
    def setUp(self):
        source_fixtures.ObservationSourcesTests.setUp(self)
        self.run['num_keystone_upgrades'] = 1
        self.activity.payload_json = {'mythic_plus_best_runs': [self.run], 'mythic_plus_recent_runs': [self.run]}
        self.activity.save()
        self.row.payload_json['num_chests'] = 1
        self.row.save()

    def transport(self, url, **kwargs):
        response = source_fixtures.ObservationSourcesTests.transport(self, url, **kwargs)
        if url.endswith('/run-details'):
            response.json.return_value['num_chests'] = 1
        return response

    refresh = source_fixtures.ObservationSourcesTests.refresh

    def test_unknown_basic_never_scans_scored_characters_even_force(self):
        result = self.refresh(progressive=True, force=True)
        self.assertEqual(self.calls, [])
        self.assertEqual(result['collection_mode'], 'progressive')
        self.assertEqual(result['basic_api_requests'], 0)
        self.assertEqual(result['followup_api_requests'], 0)
        self.assertFalse(result['followup_triggered'])

    def test_structure_alone_never_authorizes_followup(self):
        # 阵容初筛和资料缺口本身不再授权额外请求。
        with patch('botend.services.player_observation.build_evidence', return_value=self.four_evidence()):
            result = self.refresh(progressive=True, max_followup_requests=2)
        self.assertEqual(self.calls, [])
        self.assertEqual(result['followup_api_requests'], 0)
        self.assertFalse(result['followup_triggered'])

    def test_fixed_five_never_requests_scores(self):
        with patch('botend.services.player_observation.build_evidence', return_value={
                'metrics': {'team_pattern': {'kind': 'fixed_five'}}}):
            result = self.refresh(progressive=True)
        self.assertEqual(self.calls, [])
        self.assertFalse(result['followup_triggered'])

    def four_evidence(self):
        from botend.services.player_observation_sources import _characters
        from botend.raiderio_identity import activity_identity_digest
        chars, _ = _characters([self.row.payload_json], self.activity.identity_digest)
        return {'metrics': {'team_pattern': {'kind': 'suspected_four', 'core_size': 4,
            'core_members': [activity_identity_digest(**c['identity']) for c in chars[:4]]},
            'progression': {'status': 'insufficient', 'details': [{'run_id': 123, 'below_core': None}]}}}

    def test_all_fresh_needed_cache_is_zero_http_even_force(self):
        from botend.models import RaiderIOCharacterActivity
        from botend.services.player_observation_sources import _characters
        from botend.tests.test_player_observation_sources import scored
        from django.utils import timezone
        chars, _ = _characters([self.row.payload_json], self.activity.identity_digest)
        for char in chars:
            RaiderIOCharacterActivity.objects.update_or_create(season=self.season, **char['identity'],
                defaults={'scored_runs_json': scored(char['character_id']), 'scored_runs_fetched_at': timezone.now()})
        with patch('botend.services.player_observation.build_evidence', return_value=self.four_evidence()):
            result = self.refresh(progressive=True, force=True)
        self.assertEqual(result['api_requests'], 0)
        self.assertFalse(result['followup_triggered'])

    def test_no_question_does_not_contact_failing_source(self):
        # 没有具体问题时，即使远端会报错，也不应触发任何请求。
        self.http_status = 429
        with patch('botend.services.player_observation.build_evidence', return_value=self.four_evidence()):
            result = self.refresh(progressive=True)
        self.assertEqual(self.calls, [])
        self.assertEqual(result['followup_api_requests'], 0)

    def test_basic_budget_does_not_write_unattempted_errors(self):
        import copy
        from botend.models import RaiderIORunRoster
        runs = []
        for rid in range(123, 133):
            run = copy.deepcopy(self.run)
            run['url'] = run['url'].replace('/123-', f'/{rid}-')
            runs.append(run)
        self.activity.payload_json = {'mythic_plus_best_runs': runs, 'mythic_plus_recent_runs': runs}
        self.activity.save()
        self.row.delete()
        result = self.refresh(progressive=True)
        self.assertEqual(result['basic_api_requests'], 0)
        self.assertEqual(result['followup_api_requests'], 0)
        self.assertEqual(RaiderIORunRoster.objects.count(), 0)
        self.assertFalse(RaiderIORunRoster.objects.filter(status='error').exists())
        self.assertEqual(sum(s['status'] == 'not_needed' for s in result['runs']), 10)

    def test_force_preserves_privacy_leases_backoff_and_top20(self):
        from datetime import timedelta
        from django.utils import timezone
        from botend.models import PlayerSpecTopPlayer
        for status, visible in [('unavailable', False), ('fetching', True), ('error', True)]:
            self.row.status, self.row.roster_visible = status, visible
            self.row.next_attempt_at = timezone.now() + timedelta(hours=1)
            self.row.save()
            result = self.refresh(progressive=True, force=True)
            self.assertEqual(result['api_requests'], 0)
        PlayerSpecTopPlayer.objects.filter(pk=self.player.pk).update(rank=21)
        self.assertEqual(self.refresh(progressive=True, force=True)['status'], 'not_top20')
        self.assertEqual(self.calls, [])

    def test_scored_lease_backoff_no_force_bypass(self):
        from datetime import timedelta
        from django.utils import timezone
        from botend.models import RaiderIOCharacterActivity
        from botend.services.player_observation_sources import _characters
        chars, _ = _characters([self.row.payload_json], self.activity.identity_digest)
        for char in chars:
            if char['identity'] == {'region': self.activity.region, 'realm_slug': self.activity.realm_slug,
                                     'character_name': self.activity.character_name}:
                continue
            RaiderIOCharacterActivity.objects.update_or_create(season=self.season, **char['identity'],
                defaults={'status': 'fetching', 'next_attempt_at': timezone.now() + timedelta(hours=1)})
        with patch('botend.services.player_observation.build_evidence', return_value=self.four_evidence()):
            result = self.refresh(progressive=True, force=True)
        self.assertEqual(result['followup_api_requests'], 0)
        self.assertEqual(len(self.calls), 0)
        self.assertEqual(sum(s['status'] == 'busy' for s in result['characters']), 4)

    def test_due_activity_does_not_spend_unused_extra_budget(self):
        import copy
        from datetime import timedelta
        from django.utils import timezone
        evidence = self.four_evidence()
        runs = []
        for rid in range(123, 128):
            run = copy.deepcopy(self.run)
            run['url'] = run['url'].replace('/123-', f'/{rid}-')
            runs.append(run)
        self.activity.next_attempt_at = timezone.now() - timedelta(seconds=1)
        self.activity.save()
        self.row.delete()
        original = self.transport
        def transport(url, **kwargs):
            response = original(url, **kwargs)
            if url.endswith('/profile'):
                response.json.return_value.update(mythic_plus_best_runs=runs, mythic_plus_recent_runs=runs)
            return response
        before = type(self.player).objects.values().get(pk=self.player.pk)
        with patch('botend.services.player_observation.build_evidence', return_value=evidence), \
                patch.object(self, 'transport', side_effect=transport):
            result = self.refresh(progressive=True)
        self.assertEqual(result['basic_api_requests'], 1)
        self.assertEqual(result['followup_api_requests'], 0)
        self.assertEqual(result['api_requests'], 1)
        self.assertEqual(result['api_budget'], 8)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(type(self.player).objects.values().get(pk=self.player.pk), before)

    def test_missing_fifth_snapshot_alone_is_not_a_question(self):
        from botend.models import RaiderIOCharacterActivity
        from botend.services.player_observation_sources import _characters
        from botend.tests.test_player_observation_sources import scored
        from botend.raiderio_identity import activity_identity_digest
        from django.utils import timezone
        chars, _ = _characters([self.row.payload_json], self.activity.identity_digest)
        evidence = self.four_evidence()
        core = set(evidence['metrics']['team_pattern']['core_members'])
        evidence['metrics']['progression']['details'][0]['below_core'] = True
        fifth = next(c for c in chars if activity_identity_digest(**c['identity']) not in core)
        for char in chars:
            if char == fifth:
                continue
            RaiderIOCharacterActivity.objects.update_or_create(season=self.season, **char['identity'],
                defaults={'scored_runs_json': scored(char['character_id']), 'scored_runs_fetched_at': timezone.now()})
        with patch('botend.services.player_observation.build_evidence', return_value=evidence):
            result = self.refresh(progressive=True, force=True)
        self.assertEqual(result['followup_api_requests'], 0)
        self.assertEqual(self.calls, [])
        self.calls.clear()
        with patch('botend.services.player_observation.build_evidence', return_value=evidence):
            result = self.refresh(progressive=True, force=True)
        self.assertEqual(result['api_requests'], 0)
        self.assertEqual(self.calls, [])

    def test_uncached_recent_without_signal_is_left_unknown(self):
        import copy
        from django.utils import timezone
        from botend.models import RaiderIORunRoster
        from botend.services.player_observation import build_evidence
        from botend.services.player_observation_sources import _score_cache
        best = []
        for rid in range(123, 141):
            run = copy.deepcopy(self.run)
            run['url'] = run['url'].replace('/123-', f'/{rid}-')
            best.append(run)
            if rid == 123:
                continue
            fact = source_fixtures.roster(rid)
            fact['num_chests'] = 1
            # Excluded best records must not contribute additional member reads.
            if rid >= 131:
                for member in fact['roster'][:4]:
                    character = member['character']
                    name = f'Excluded{rid}-{character["id"]}'
                    character.update(id=rid * 100 + character['id'], name=name,
                                     path=f'/characters/eu/test-realm/{name}')
            RaiderIORunRoster.objects.create(rio_season=self.season.rio_season,
                run_id=rid, payload_json=fact, roster_visible=True, status='ready',
                fetched_at=timezone.now())
        recent = copy.deepcopy(self.run)
        recent['url'] = recent['url'].replace('/123-', '/999-')
        self.activity.payload_json = {'mythic_plus_best_runs': best,
                                      'mythic_plus_recent_runs': [recent]}
        self.activity.save()
        with patch('botend.services.player_observation_sources._score_cache', wraps=_score_cache) as cache:
            result = self.refresh(progressive=True)
        self.assertEqual(self.calls, [])
        self.assertFalse(RaiderIORunRoster.objects.filter(run_id=999).exists())
        selected = {row['run_id'] for row in result['runs']}
        self.assertEqual(selected, set(range(123, 131)) | {999})
        evidence = build_evidence(self.player)
        self.assertEqual(selected - {999}, {row['run_id'] for row in evidence['source_stamps']['rosters']})
        self.assertEqual(evidence['coverage']['unique_runs'], 9)
        self.assertEqual(evidence['coverage']['roster_runs'], result['rosters_visible'])
        self.assertEqual(cache.call_count, 5)
        self.assertEqual(result['characters_discovered'], 5)
        self.assertFalse(result['runs_truncated'])
        self.assertEqual(result['status'], 'ready')
        self.calls.clear()
        self.assertEqual(self.refresh(progressive=True)['api_requests'], 0)
        self.assertEqual(self.calls, [])

    def test_score_conflict_is_excluded_by_collection_and_evidence(self):
        import copy
        from botend.services.player_observation import build_evidence
        conflicting = copy.deepcopy(self.run)
        conflicting['score'] = self.run['score'] + 1
        self.activity.payload_json['mythic_plus_recent_runs'] = [conflicting]
        self.activity.save()
        result = self.refresh(progressive=True)
        self.assertEqual(result['runs'], [])
        self.assertEqual(result['invalid_references'], 1)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(build_evidence(self.player)['coverage']['unique_runs'], 0)
        self.assertEqual(self.calls, [])

    def test_identical_recent_duplicates_are_selected_once(self):
        from botend.services.player_observation import build_evidence
        self.activity.payload_json['mythic_plus_recent_runs'] = [self.run, self.run]
        self.activity.save()
        result = self.refresh(progressive=True)
        self.assertEqual([row['run_id'] for row in result['runs']], [123])
        self.assertEqual(result['rosters_visible'], build_evidence(self.player)['coverage']['roster_runs'])
        self.assertEqual(result['invalid_references'], 0)
        self.assertEqual(self.calls, [])

    def test_truncated_runs_or_characters_never_report_ready(self):
        import copy
        from django.utils import timezone
        from botend.models import RaiderIORunRoster
        run = copy.deepcopy(self.run)
        run['url'] = run['url'].replace('/123-', '/124-')
        fact = source_fixtures.roster(124)
        fact['num_chests'] = 1
        RaiderIORunRoster.objects.create(rio_season=self.season.rio_season,
            run_id=124, payload_json=fact, roster_visible=True, status='ready',
            fetched_at=timezone.now())
        self.activity.payload_json['mythic_plus_best_runs'].append(run)
        self.activity.save()
        for options, field in (({'max_runs': 1}, 'runs_truncated'),
                               ({'max_characters': 1}, 'characters_truncated')):
            with self.subTest(options=options):
                result = self.refresh(progressive=True, **options)
                self.assertEqual(result[field], field == 'characters_truncated')
                self.assertEqual(result['status'], 'partial' if field == 'characters_truncated' else 'ready')
                self.assertEqual(result['api_requests'], 0)

    def test_failed_roster_without_question_preserves_failure_without_retry(self):
        from datetime import timedelta
        from django.utils import timezone
        from botend.services.player_observation import build_evidence
        for status,error in (('error','http_504'),('error',''),('ready','http_504')):
            self.row.status,self.row.last_error=status,error
            self.row.next_attempt_at=timezone.now()-timedelta(seconds=1)
            self.row.save()
            before=type(self.row).objects.values().get(pk=self.row.pk)
            result=self.refresh(progressive=True,max_followup_requests=0)
            self.assertEqual(self.calls,[])
            self.assertEqual(result['runs'][0]['status'],'not_needed')
            self.assertEqual(type(self.row).objects.values().get(pk=self.row.pk),before)
            self.assertTrue(build_evidence(self.player)['source_failed'])

    def test_last_good_error_backoff_and_fetching_lease_are_unchanged(self):
        from datetime import timedelta
        from django.utils import timezone
        from botend.services.player_observation import build_evidence
        for status in ('error', 'fetching', 'ready'):
            with self.subTest(status=status):
                self.row.status, self.row.last_error = status, 'http_504'
                self.row.next_attempt_at = timezone.now() + timedelta(hours=1)
                self.row.save()
                before = type(self.row).objects.values().get(pk=self.row.pk)
                result = self.refresh(progressive=True, force=True, max_followup_requests=0)
                self.assertEqual(self.calls, [])
                self.assertEqual(result['api_requests'], 0)
                self.assertEqual(result['runs'][0]['status'], 'busy' if status == 'fetching' else 'backoff')
                self.assertEqual(type(self.row).objects.values().get(pk=self.row.pk), before)
                self.assertTrue(build_evidence(self.player)['source_failed'])

    def test_healthy_immutable_roster_is_zero_http_even_after_ttl(self):
        from datetime import timedelta
        from django.utils import timezone
        from botend.services.player_observation import build_evidence
        self.row.next_attempt_at = timezone.now() - timedelta(days=1)
        self.row.save()
        before = type(self.row).objects.values().get(pk=self.row.pk)
        result = self.refresh(progressive=True, force=True, max_followup_requests=0)
        self.assertEqual(self.calls, [])
        self.assertEqual(result['api_requests'], 0)
        self.assertEqual(result['runs'], [{'run_id': self.row.run_id, 'status': 'fresh'}])
        self.assertEqual(type(self.row).objects.values().get(pk=self.row.pk), before)
        self.assertFalse(build_evidence(self.player)['source_failed'])

    def test_unused_roster_budget_does_not_clear_existing_failures(self):
        import copy
        from datetime import timedelta
        from django.utils import timezone
        from botend.models import RaiderIORunRoster
        from botend.services.player_observation import build_evidence
        runs = []
        for rid in range(123, 129):
            run = copy.deepcopy(self.run)
            run['url'] = run['url'].replace('/123-', f'/{rid}-')
            runs.append(run)
            fact = source_fixtures.roster(rid)
            fact['num_chests'] = 1
            RaiderIORunRoster.objects.update_or_create(rio_season=self.season.rio_season, run_id=rid,
                defaults={'payload_json': fact, 'roster_visible': True, 'status': 'error',
                          'last_error': 'http_504', 'failure_count': 1, 'fetched_at': timezone.now(),
                          'next_attempt_at': timezone.now() - timedelta(seconds=1)})
        self.activity.payload_json = {'mythic_plus_best_runs': runs, 'mythic_plus_recent_runs': runs}
        self.activity.save()
        self.http_status = 504
        for budget in (0, 5):
            with self.subTest(budget=budget):
                before = {row['run_id']: row for row in RaiderIORunRoster.objects.values()}
                result = self.refresh(progressive=True, max_basic_roster_requests=budget, max_followup_requests=0)
                self.assertEqual(len(self.calls), 0)
                self.assertEqual(result['basic_api_requests'], 0)
                self.assertEqual(result['api_requests'], 0)
                self.assertLessEqual(result['api_requests'], result['api_budget'])
                self.assertEqual(sum(s['status'] == 'not_needed' for s in result['runs']), 6)
                self.assertEqual(sum(s['status'] == 'http_504' for s in result['runs']), 0)
                self.assertTrue(build_evidence(self.player)['source_failed'])
                attempted = {call[1]['params']['id'] for call in self.calls}
                for row in RaiderIORunRoster.objects.values():
                    self.assertEqual(row['last_error'], 'http_504')
                    self.assertEqual(row['status'], 'error')
                    self.assertEqual(row['payload_json'], before[row['run_id']]['payload_json'])
                    self.assertEqual(row['fetched_at'], before[row['run_id']]['fetched_at'])
                    if row['run_id'] not in attempted:
                        self.assertEqual(row, before[row['run_id']])

    def test_followup_cap_rejected_not_expanded(self):
        for value in (3, True, -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.refresh(progressive=True, max_followup_requests=value)
