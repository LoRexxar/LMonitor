"""增量采集、私有快照、失败恢复及团本入口边界回归。"""

import tempfile
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings

from botend.services import raid_burst_aggregation as agg
from botend.services.raid_burst_events import EventFetchDeferred, cache_path, fetch_events
from botend.services.raid_burst_snapshot import (
    digest, load_published, private_root, read_blob, read_json, read_manifest, write_json,
)


class EventClient:
    """小型真实结构响应；不连接 WCL，也不读取业务数据库。"""
    def __init__(self):
        self.calls = []
        self.failure = None
        self.pi = False
        self.no_lust = False
        self.paginate = False
        self._wcl_last_error = 'budget_wait'
        self._wcl_retry_at = '2026-10-21T00:00:00+00:00'

    def _wcl_graphql(self, query, variables):
        self.calls.append(dict(variables))
        aliases = [key[5:] for key in variables if key.startswith('kind_')]
        if aliases:
            report = {}
            for field in aliases:
                single = {**variables, 'kind': variables['kind_' + field],
                          'start': variables['start_' + field]}
                response = self._response(single)
                if response is None:
                    return None
                report[field] = response['reportData']['report']['page']
            return {'reportData': {'report': report}}
        return self._response(variables)

    def _response(self, variables):
        kind = variables.get('kind')
        if self.failure and self.failure(variables):
            return None
        fight_id = variables['ids'][0]
        if kind is None:
            result = {'fights': [{'id': fight_id, 'encounterID': 3470, 'difficulty': 5, 'kill': True,
                                  'startTime': 10000, 'endTime': 310000,
                                  'phaseTransitions': [{'id': 1, 'startTime': 10000}]}],
                      'masterData': {'actors': [{'id': 7, 'name': '测试玩家', 'server': '测试服', 'subType': 'Warrior'}]}}
        else:
            base = {'fight': fight_id, 'sourceID': 7, 'targetID': 7}
            if kind == 'CombatantInfo':
                data = [dict(base, specID=71, auras=[])]
            elif kind == 'Buffs':
                data = [] if self.no_lust else [
                    dict(base, type='applybuff', timestamp=11000, abilityGameID=2825),
                    dict(base, type='removebuff', timestamp=51000, abilityGameID=2825)]
                if self.pi:
                    data.append(dict(base, type='refreshbuff', timestamp=20000, abilityGameID=10060))
            else:
                data = [dict(base, type='cast', timestamp=t, abilityGameID=107574)
                        for t in (12000, 132000, 252000) if t >= variables['start']]
            next_page = None
            if kind == 'Casts' and self.paginate and variables['start'] == 10000:
                data, next_page = data[:1], 130000
            result = {'page': {'data': data, 'nextPageTimestamp': next_page}}
        return {'reportData': {'report': result}}


