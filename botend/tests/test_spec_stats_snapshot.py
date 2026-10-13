"""验证分片读取量、数据契约、难度隔离和原子发布。"""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, RequestFactory

from botend.services import spec_stats_snapshot as snapshots
from botend.services.spec_overview_service import SpecOverviewService
from botend.controller.plugins.portal.SpecDetailAggregationMonitor import atomic_dump_json, DecimalEncoder
from botend.portal import spec_detail_views as views


def fixture(module, size=60000):
    def detail(identifier, difficulty=5):
        return {('boss_id' if module == 'raid' else 'dungeon_id'): identifier,
                ('boss_name' if module == 'raid' else 'dungeon_name'): f'测试范围 {identifier}',
                'sample_size': 20, 'difficulty': difficulty,
                'dps': {'avg': difficulty * 1000, 'max': 9000, 'median': 4500},
                'kill_time': {'avg_fmt': '05:00', 'median_fmt': '04:50'},
                'faction_distribution': {'0': {'pct': 100}},
                'talent_usage': [{'description': 'x' * size}],
                'gear_popularity': [{'id': 123, 'name': '测试装备'}],
                'last_updated': '2026-10-08T10:00:00Z'}
    if module == 'dungeon':
        return {'dungeons': [detail(i) for i in range(1, 9)], 'summary': detail('all')}
    difficulties = [{'difficulty': diff, 'label': '史诗' if diff == 5 else '英雄',
                     'zone_groups': [{'zone_id': 99, 'zone_cn': '测试团本',
                                      'bosses': [detail(i, diff) for i in range(1, 9)]}]}
                    for diff in (5, 4)]
    return {'zone_groups': difficulties[0]['zone_groups'], 'difficulties': difficulties}


