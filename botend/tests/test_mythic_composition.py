"""阵容查询的职责、同场身份与分页回归。"""
from copy import deepcopy
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from botend.models import MythicCompositionJob, SeasonMeta
from botend.services import mythic_composition_jobs as jobs

from botend.services.mythic_composition import candidates, matches, selected_specs, wcl_link


class CompositionTests(SimpleTestCase):
    def test_positions_wildcards_and_two_identical_damage_specs(self):
        required = selected_specs([0, 0, 62, 0, 62])
        self.assertTrue(matches([65, 62, 104, 261, 62], required))
        self.assertFalse(matches([65, 62, 104, 261, 63], required))
        self.assertFalse(matches([65, 62, 65, 261, 62], required))
        for slots in ([0], [62, 0, 0, 0, 0], [0, False, 0, 0, 0], [0, 0, 9999, 0, 0]):
            with self.assertRaises(ValueError):
                selected_specs(slots)

    def test_search_validates_upstream_candidates_and_preserves_score_order(self):
        required = selected_specs([104, 0, 62, 62, 0])
        run = dict(season='season-mn-2', numChests=1, hasAutoRoute=True,
                   members=[{'specId': i} for i in [104, 65, 62, 62, 261]])
        source = Mock()
        source.rio.return_value = {'matches': [
            {'data': dict(run, id=12)}, {'data': dict(run, id=16, season='season-mn-1')},
            {'data': dict(run, id=15, hasAutoRoute=False)}, {'data': dict(run, id=4)}]}
        self.assertEqual(candidates(source, 'season-mn-2', required, 0)['ids'], [12, 15, 4])
        self.assertNotIn('hasAutoRoute[0][eq]', source.rio.call_args.kwargs)
        page = candidates(source, 'season-mn-2', required, 0, complete=True)
        self.assertEqual(page, {'ids': [12, 4], 'next': None})
        params = source.rio.call_args.kwargs
        self.assertEqual(params['sort[score]'], 'desc')
        self.assertEqual([params[f'memberSpecIds[{i}][eq]'] for i in range(3)], [104, 62, 62])

    def test_wcl_requires_same_roster_level_duration_and_time_window(self):
        roster = [dict(character=dict(name=f'玩家{i}', realm=dict(slug='realm'), region=dict(slug='eu')))
                  for i in range(5)]
        detail = dict(roster=roster, mythic_level=18, clear_time_ms=100000,
                      completed_at='2026-01-01T00:00:00Z')
        row = dict(startTime=1767225500000, duration=100000, bracketData=18,
                   report=dict(code='Abcd1234', fightID=3),
                   allCharacters=[dict(name=m['character']['name'],
                                       canonical=dict(server=dict(region='EU', slug='realm'))) for m in roster])
        client = Mock()
        with (patch('botend.services.mythic_composition.SpecDetailBase', return_value=client),
              patch('botend.services.mythic_composition.token_provider', return_value=client)):
            for change in ({'duration': 99999}, {'bracketData': 17}, {'startTime': 1767100000000},
                           {'allCharacters': row['allCharacters'][:-1]}, {}):
                record = deepcopy(dict(row, **change))
                client._wcl_graphql.return_value = {'characterData': {'p0': {'encounterRankings': {'ranks': [record]}}}}
                expected = None if change else 'https://www.warcraftlogs.com/reports/Abcd1234#fight=3'
                self.assertEqual(wcl_link(detail, 1234), expected)


