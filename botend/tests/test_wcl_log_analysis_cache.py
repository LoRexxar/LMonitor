"""排名与日志独立：真实数据库、文件检查点和增量计算结果对照。"""
from collections import defaultdict
from copy import deepcopy
from datetime import timedelta
import time
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from botend.models import SeasonMeta, SpecRaidRanking, WclCombatantSnapshot, WowTalentVersion, WowTalentNodeMetadata
from botend.tests.aggregation_cache_helpers import isolate_work_cache
from botend.constants.hero_talents import spec_hero_subtree_ids
from botend.services import raid_aggregation as raid, wcl_combatant_snapshot as combatants
from botend.services import wcl_log_analysis_cache as analysis
from botend.services import spec_aggregation_cache as work
from botend.services.spec_stats_service import SpecStatsService


class LogAnalysisCacheTests(TestCase):
    def setUp(self):
        self.root = isolate_work_cache(self)
        self.season = SeasonMeta.objects.create(season_key='日志独立缓存', season_name='测试赛季',
                                               mplus_zone_id=1, raid_zone_id=2)
        self.version, _ = WowTalentVersion.objects.update_or_create(key='retail', defaults={
            'is_active': True, 'status': 'active', 'is_default_stats': True})
        hero = min(spec_hero_subtree_ids('Warrior', 'Arms'))
        self.nodes = []
        for entry, tree, subtree in ((77, 'spec', 0), (78, 'class', 0), (79, 'hero', hero)):
            self.nodes.append(WowTalentNodeMetadata.objects.create(talent_version=self.version,
                class_name='Warrior', spec_name='Arms', node_id=entry, talent_id=entry,
                spell_id=entry * 10, tree_type=tree, db2_subtree_id=subtree,
                source='db2_backfill', name='测试天赋', name_zh='测试天赋',
                row=1, column=1, max_points=1))
        self.scope = (self.season.pk, 1, '测试首领', 'Warrior', 'Arms', 5)
        self.slot = 'log-analysis:raid:Warrior:Arms:5:1'
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('测试禁止访问上游')))

    def row(self, index, *, fact=True):
        row = SpecRaidRanking.objects.create(season_id=self.season.pk, boss_id=1, boss_name='测试首领',
            difficulty=5, class_name='Warrior', spec_name='Arms', character_name=f'玩家{index}',
            realm='Realm', region='eu', report_code=f'Report{index}', fight_id=1, dps=1000-index)
        if fact:
            self.fact(row)
        return row

    def fact(self, row):
        return WclCombatantSnapshot.objects.create(report_code=row.report_code, fight_id=1, actor_id=1,
            payload_json={'sourceID': 1, 'source': {'id': 1, 'name': row.character_name, 'server': 'Realm'},
                'specID': 71, 'critMelee': 100, 'talentTree': [{'id': node.node_id, 'rank': 1} for node in self.nodes],
                'gear': [{'id': 123, 'slot': 0, 'itemLevel': 200}]})

    def inputs(self):
        return raid._inputs(self.season.pk, 1, 'Warrior', 'Arms', 5, None, None)

    def full_result(self):
        rows, facts = self.inputs()
        enriched = combatants.enrich_dungeon_records(rows, 'Warrior', 'Arms', facts=facts)
        return work._normalized(SpecStatsService._compute_raid_records(rows, *self.scope[:3],
            'Warrior', 'Arms', full=True, enriched_records=enriched))

    def test_ranking_changes_reuse_all_logs_and_five_replacements_parse_only_five(self):
        originals = [self.row(i) for i in range(100)]
        timing = defaultdict(float)
        first = raid.raid_stats(*self.scope, phase_timings=timing)[0]
        self.assertEqual(timing['log_analysis_parsed'], 100)
        cache_path = work.work_path(self.season.pk, self.slot)
        before = cache_path.read_bytes()
        for i, row in enumerate(originals):
            row.dps = 2000+i
            row.last_updated = timezone.now()+timedelta(hours=1)
        SpecRaidRanking.objects.bulk_update(originals, ['dps', 'last_updated'])
        rows, _ = self.inputs()
        fetcher = Mock(_wcl_last_error='')
        counts = combatants.collect_dungeon_combatants(rows, fetcher, apply=True)
        self.assertEqual(counts['cached'], 100)
        fetcher.fetch_wcl_combatant_info.assert_not_called()
        timing.clear()
        with patch.object(analysis, 'prepare_log_talents', wraps=analysis.prepare_log_talents) as parse:
            changed = raid.raid_stats(*self.scope, phase_timings=timing)[0]
        parse.assert_not_called()
        self.assertEqual(timing['gear_statistics_reused'], 1)
        self.assertEqual(timing['log_analysis_reused'], 100)
        self.assertEqual(cache_path.read_bytes(), before)
        self.assertNotEqual(first['dps'], changed['dps'])
        self.assertEqual(changed, self.full_result())
        newcomers = [self.row(100+i, fact=False) for i in range(5)]
        SpecRaidRanking.objects.filter(pk__in=[r.pk for r in newcomers]).update(dps=3000)
        rows, _ = self.inputs()
        counts = combatants.collect_dungeon_combatants(rows, apply=False)
        self.assertEqual((counts['cached'], counts['pending']), (95, 5))
        fetcher = Mock(_wcl_last_error='request_failed')
        fetcher.fetch_wcl_combatant_info.return_value = None
        with patch.object(combatants, 'load_staged_events', return_value=None):
            combatants.collect_dungeon_combatants(rows, fetcher, apply=True)
        self.assertEqual({call.args[0] for call in fetcher.fetch_wcl_combatant_info.call_args_list},
                         {row.report_code for row in newcomers})
        for row in newcomers:
            self.fact(row)
        timing.clear()
        with patch.object(analysis, 'prepare_log_talents', wraps=analysis.prepare_log_talents) as parse:
            changed = raid.raid_stats(*self.scope, phase_timings=timing)[0]
        self.assertEqual(sum(len(call.args[0]) for call in parse.call_args_list), 5)
        self.assertEqual((timing['log_analysis_reused'], timing['log_analysis_parsed']), (95, 5))
        self.assertEqual(changed, self.full_result())
        stored = work.load_work(self.season.pk, self.slot, analysis.SCHEMA)
        self.assertEqual(len(stored['entries']), 100)
        self.assertEqual(sum(bool(node.get('_log_resolved')) for node in stored['nodes']), 3)
        self.assertTrue(all(set(entry['log']).issubset(analysis.LOG_FIELDS) for entry in stored['entries'].values()))
        print('排名日志分离验收', {'排名变化解析数': 0, '替换后复用': 95, '替换后解析': 5,
              '检查点压缩字节': cache_path.stat().st_size})

    def test_fact_change_metadata_change_corruption_and_callers_are_isolated(self):
        for i in range(3):
            self.row(i)
        rows, facts = self.inputs()
        def run(source=facts, selected=rows):
            return analysis.prepare_cached_logs(selected, 'Warrior', 'Arms', season_id=self.season.pk,
                scope='raid:Warrior:Arms:5:1', facts=source)
        _, initial, _ = run()
        initial[0]['talents_json'][0]['name'] = '调用方修改'
        with patch.object(analysis, 'prepare_log_talents', wraps=analysis.prepare_log_talents) as parse:
            _, again, _ = run(selected=[dict(row, id=row['id']+100) for row in rows])
        parse.assert_not_called()
        self.assertNotEqual(again[0]['talents_json'][0]['name'], '调用方修改')
        changed = deepcopy(facts)
        changed[next(iter(changed))][0]['critMelee'] = 500
        with patch.object(analysis, 'prepare_log_talents', wraps=analysis.prepare_log_talents) as parse:
            run(source=changed)
        self.assertEqual(sum(len(call.args[0]) for call in parse.call_args_list), 1)
        WowTalentNodeMetadata.objects.filter(pk=self.nodes[0].pk).update(name_zh='修正名称')
        with patch.object(analysis, 'prepare_log_talents', wraps=analysis.prepare_log_talents) as parse:
            _, refreshed, _ = run(source=changed)
        self.assertEqual(len(parse.call_args.args[0]), 3)
        self.assertIn('修正名称', {n['name'] for n in refreshed[0]['talents_json']})
        work.work_path(self.season.pk, self.slot).write_bytes('损坏检查点'.encode())
        with patch.object(analysis, 'prepare_log_talents', wraps=analysis.prepare_log_talents) as parse:
            _, restored, _ = run(source=changed)
        self.assertEqual(len(parse.call_args.args[0]), 3)
        self.assertEqual(restored, refreshed)

    def test_missing_fact_can_arrive_later_and_duplicate_logs_keep_independent_rankings(self):
        row = self.row(1, fact=False)
        rows, facts = self.inputs()
        def run(selected, source):
            return analysis.prepare_cached_logs(selected, 'Warrior', 'Arms', season_id=self.season.pk,
                scope='raid:Warrior:Arms:5:1', facts=source)
        _, missing, _ = run(rows, facts)
        self.assertEqual(missing[0]['_log_talent_status'], 'missing')
        self.fact(row)
        rows, facts = self.inputs()
        duplicate = dict(rows[0], id=999, dps=5000)
        with patch.object(analysis, 'prepare_log_talents', wraps=analysis.prepare_log_talents) as parse:
            _, result, _ = run([rows[0], duplicate], facts)
        self.assertEqual(len(parse.call_args.args[0]), 1)
        self.assertEqual([r['dps'] for r in result], [rows[0]['dps'], 5000])
        self.assertEqual([r['id'] for r in result], [row.pk, 999])
        result[0]['talents_json'][0]['name'] = '修改第一个样本'
        self.assertNotEqual(result[1]['talents_json'][0]['name'], '修改第一个样本')
        with patch.object(analysis, 'prepare_log_talents', wraps=analysis.prepare_log_talents) as parse:
            _, warm, _ = run([duplicate, rows[0]], facts)
        parse.assert_not_called()
        self.assertEqual(warm[0]['_log_talent_status'], 'ok')
        self.assertEqual(run([], facts)[:2], ([], []))
        stored = work.load_work(self.season.pk, self.slot, analysis.SCHEMA)
        self.assertFalse(stored['entries'])
        self.assertFalse(stored['nodes'])

    def test_stored_identity_key_does_not_hash_log_body_or_ranking(self):
        from botend.services import wcl_combatant_store as store
        row = self.row(1)
        raw = WclCombatantSnapshot.objects.get(report_code=row.report_code).payload_json
        store.persist_events(row.report_code, {1: [raw], 2: [raw]})
        records, facts = self.inputs()
        event = facts[(row.report_code, 1)][0]
        with patch.object(analysis, 'content_digest', wraps=analysis.content_digest) as digest:
            key = analysis.observation_key(records[0], event)
        self.assertEqual(digest.call_args.args[0], ['stored', (row.report_code, 1, 1), {}])
        changed = dict(records[0], dps=9999, id=9999, gear_json=[{'很大': 'x'*100000}],
                       talents_json=[{'很大': 'y'*100000}])
        self.assertEqual(key, analysis.observation_key(changed, event))
        other = store.load_facts([dict(report_code=row.report_code, fight_id=2)])[(row.report_code, 2)][0]
        self.assertNotEqual(key, analysis.observation_key(changed, other))
        incomplete = store.StoredCombatant(dict(event, gear=[]))
        incomplete.cache_identity = event.cache_identity
        self.assertNotEqual(analysis.observation_key(records[0], incomplete),
                            analysis.observation_key(changed, incomplete))

    def test_rank_only_update_skips_log_parsing_and_gear_statistics(self):
        from botend.services import wcl_combatant_store as store
        from botend.services import spec_stats_service as stats
        for i in range(10):
            row = self.row(i)
            raw = WclCombatantSnapshot.objects.get(report_code=row.report_code).payload_json
            store.persist_events(row.report_code, {1: [raw]})
        before = raid.raid_stats(*self.scope)[0]
        SpecRaidRanking.objects.filter(character_name='玩家9').update(dps=9999)
        timing = defaultdict(float)
        with patch.object(analysis, 'prepare_log_talents', side_effect=AssertionError('排名变化不应解析日志')), \
             patch.object(stats, '_compute_gear_popularity', side_effect=AssertionError('排名变化不应重算装备')):
            after = raid.raid_stats(*self.scope, phase_timings=timing)[0]
        self.assertEqual(timing['gear_statistics_reused'], 1)
        self.assertNotEqual(before['dps'], after['dps'])
        self.assertEqual(after['top5'][0]['character_name'], '玩家9')
        self.assertEqual(after, self.full_result())

    def test_raw_checkpoint_preserves_integrity_and_size_limit_without_display_codec(self):
        payload = {'layout': 2, 'entries': {}, 'nodes': [{'description': '重复说明'*2000}]}
        with patch('botend.services.stats_storage_codec.pack_detail', side_effect=AssertionError('不应执行展示编码')), \
             patch.object(work.os, 'fsync', side_effect=AssertionError('派生缓存不应强制刷盘')):
            work.save_work(self.season.pk, self.slot, analysis.SCHEMA, payload,
                           raw_json=True, strict=True, durable=False)
        self.assertEqual(work.load_work(self.season.pk, self.slot, analysis.SCHEMA), payload)
        with self.settings(SPEC_AGGREGATION_WORK_MAX_BYTES=1024):
            with self.assertRaises(work.WorkCacheTooLarge):
                work.save_work(self.season.pk, self.slot, analysis.SCHEMA, payload, raw_json=True, strict=True)
        self.assertEqual(work.load_work(self.season.pk, self.slot, analysis.SCHEMA), payload)
        import gzip
        path = work.work_path(self.season.pk, self.slot)
        raw = gzip.decompress(path.read_bytes()).replace('重复说明'.encode(), '篡改说明'.encode(), 1)
        path.write_bytes(gzip.compress(raw))
        self.assertIsNone(work.load_work(self.season.pk, self.slot, analysis.SCHEMA))

    def test_full_size_talent_checkpoint_cost_and_output(self):
        """记录完整天赋输入下的实际文件成本，不用不稳定的计时阈值判断正确性。"""
        hero = min(spec_hero_subtree_ids('Warrior', 'Arms'))
        for i in range(57):
            self.nodes.append(WowTalentNodeMetadata.objects.create(talent_version=self.version,
                class_name='Warrior', spec_name='Arms', node_id=100+i, talent_id=100+i,
                spell_id=10000+i, tree_type='hero' if i >= 45 else 'spec',
                db2_subtree_id=hero if i >= 45 else 0, source='db2_backfill',
                name=f'完整天赋{i}', name_zh=f'完整天赋{i}', max_points=2,
                description_zh='测试完整天赋说明。'*40))
        for i in range(100):
            self.row(i)
        rows, facts = self.inputs()
        for i, events in enumerate(facts.values()):
            for j in range(7):
                events[0]['talentTree'][3+j]['rank'] = 1+((i >> j) & 1)
        def full(source):
            enriched = combatants.enrich_dungeon_records(rows, 'Warrior', 'Arms', facts=source)
            return analysis.prepare_log_talents(enriched, 'Warrior', 'Arms')
        def cached(source):
            return analysis.prepare_cached_logs(rows, 'Warrior', 'Arms', season_id=self.season.pk,
                scope='raid:Warrior:Arms:5:1', facts=source)[1]
        start = time.perf_counter()
        expected = full(facts)
        baseline = time.perf_counter()-start
        start = time.perf_counter()
        self.assertEqual(cached(facts), expected)
        cold = time.perf_counter()-start
        start = time.perf_counter()
        self.assertEqual(cached(facts), expected)
        warm = time.perf_counter()-start
        for events in list(facts.values())[:5]:
            events[0]['critMelee'] = 321
        expected = full(facts)
        start = time.perf_counter()
        self.assertEqual(cached(facts), expected)
        delta = time.perf_counter()-start
        print('完整天赋检查点基准', {'全量秒': round(baseline, 4), '首次检查点秒': round(cold, 4),
            '排名更新秒': round(warm, 4), '五份变化秒': round(delta, 4),
            '压缩字节': work.work_path(self.season.pk, self.slot).stat().st_size})
