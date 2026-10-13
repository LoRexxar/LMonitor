"""Manual replay of a completed WCL cohort, or explicit pre-cycle compatibility."""
import os
import time
from functools import partial

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from botend.models import MonitorTask, SeasonMeta, WclCollectionCycle
from botend.constants.wow import CLASS_SPEC_MAP
from botend.controller.plugins.portal.SpecDetailAggregationMonitor import (
    SpecDetailAggregationMonitor, publish_stats,
)
from botend.plugin_sync import claim_monitor_task, release_monitor_task_lease
from botend.services.wcl_collection_cycle import (
    CycleOwnershipLost, active_cycle, begin_aggregation, defer_cycle, finish_publication,
    frozen_season,
)


class Command(BaseCommand):
    help = '聚合已完成 WCL 采集周期；无周期旧数据须显式 --allow-legacy'

    def add_arguments(self, parser):
        parser.add_argument('--class', dest='class_name', help='职业名 (如 DeathKnight)')
        parser.add_argument('--spec', dest='spec_name', help='专精名 (如 Blood)')
        parser.add_argument('--season', type=int, dest='season_id', help='赛季 ID')
        parser.add_argument('--dungeon-only', action='store_true', help='仅刷新大秘境缓存')
        parser.add_argument('--allow-legacy', action='store_true',
                            help='仅当赛季尚无任何采集周期时，显式允许旧原始数据预热')

    def handle(self, *args, **options):
        season_id = options.get('season_id')
        season = (SeasonMeta.objects.filter(id=season_id).first() if season_id else
                  SeasonMeta.objects.filter(is_active=True).first())
        if not season:
            raise CommandError('未找到赛季')
        if active_cycle(season.pk):
            raise CommandError('WCL 采集/发布周期尚未完成；请由正常调度续跑，不可手工绕过')
        has_cycle = WclCollectionCycle.objects.filter(season=season).exists()
        if not has_cycle and not options.get('allow_legacy'):
            raise CommandError('无 WCL 采集周期；旧数据兼容预热须显式 --allow-legacy')
        task = cycle = None
        if has_cycle:
            configured = MonitorTask.objects.filter(name='SpecDetailAggregationMonitor').first()
            if configured is None:
                raise CommandError('未配置聚合 MonitorTask，无法取得统一发布租约')
            task = claim_monitor_task(configured.pk)
            if task is None:
                raise CommandError('聚合任务运行中，未取得发布租约')
        try:
            if has_cycle:
                cycle = begin_aggregation(season, task, replay_published=True)
                if cycle is None:
                    raise CommandError('采集周期状态已变化，取消手工发布')
                season = frozen_season(cycle, season)
            publish = partial(publish_stats, cycle=cycle) if cycle else partial(
                publish_stats, legacy_season=season)
            base_dir = os.path.join(getattr(settings, 'MEDIA_ROOT', '') or 'media',
                                    'aggregated', str(season.pk))
            total_files, t0 = 0, time.time()
            self.stdout.write(f'聚合赛季 {season.season_key} (id={season.pk})')
            for class_name, specs in CLASS_SPEC_MAP.items():
                if options.get('class_name') and class_name != options['class_name']:
                    continue
                for spec_name in specs:
                    if options.get('spec_name') and spec_name != options['spec_name']:
                        continue
                    spec_dir = os.path.join(base_dir, class_name, spec_name)
                    self._aggregate_dungeon(season, class_name, spec_name, spec_dir,
                                            cycle=cycle, publish=publish)
                    if not options.get('dungeon_only'):
                        self._aggregate_raid(season, class_name, spec_name, spec_dir, publish=publish)
                        self._aggregate_leaderboard(season.pk, class_name, spec_name, spec_dir)
                    total_files += 1 if options.get('dungeon_only') else 3
                    self.stdout.write(f'  {class_name}/{spec_name} ✓')
            if cycle:
                finish_publication(cycle)
            self.stdout.write(self.style.SUCCESS(f'完成: {total_files} 个文件, {time.time() - t0:.1f}s'))
        except CycleOwnershipLost as exc:
            raise CommandError(str(exc)) from exc
        except Exception as exc:
            if cycle:
                defer_cycle(cycle, str(exc))
            raise
        finally:
            if task:
                release_monitor_task_lease(task.pk, task._monitor_task_lease_owner)

    _aggregate_dungeon = staticmethod(SpecDetailAggregationMonitor._aggregate_dungeon)
    _aggregate_raid = staticmethod(SpecDetailAggregationMonitor._aggregate_raid)
    _aggregate_leaderboard = staticmethod(SpecDetailAggregationMonitor._aggregate_leaderboard)