class CompositionJobTests(TestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(season_key='test-mn', season_name='测试赛季',
            raid_zone_id=0, mplus_zone_id=55, rio_season='test-season', is_active=True)
        self.slots = [0] * 5
        self.owner = get_user_model().objects.create_superuser(username="阵容测试")

    def test_account_slot_and_single_dispatch_after_commit(self):
        with patch.object(jobs, 'dispatch') as spawn:
            with self.captureOnCommitCallbacks(execute=True):
                first, created = jobs.enqueue(self.owner, 'test-season', self.slots, False)
                self.assertTrue(created)
                other, created = jobs.enqueue(self.owner, 'test-season', [104, 0, 0, 0, 0], True)
                self.assertFalse(created)
                self.assertEqual(first.pk, other.pk)
                spawn.assert_not_called()
            spawn.assert_called_once_with(first.pk)

    def test_interrupted_worker_releases_slot_and_cannot_reclaim_job(self):
        old, _ = jobs.enqueue(self.owner, 'test-season', self.slots, False)
        MythicCompositionJob.objects.filter(pk=old.pk).update(updated_at=timezone.now()-2*jobs.LEASE)
        fresh, created = jobs.enqueue(self.owner, 'test-season', self.slots, False)
        self.assertTrue(created)
        old.refresh_from_db()
        self.assertEqual(old.state, 'failed')
        jobs.collect(old.pk)
        self.assertNotEqual(old.pk, fresh.pk)
        self.assertEqual(MythicCompositionJob.objects.get(active_slot=1), fresh)

    def test_worker_saves_five_matches_and_status_survives_new_submission(self):
        self.client.force_login(self.owner)
        url = '/dashboard/mythic-composition/'
        job, _ = jobs.enqueue(self.owner, 'test-season', self.slots, True)
        self.assertNotIn('results', self.client.get(url, {'view': 'status'}).json()['job'])
        with (patch.object(jobs, 'RouteSource'), patch.object(jobs, 'candidates', return_value={
                'ids': [1, 1, 2, 3, 4, 5, 6, 7], 'next': None}),
              patch.object(jobs, 'resolve_run', side_effect=lambda s, season, req, i:
                    dict(id=i, route=['map','key'], wcl='url' if i > 1 else None, wcl_status='')),
              patch.object(jobs, 'export_route', return_value='路线') as export):
            jobs.collect(job.pk)
            jobs.collect(job.pk)
        self.assertEqual(export.call_count, 5)
        self.assertEqual(self.client.post(url, dict(action='submit', slots=self.slots),
                                         content_type='application/json').status_code, 202)
        history = self.client.get(url, {'view': 'status', 'job': job.pk}).json()['job']
        self.assertEqual((history['state'], history['found'], history['checked']), ('done',5,6))
        self.assertEqual([r['id'] for r in history['results']], list(range(2,7)))

    def test_failure_releases_slot_and_preserves_partial_results(self):
        job, _ = jobs.enqueue(self.owner, 'test-season', self.slots, False)
        with (patch.object(jobs, 'RouteSource'), patch.object(jobs, 'candidates',
              return_value={'ids': [1, 2], 'next': None}), patch.object(jobs, 'resolve_run',
              side_effect=[dict(id=1, route=None, wcl=None), RuntimeError('上游中断')]),
              self.assertLogs(jobs.logger, level='ERROR')):
            jobs.collect(job.pk)
        job.refresh_from_db()
        self.assertIsNone(job.active_slot)
        self.assertEqual((job.state, job.message, job.results[0]['id']), ('failed', '上游中断', 1))

    def test_accounts_can_run_independently_and_only_manage_their_own_jobs(self):
        url = '/dashboard/mythic-composition/'
        body = dict(action='submit', slots=self.slots)
        second = get_user_model().objects.create_superuser(username='另一个账号')
        self.client.force_login(self.owner)
        first = self.client.post(url, body, content_type='application/json').json()['job']['id']
        MythicCompositionJob.objects.filter(pk=first).update(state='running')
        self.client.force_login(second)
        self.assertIsNone(self.client.get(url, {'view': 'status'}).json()['job'])
        self.assertEqual(self.client.get(url, {'view': 'status', 'job': first}).status_code, 404)
        self.assertEqual(self.client.post(url, dict(action='stop', id=first),
                                         content_type='application/json').status_code, 404)
        self.assertEqual(MythicCompositionJob.objects.get(pk=first).state, 'running')
        response = self.client.post(url, body, content_type='application/json')
        self.assertEqual(response.status_code, 202)
        other = response.json()['job']['id']
        self.assertNotEqual(first, other)
        self.assertEqual(MythicCompositionJob.objects.filter(active_slot=1).count(), 2)
        self.assertEqual(list(self.client.get(url).context['jobs'].values_list('pk', flat=True)), [other])
        self.client.force_login(self.owner)
        repeat = self.client.post(url, body, content_type='application/json').json()
        self.assertFalse(repeat['created'])
        self.assertEqual(repeat['job']['id'], first)