class SpecStatsSnapshotTests(SimpleTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='stats-shards-'))
        self.directory = self.root / 'aggregated/77/Warrior/Arms'
        self.directory.mkdir(parents=True)
        self.override = self.settings(MEDIA_ROOT=self.root)
        self.override.enable()
        self.addCleanup(self.override.disable)
        cache.clear()
        self.addCleanup(cache.clear)
        self.season = SimpleNamespace(id=77, mplus_encounters=[{'id': i, 'name': f'副本 {i}'} for i in range(1, 9)],
                                      raid_encounters=[{'id': i} for i in range(1, 9)])

    @staticmethod
    def write(path, data):
        atomic_dump_json(path, data, cls=DecimalEncoder, ensure_ascii=False, allow_nan=False)

    def publish(self, module, data=None):
        data = data or fixture(module)
        path = self.directory / f'{module}.json'
        snapshots.publish_projection(path, data, self.write)
        return path, data

    def test_overview_reads_only_small_index_and_preserves_card_fields(self):
        for module in ('dungeon', 'raid'):
            path, data = self.publish(module)
            with patch.object(snapshots, '_load', wraps=snapshots._load) as reads:
                index = snapshots.read_projection(path)
            self.assertEqual([call.args[0] for call in reads.call_args_list], [path])
            self.assertLess(path.stat().st_size, len(json.dumps(data).encode()) / 20)
            self.assertNotIn('talent_usage', json.dumps(index))
            self.assertNotIn('gear_popularity', json.dumps(index))
            rows = index['dungeons'] if module == 'dungeon' else index['zone_groups'][0]['bosses']
            self.assertEqual(rows[0]['dps']['max'], 9000)
            self.assertEqual(rows[0]['kill_time']['avg_fmt'], '05:00')
            self.assertEqual(rows[0]['faction_distribution']['0']['pct'], 100)

    def test_every_detail_reads_exactly_one_shard_with_full_original_contract(self):
        for module in ('dungeon', 'raid'):
            path, original = self.publish(module)
            selections = [('dungeon-all', original['summary'])] + [
                (f'dungeon-{i}', original['dungeons'][i - 1]) for i in range(1, 9)] if module == 'dungeon' else [
                (f"raid-{item['difficulty']}-{boss['boss_id']}", boss)
                for item in original['difficulties'] for boss in item['zone_groups'][0]['bosses']]
            for key, expected in selections:
                with patch.object(snapshots, '_load', wraps=snapshots._load) as reads:
                    result = snapshots.read_projection(path, key)
                self.assertEqual(reads.call_count, 2)
                self.assertEqual(reads.call_args_list[1].args[0].name, f'{key}.json')
                if key == 'dungeon-all':
                    actual = result['summary']
                elif module == 'dungeon':
                    actual = result['dungeons'][0]
                else:
                    difficulty, bid = map(int, key.split('-')[1:])
                    actual = next(boss for item in result['difficulties'] if item['difficulty'] == difficulty
                                  for boss in item['zone_groups'][0]['bosses'] if boss['boss_id'] == bid)
                self.assertEqual(actual, expected)

    def test_failed_detail_or_index_write_keeps_previous_publication(self):
        path, original = self.publish('raid')
        before = path.read_bytes()
        for fail_name in ('raid-4-3.json', 'raid.json'):
            def failing_write(target, value):
                if target.name == fail_name:
                    raise OSError('模拟发布中断')
                self.write(target, value)
            with self.assertRaises(OSError):
                snapshots.publish_projection(path, fixture('raid', size=100), failing_write)
            self.assertEqual(path.read_bytes(), before)
            current = snapshots.read_projection(path, 'raid-4-3')
            self.assertEqual(current['difficulties'][1]['zone_groups'][0]['bosses'][2],
                             original['difficulties'][1]['zone_groups'][0]['bosses'][2])

    def test_reader_keeps_captured_generation_during_concurrent_publish(self):
        path, original = self.publish('dungeon')
        load = snapshots._load
        def switched_load(target):
            value = load(target)
            if target == path:
                self.publish('dungeon', fixture('dungeon', size=100))
            return value
        with patch.object(snapshots, '_load', side_effect=switched_load):
            result = snapshots.read_projection(path, 'dungeon-1')
        self.assertEqual(result['dungeons'][0], original['dungeons'][0])

    def test_missing_corrupt_wrong_scope_and_invalid_keys_never_fallback(self):
        path, _ = self.publish('dungeon')
        index = snapshots.read_projection(path)
        shard = path.parent / 'stats-details' / index['_snapshot']['generation'] / 'dungeon-1.json'
        envelope = json.loads(shard.read_text(encoding='utf-8'))
        for invalid in ('{', json.dumps({**envelope, 'scope': ['other', 'Mage', 'Fire']}),
                        json.dumps({**envelope, 'key': 'dungeon-2'})):
            shard.write_text(invalid, encoding='utf-8')
            self.assertEqual(snapshots.read_projection(path, 'dungeon-1')['dungeons'], [])
        for key in ('dungeon-999', '../../secret'):
            with patch.object(snapshots, '_load', wraps=snapshots._load) as reads:
                self.assertEqual(snapshots.read_projection(path, key)['dungeons'], [])
            self.assertEqual(reads.call_count, 1)
        index['_snapshot']['generation'] = '0' * 32
        self.write(path, index)
        self.assertEqual(snapshots.read_projection(path, 'dungeon-1')['dungeons'], [])
        index['_snapshot']['generation'] = '../../other'
        self.write(path, index)
        self.assertIsNone(snapshots.read_projection(path, 'dungeon-1'))

    def test_structurally_corrupt_indexes_return_none_without_loading_shards(self):
        for module in ('raid', 'dungeon'):
            path, _ = self.publish(module)
            original = json.loads(path.read_text(encoding='utf-8'))
            cases = []
            if module == 'raid':
                for value in (None, {}, [None], [{}], [{'difficulty': 5}]):
                    cases.append((('difficulties',), value))
                for field, values in (
                    (('difficulties', 0, 'difficulty'), (None, [], 3)),
                    (('difficulties', 0, 'zone_groups'), (None, {}, [None], [{}])),
                    (('difficulties', 0, 'zone_groups', 0, 'bosses'), (None, {}, [None], [{}])),
                    (('difficulties', 0, 'zone_groups', 0, 'bosses', 0, 'boss_id'), (None, [])),
                    (('zone_groups',), (None, {}, [None], [{}])),
                ):
                    cases.extend((field, value) for value in values)
                selected = 'raid-5-1'
            else:
                cases.extend((('dungeons',), value) for value in (None, {}, [None], [{}]))
                cases.append((('dungeons', 0, 'dungeon_id'), None))
                selected = 'dungeon-1'
            for keys, value in cases:
                broken = json.loads(json.dumps(original))
                target = broken
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = value
                self.write(path, broken)
                for selection in (None, selected):
                    with self.subTest(module=module, keys=keys, value=value, selection=selection), \
                            patch.object(snapshots, '_load', wraps=snapshots._load) as reads:
                        self.assertIsNone(snapshots.read_projection(path, selection))
                        self.assertEqual(reads.call_count, 1)
            broken = dict(original)
            del broken['difficulties' if module == 'raid' else 'dungeons']
            self.write(path, broken)
            self.assertIsNone(snapshots.read_projection(path, selected))

    def test_legacy_file_and_leaderboard_remain_compatible(self):
        for module in ('dungeon', 'raid', 'leaderboard'):
            data = {'players': [{'id': 1}]} if module == 'leaderboard' else fixture(module)
            path = self.directory / f'{module}.json'
            self.write(path, data)
            self.assertEqual(snapshots.read_projection(path, 'dungeon-1'), data)
            if module == 'leaderboard':
                snapshots.publish_projection(path, data, self.write)
                self.assertEqual(snapshots.read_projection(path), data)

    def test_public_views_preserve_selected_scope_and_overview_api_uses_only_index(self):
        self.publish('dungeon')
        self.publish('raid')
        factory = RequestFactory()
        with patch.object(views, '_base_context', return_value={'season': self.season}), \
                patch.object(views, 'render', side_effect=lambda req, tpl, ctx: dict(ctx)):
            summary = views.SpecDetailDungeonView().get(factory.get('/'), 'Warrior', 'Arms')
            self.assertIn('gear_popularity', summary['dungeon_detail'])
            single = views.SpecDetailDungeonView().get(factory.get('/?dungeon_id=3'), 'Warrior', 'Arms')
            self.assertEqual(single['dungeon_detail']['dungeon_id'], 3)
            for difficulty in (4, 5):
                result = views.SpecDetailRaidView().get(factory.get(f'/?boss_id=2&difficulty={difficulty}'), 'Warrior', 'Arms')
                self.assertEqual(result['boss_detail']['difficulty'], difficulty)
                self.assertEqual(result['boss_detail']['dps']['avg'], difficulty * 1000)
        opened = []
        original_open = Path.open
        def tracking_open(target, *args, **kwargs):
            opened.append(target)
            return original_open(target, *args, **kwargs)
        with patch.object(SpecOverviewService, '_latest_timestamp', side_effect=AssertionError('请求不得扫描详情时间')), \
                patch.object(views.SpecStatsService, 'get_active_season', return_value=self.season), \
                patch.object(Path, 'open', tracking_open):
            self.assertEqual(len(SpecOverviewService.mythic_plus('Warrior', 'Arms')['dungeons']), 8)
            self.assertEqual(len(SpecOverviewService.raid('Warrior', 'Arms')['difficulties']), 2)
        self.assertEqual({item.name for item in opened}, {'dungeon.json', 'raid.json'})
