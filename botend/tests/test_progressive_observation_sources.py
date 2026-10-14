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

    def test_four_core_followup_is_two_real_requests_not_two_members_times_runs(self):
        from botend.services.player_observation_sources import _characters
        chars, _ = _characters([self.row.payload_json], self.activity.identity_digest)
        from botend.raiderio_identity import activity_identity_digest
        core = [activity_identity_digest(**c['identity']) for c in chars[:4]]
        evidence = {'metrics': {'team_pattern': {'kind': 'suspected_four', 'core_size': 4,
                     'core_members': core}, 'progression': {'status': 'insufficient',
                     'details': [{'run_id': 123, 'below_core': None}]}}}
        with patch('botend.services.player_observation.build_evidence', return_value=evidence):
            result = self.refresh(progressive=True, max_followup_requests=2)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(result['followup_api_requests'], 2)
        self.assertEqual(result['basic_api_requests'], 0)
        self.assertTrue(result['followup_triggered'])
        self.assertTrue(all(c[0].endswith('/mythic-plus-scored-runs') for c in self.calls))

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

    def test_429_and_timeout_consume_literal_followup_budget(self):
        import requests
        for kind in ('429', 'timeout', '500'):
            self.calls.clear()
            self.http_status = int(kind) if kind != 'timeout' else 200
            original = self.transport
            def transport(url, **kwargs):
                response = original(url, **kwargs)
                if kind == 'timeout':
                    raise requests.Timeout()
                return response
            with patch('botend.services.player_observation.build_evidence', return_value=self.four_evidence()), \
                    patch.object(self, 'transport', side_effect=transport):
                result = self.refresh(progressive=True)
            expected = 1 if kind == '429' else 2
            self.assertEqual(result['followup_api_requests'], expected)
            self.assertEqual(len(self.calls), expected)
            self.assertEqual(result['status'], 'rate_limited' if kind == '429' else 'partial')

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
        self.assertEqual(result['basic_api_requests'], 5)
        self.assertEqual(result['followup_api_requests'], 0)
        self.assertEqual(RaiderIORunRoster.objects.count(), 5)
        self.assertFalse(RaiderIORunRoster.objects.filter(status='error').exists())
        self.assertEqual(sum(s['status'] == 'budget_exhausted' for s in result['runs']), 5)

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
        self.assertEqual(result['followup_api_requests'], 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(sum(s['status'] == 'busy' for s in result['characters']), 4)

    def test_total_budget_includes_activity_five_rosters_and_two_scores(self):
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
        self.assertEqual(result['basic_api_requests'], 6)
        self.assertEqual(result['followup_api_requests'], 2)
        self.assertEqual(result['api_requests'], 8)
        self.assertEqual(result['api_budget'], 8)
        self.assertEqual(len(self.calls), 8)
        self.assertEqual(type(self.player).objects.values().get(pk=self.player.pk), before)

    def test_fresh_fifth_cache_not_refetched_and_missing_fifth_preferred(self):
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
        self.assertEqual(result['followup_api_requests'], 1)
        self.assertEqual(self.calls[0][1]['params']['characterId'], fifth['character_id'])
        self.calls.clear()
        with patch('botend.services.player_observation.build_evidence', return_value=evidence):
            result = self.refresh(progressive=True, force=True)
        self.assertEqual(result['api_requests'], 0)
        self.assertEqual(self.calls, [])

    def test_cached_old_best_cannot_starve_uncached_recent_canonical_sample(self):
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
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.calls[0][0].endswith('/run-details'))
        self.assertEqual(self.calls[0][1]['params']['id'], 999)
        self.assertTrue(RaiderIORunRoster.objects.get(run_id=999).roster_visible)
        selected = {row['run_id'] for row in result['runs']}
        self.assertEqual(selected, set(range(123, 131)) | {999})
        evidence = build_evidence(self.player)
        self.assertEqual(selected, {row['run_id'] for row in evidence['source_stamps']['rosters']})
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
                self.assertTrue(result[field])
                self.assertEqual(result['status'], 'partial')
                self.assertEqual(result['api_requests'], 0)

    def test_followup_cap_rejected_not_expanded(self):
        for value in (3, True, -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.refresh(progressive=True, max_followup_requests=value)