class RaidBurstAggregationTests(TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='raid-burst-tests-'))
        override = override_settings(RAID_BURST_SNAPSHOT_ROOT=self.root)
        override.enable()
        self.addCleanup(override.disable)
        self.row = dict(report_code='ABC123', fight_id=1, character_name='测试玩家', realm='测试服',
                        region='CN', class_name='Warrior', spec_name='Arms', boss_id=3470, difficulty=5, dps=123456)
        self.scope = dict(season_id=1, class_name='Warrior', spec_name='Arms', boss_id=3470,
                          difficulty=5, key='1/Warrior/Arms/5/3470', label='测试团本')
        self.config = agg.SUPPORTED[('Warrior', 'Arms')]
        self.client = EventClient()
        self.directory = self.root / '1/Warrior/Arms'

    def build(self, rows=None):
        return agg.build_scope(rows if rows is not None else [self.row], self.scope, self.config,
                               self.root, nullcontext, self.client)

    def publish(self, descriptor):
        manifest = {'schema': 1, 'scopes': [descriptor]}
        write_json(self.directory / 'index.json', {'data': manifest, 'digest': digest(manifest)})

    def test_identical_data_reuses_snapshot_without_fetch_or_regroup(self):
        descriptor = self.build()
        self.assertEqual(len(self.client.calls), 2)
        self.publish(descriptor)
        path = self.directory / 'snapshots' / f"{descriptor['blob']}.json"
        before = path.stat().st_mtime_ns
        self.client.calls.clear()
        with patch.object(agg, 'build_groups', side_effect=AssertionError('旧数据重算')):
            self.assertEqual(self.build(), descriptor)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_new_log_only_fetches_new_identity_and_dps_change_uses_raw_cache(self):
        self.publish(self.build())
        self.client.calls.clear()
        self.build([self.row, dict(self.row, region='EU', report_code='NEW456', fight_id=2, dps=90000)])
        self.assertEqual(len(self.client.calls), 2)
        self.assertTrue(all(c['code'] == 'NEW456' for c in self.client.calls))
        self.client.calls.clear()
        descriptor = self.build([dict(self.row, dps=777)])
        self.assertEqual(self.client.calls, [])
        self.assertEqual(read_blob(self.directory, descriptor['blob'])['samples'][0]['dps'], 777)

    def test_parser_change_uses_complete_raw_and_duplicates_do_not_inflate(self):
        self.build()
        self.client.calls.clear()
        with patch.object(agg, 'ANALYSIS_VERSION', 999):
            descriptor = self.build([self.row, self.row])
        self.assertEqual(self.client.calls, [])
        self.assertEqual(read_blob(self.directory, descriptor['blob'])['attempted'], 1)

    def test_budget_failure_resumes_at_next_page_and_preserves_published_result(self):
        descriptor = self.build()
        self.publish(descriptor)
        before = (self.directory / 'index.json').read_bytes()
        self.client.paginate = True
        self.client.failure = lambda v: v.get('kind') == 'Casts' and v.get('start') == 130000
        new = dict(self.row, region='EU', report_code='NEW456')
        with self.assertRaises(EventFetchDeferred) as caught:
            self.build([self.row, new])
        self.assertEqual(caught.exception.retry_at, self.client._wcl_retry_at)
        self.assertEqual((self.directory / 'index.json').read_bytes(), before)
        self.client.calls.clear()
        self.client.failure = None
        descriptor = self.build([self.row, new])
        self.assertEqual(self.client.calls[0]['start_casts'], 130000)
        self.assertTrue(all(call.get('kind_casts') for call in self.client.calls))
        payload = read_blob(self.directory, descriptor['blob'])
        self.assertEqual([len(s['times']['107574']) for s in payload['samples']], [3, 3])

    def test_pi_and_no_lust_exclusions_are_cached(self):
        for field, counter in [('pi', 'excluded_pi'), ('no_lust', 'excluded_no_lust')]:
            with self.subTest(field=field):
                self.client.pi = field == 'pi'
                self.client.no_lust = field == 'no_lust'
                row = dict(self.row, report_code=field)
                descriptor = self.build([row])
                payload = read_blob(self.directory, descriptor['blob'])
                self.assertEqual(payload[counter], 1)
                self.assertEqual(payload['samples'], [])
                self.client.calls.clear()
                with patch.object(agg, 'ANALYSIS_VERSION', 2):
                    self.build([row])
                self.assertEqual(self.client.calls, [])

    def test_rejected_candidate_skips_cast_continuation_and_reuses_evidence(self):
        for reason in ('pi', 'no_lust'):
            with self.subTest(reason=reason):
                self.client.paginate = True
                self.client.pi = reason == 'pi'
                self.client.no_lust = reason == 'no_lust'
                self.client.calls.clear()
                row = dict(self.row, report_code='early-' + reason)
                descriptor = self.build([row])
                self.assertEqual(len(self.client.calls), 2)
                self.assertEqual(self.client.calls[1]['start_casts'], 10000)
                self.assertEqual(read_blob(self.directory, descriptor['blob'])['excluded_' + reason], 1)
                state = read_json(cache_path(self.root, row))['data']
                self.assertFalse(state['streams']['casts']['complete'])
                self.assertEqual(state['streams']['casts']['cursor'], 130000)
                self.client.calls.clear()
                with patch.object(agg, 'ANALYSIS_VERSION', 999):
                    self.build([row])
                self.assertEqual(self.client.calls, [])

    def test_eligibility_cannot_accept_partial_buffs_or_publish_partial_casts(self):
        raw = fetch_events(self.root, self.row, self.client, nullcontext)
        raw['streams']['buffs']['complete'] = False
        with self.assertRaises(ValueError):
            agg.eligibility(raw, self.row, self.config)
        raw['streams']['buffs']['complete'] = True
        raw['streams']['casts']['complete'] = False
        with self.assertRaises(ValueError):
            agg.normalize(raw, self.row, 1, self.config)

    def test_combined_empty_pages_follow_each_cursor_and_do_not_repeat_complete_alias(self):
        original = self.client._wcl_graphql
        first = True
        def pages(query, variables):
            nonlocal first
            response = original(query, variables)
            if first and 'kind_casts' in variables:
                first = False
                for field in ('casts', 'buffs'):
                    response['reportData']['report'][field] = {'data': [], 'nextPageTimestamp': 11000}
            return response
        self.client._wcl_graphql = pages
        descriptor = self.build()
        self.assertEqual(len(self.client.calls), 4)
        self.assertEqual(set(k for k in self.client.calls[2] if k.startswith('kind_')), {'kind_buffs'})
        self.assertEqual(set(k for k in self.client.calls[3] if k.startswith('kind_')), {'kind_casts'})
        self.assertEqual(self.client.calls[3]['start_casts'], 11000)
        payload = read_blob(self.directory, descriptor['blob'])
        self.assertEqual(len(payload['samples'][0]['times']['107574']), 3)

    def test_malformed_alias_does_not_advance_any_event_stream(self):
        original = self.client._wcl_graphql
        def malformed(query, variables):
            response = original(query, variables)
            if 'kind_casts' in variables:
                del response['reportData']['report']['combatants']['nextPageTimestamp']
            return response
        self.client._wcl_graphql = malformed
        with self.assertRaises(EventFetchDeferred):
            self.build()
        self.assertEqual(read_json(cache_path(self.root, self.row))['data']['streams'], {})
        self.client._wcl_graphql = original
        self.client.calls.clear()
        self.build()
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.client.calls[0]['start_casts'], 10000)

    def test_corrupt_cache_repaired_when_rebuilding_and_old_snapshot_read_only(self):
        descriptor = self.build()
        self.publish(descriptor)
        cache_path(self.root, self.row).write_text('{', encoding='utf8')
        self.client.calls.clear()
        with patch.object(agg, 'ANALYSIS_VERSION', 2):
            self.build()
        self.assertEqual(len(self.client.calls), 2)
        with patch.object(agg, 'build_scope', side_effect=AssertionError('访问重算')):
            self.assertEqual(load_published(self.scope['key'])[0]['samples'][0]['player']['name'], '测试玩家')

    def test_failed_atomic_replace_keeps_old_file(self):
        self.publish(self.build())
        path = self.directory / 'index.json'
        before = path.read_bytes()
        with patch('botend.services.raid_burst_snapshot.os.replace', side_effect=OSError('磁盘失败')):
            with self.assertRaises(OSError):
                write_json(path, {'data': '不完整'})
        self.assertEqual(path.read_bytes(), before)
        self.assertIsNotNone(read_manifest(self.directory))

    def test_invalid_current_manifest_falls_back_and_query_cannot_read_arbitrary_file(self):
        self.publish(self.build())
        write_json(self.directory / 'previous.json', read_json(self.directory / 'index.json'))
        (self.directory / 'index.json').write_text('{}', encoding='utf8')
        self.assertIsNotNone(load_published(self.scope['key'])[0])
        self.assertIsNone(load_published('../../events')[0])

    def test_public_directory_rejected(self):
        with override_settings(MEDIA_ROOT=self.root, RAID_BURST_SNAPSHOT_ROOT=self.root / 'private'):
            with self.assertRaises(ValueError):
                private_root()

    def test_cached_log_cannot_be_reused_for_another_boss_or_difficulty(self):
        self.build()
        self.client.calls.clear()
        for mismatch in ({'boss_id': 3471}, {'difficulty': 4}):
            with self.subTest(mismatch=mismatch):
                raw = fetch_events(self.root, dict(self.row, **mismatch), self.client, nullcontext)
                self.assertIn('invalid', raw)
        self.assertEqual(self.client.calls, [])

    def test_daily_aggregation_keeps_each_boss_and_difficulty_separate(self):
        season = SimpleNamespace(id=1, raid_encounters=[
            {'id': 3470, 'name': '首领甲'}, {'id': 3471, 'name': '首领乙'}], raid_zones=[])
        def query(**filters):
            return Mock(values=Mock(return_value=[dict(self.row, boss_id=filters['boss_id'],
                                                       difficulty=filters['difficulty'])]))
        def build(rows, scope, *args):
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['boss_id'], scope['boss_id'])
            self.assertEqual(rows[0]['difficulty'], scope['difficulty'])
            return dict(scope, blob='0' * 64)
        with patch('botend.models.SpecRaidRanking.objects.filter', side_effect=query) as queries, \
                patch.object(agg, 'build_scope', side_effect=build) as builder, \
                patch('botend.controller.plugins.portal.SpecDetailBase.SpecDetailBase'):
            agg.aggregate_raid_burst(season, 'Warrior', 'Arms', guard=nullcontext)
        self.assertEqual(builder.call_count, 4)
        self.assertEqual({(call.kwargs['boss_id'], call.kwargs['difficulty']) for call in queries.call_args_list},
                         {(3470, 5), (3470, 4), (3471, 5), (3471, 4)})

    def test_lost_owner_cannot_write_events_or_publish(self):
        def lost():
            raise RuntimeError('发布所有权丢失')
        with self.assertRaises(RuntimeError):
            fetch_events(self.root, self.row, self.client, lost)
        self.assertEqual(self.client.calls, [])
        self.assertFalse(cache_path(self.root, self.row).exists())

    def test_groups_bounded_by_total_width_not_adjacent_distance(self):
        rows = [dict(rank=i, lust_group='opener', lust_focus={'107574': [t]},
                     times={'107574': [t, t + 120]}) for i, t in enumerate((0, 4, 8), 1)]
        groups = agg.build_groups(rows, ['107574'])['opener']['107574']
        self.assertEqual([w['ranks'] for w in groups['windows']], [[1, 2], [3]])
        self.assertEqual([p['ranks'] for p in groups['plans']], [[1, 2], [3]])
        self.assertTrue(all(w['end'] - w['start'] <= 5 for w in groups['windows']))

    def test_raid_entry_does_not_build_private_statistics_and_other_specs_skip(self):
        from botend.controller.plugins.portal.SpecDetailAggregationMonitor import SpecDetailAggregationMonitor
        season = SimpleNamespace(id=1, raid_encounters=[{'id': 3470, 'name': '测试首领'}], raid_zones=[])
        with patch('botend.services.raid_aggregation.raid_stats', return_value=({}, '范围指纹', False)), \
                patch.object(agg, 'aggregate_raid_burst') as build:
            SpecDetailAggregationMonitor._aggregate_raid(
                season, 'Warrior', 'Arms', str(self.root), publish=Mock())
        build.assert_not_called()
        with patch('botend.models.SpecRaidRanking.objects.filter', side_effect=AssertionError('不支持的职业查询')):
            self.assertIsNone(agg.aggregate_raid_burst(season, 'Unknown', 'Unknown', guard=nullcontext))

    def test_complete_raid_run_reuses_index_and_empty_cohort_removes_old_samples(self):
        season = SimpleNamespace(id=1, raid_encounters=[{'id': 3470, 'name': '测试首领'}], raid_zones=[])
        rows = [self.row]
        def query(**kwargs):
            return Mock(values=Mock(return_value=rows if kwargs['difficulty'] == 5 else []))
        with patch('botend.models.SpecRaidRanking.objects.filter', side_effect=query), \
                patch('botend.controller.plugins.portal.SpecDetailBase.SpecDetailBase', return_value=self.client):
            agg.aggregate_raid_burst(season, 'Warrior', 'Arms', guard=nullcontext)
            before = (self.directory / 'index.json').stat().st_mtime_ns
            self.client.calls.clear()
            agg.aggregate_raid_burst(season, 'Warrior', 'Arms', guard=nullcontext)
            self.assertEqual(self.client.calls, [])
            self.assertEqual((self.directory / 'index.json').stat().st_mtime_ns, before)
            rows.clear()
            agg.aggregate_raid_burst(season, 'Warrior', 'Arms', guard=nullcontext)
        payload, _ = load_published('1/Warrior/Arms/5/3470')
        self.assertEqual(payload['samples'], [])

    def test_daily_task_ignores_burst_budget_failure(self):
        from botend.controller.plugins.portal.SpecDetailAggregationMonitor import SpecDetailAggregationMonitor
        module = 'botend.controller.plugins.portal.SpecDetailAggregationMonitor'
        season = SimpleNamespace(id=1, raid_encounters=[{'id': 3470, 'name': '测试首领'}], raid_zones=[])
        cycle = SimpleNamespace(collected_at='2026-10-20T00:00:00+00:00')
        with patch(f'{module}.SeasonMeta.objects.filter') as seasons, \
                patch(f'{module}.CLASS_SPEC_MAP', {'Warrior': ['Arms']}), \
                patch(f'{module}.begin_aggregation', return_value=cycle), \
                patch(f'{module}.frozen_season', return_value=season), \
                patch(f'{module}.publish_stats'), \
                patch(f'{module}.finish_publication') as finish, \
                patch(f'{module}.defer_cycle') as defer, \
                patch.object(SpecDetailAggregationMonitor, '_aggregate_leaderboard'), \
                patch.object(SpecDetailAggregationMonitor, '_aggregate_dungeon'), \
                patch('botend.services.raid_aggregation.raid_stats', return_value=({}, '范围指纹', False)), \
                patch.object(agg, 'aggregate_raid_burst') as build:
            seasons.return_value.first.return_value = season
            self.assertTrue(SpecDetailAggregationMonitor(None, None).scan(''))
            build.assert_not_called()
            finish.assert_called_once_with(cycle)
            finish.reset_mock()
            build.side_effect = EventFetchDeferred('budget_wait', self.client._wcl_retry_at)
            self.assertTrue(SpecDetailAggregationMonitor(None, None).scan(''))
            finish.assert_called_once_with(cycle)
            build.assert_not_called()
            defer.assert_not_called()

    def test_dungeon_only_manual_command_never_calls_raid_or_burst(self):
        from botend.management.commands.aggregate_spec_stats import Command
        module = 'botend.management.commands.aggregate_spec_stats'
        season = SimpleNamespace(pk=1, id=1, season_key='测试赛季')
        with patch(f'{module}.SeasonMeta.objects.filter') as seasons, \
                patch(f'{module}.active_cycle', return_value=None), \
                patch(f'{module}.WclCollectionCycle.objects.filter') as cycles, \
                patch(f'{module}.CLASS_SPEC_MAP', {'Warrior': ['Arms']}), \
                patch.object(Command, '_aggregate_dungeon') as dungeon, \
                patch.object(Command, '_aggregate_raid') as raid, \
                patch.object(Command, '_aggregate_leaderboard') as leaderboard, \
                patch.object(agg, 'aggregate_raid_burst') as burst:
            seasons.return_value.first.return_value = season
            cycles.return_value.exists.return_value = False
            Command().handle(allow_legacy=True, dungeon_only=True)
            dungeon.assert_called_once()
            raid.assert_not_called()
            leaderboard.assert_not_called()
            burst.assert_not_called()
