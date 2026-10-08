# -*- coding: utf-8 -*-
"""
聚合统计命令
从数据库计算各专精的副本/团本/人物榜统计，输出 JSON 文件供 Portal 直接读取。

用法:
  python manage.py aggregate_spec_stats           # 全量聚合
  python manage.py aggregate_spec_stats --class DeathKnight --spec Blood  # 单专精
  python manage.py aggregate_spec_stats --season 2  # 指定赛季
"""

import os
import time
from django.conf import settings
from django.core.management.base import BaseCommand

from botend.models import SeasonMeta
from botend.constants.wow import CLASS_SPEC_MAP
from botend.controller.plugins.portal.SpecDetailAggregationMonitor import SpecDetailAggregationMonitor


class Command(BaseCommand):
    help = '聚合各专精统计数据为 JSON 文件'

    def add_arguments(self, parser):
        parser.add_argument('--class', dest='class_name', help='职业名 (如 DeathKnight)')
        parser.add_argument('--spec', dest='spec_name', help='专精名 (如 Blood)')
        parser.add_argument('--season', type=int, dest='season_id', help='赛季 ID')
        parser.add_argument('--dungeon-only', action='store_true', help='仅刷新该赛季大秘境缓存，不写团本或人物榜')

    def handle(self, *args, **options):
        season_id = options.get('season_id')
        target_class = options.get('class_name')
        target_spec = options.get('spec_name')
        dungeon_only = bool(options.get('dungeon_only'))

        season = SeasonMeta.objects.filter(id=season_id).first() if season_id else SeasonMeta.objects.filter(is_active=True).first()
        if not season:
            self.stderr.write('未找到活跃赛季')
            return

        season_id = season.id
        self.stdout.write(f'聚合赛季 {season.season_key} (id={season_id})')

        base_dir = os.path.join(getattr(settings, 'MEDIA_ROOT', '') or 'media', 'aggregated', str(season_id))
        total_files = 0
        t0 = time.time()

        for class_name, specs in CLASS_SPEC_MAP.items():
            if target_class and class_name != target_class:
                continue
            for spec_name in specs:
                if target_spec and spec_name != target_spec:
                    continue

                spec_dir = os.path.join(base_dir, class_name, spec_name)
                os.makedirs(spec_dir, exist_ok=True)

                # 1. 副本统计
                self._aggregate_dungeon(season, class_name, spec_name, spec_dir)
                if not dungeon_only:
                    # 2. 团本统计
                    self._aggregate_raid(season, class_name, spec_name, spec_dir)
                    # 3. 人物榜
                    self._aggregate_leaderboard(season.id, class_name, spec_name, spec_dir)

                total_files += 1 if dungeon_only else 3
                self.stdout.write(f'  {class_name}/{spec_name} ✓')

        elapsed = time.time() - t0
        self.stdout.write(self.style.SUCCESS(f'完成: {total_files} 个文件, {elapsed:.1f}s'))

    # 定时任务和手工预热使用同一生成器，避免难度与展示字段不同步。
    _aggregate_dungeon = staticmethod(SpecDetailAggregationMonitor._aggregate_dungeon)
    _aggregate_raid = staticmethod(SpecDetailAggregationMonitor._aggregate_raid)
    _aggregate_leaderboard = staticmethod(SpecDetailAggregationMonitor._aggregate_leaderboard)
