# -*- coding: utf-8 -*-
"""
聚合统计命令
从数据库计算各专精的副本/团本/人物榜统计，输出 JSON 文件供 Portal 直接读取。

用法:
  python manage.py aggregate_spec_stats           # 全量聚合
  python manage.py aggregate_spec_stats --class DeathKnight --spec Blood  # 单专精
  python manage.py aggregate_spec_stats --season 2  # 指定赛季
"""

import json
import os
import time
from decimal import Decimal
from django.core.management.base import BaseCommand

from botend.models import SeasonMeta
from botend.constants.wow import CLASS_SPEC_MAP, DUNGEON_CN, RAID_BOSS_CN, RAID_ZONE_CN
from botend.services.spec_stats_service import (
    SpecStatsService,
    _lookup_dungeon_cn,
)


class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


class Command(BaseCommand):
    help = '聚合各专精统计数据为 JSON 文件'

    def add_arguments(self, parser):
        parser.add_argument('--class', dest='class_name', help='职业名 (如 DeathKnight)')
        parser.add_argument('--spec', dest='spec_name', help='专精名 (如 Blood)')
        parser.add_argument('--season', type=int, dest='season_id', help='赛季 ID')

    def handle(self, *args, **options):
        season_id = options.get('season_id')
        target_class = options.get('class_name')
        target_spec = options.get('spec_name')

        season = SeasonMeta.objects.filter(id=season_id).first() if season_id else SeasonMeta.objects.filter(is_active=True).first()
        if not season:
            self.stderr.write('未找到活跃赛季')
            return

        season_id = season.id
        self.stdout.write(f'聚合赛季 {season.season_key} (id={season_id})')

        base_dir = os.path.join('media', 'aggregated', str(season_id))
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

                # 2. 团本统计
                self._aggregate_raid(season, class_name, spec_name, spec_dir)

                # 3. 人物榜
                self._aggregate_leaderboard(class_name, spec_name, spec_dir)

                total_files += 3
                self.stdout.write(f'  {class_name}/{spec_name} ✓')

        elapsed = time.time() - t0
        self.stdout.write(self.style.SUCCESS(f'完成: {total_files} 个文件, {elapsed:.1f}s'))

    def _aggregate_dungeon(self, season, class_name, spec_name, spec_dir):
        """聚合副本统计，每个副本 full=True（含 top5、种族分布）"""
        if not season.mplus_encounters:
            return

        dungeons = []
        for enc in season.mplus_encounters:
            cn_name = _lookup_dungeon_cn(enc['name'])
            stats = SpecStatsService._compute_dungeon_stats(
                season.id, enc['id'], cn_name, class_name, spec_name, full=True
            )
            dungeons.append(stats)

        path = os.path.join(spec_dir, 'dungeon.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'dungeons': dungeons}, f, cls=DecimalEncoder, ensure_ascii=False)

    def _aggregate_raid(self, season, class_name, spec_name, spec_dir):
        """聚合团本统计，按区域分组，每个 boss full=True（含 top5）"""
        if not season.raid_encounters:
            return

        # 按区域分组
        if season.raid_zones:
            zone_groups = []
            for rz in season.raid_zones:
                zone_cn = RAID_ZONE_CN.get(rz.get('name', ''), rz.get('name', ''))
                zone_bosses = []
                for enc in rz.get('encounters', []):
                    cn_name = RAID_BOSS_CN.get(enc['name'], enc['name'])
                    stats = SpecStatsService._compute_raid_stats(
                        season.id, enc['id'], cn_name, class_name, spec_name, full=True
                    )
                    # 附加区域信息
                    stats['raid_zone_id'] = rz.get('id')
                    stats['raid_zone_name'] = rz.get('name', '')
                    stats['raid_zone_cn'] = zone_cn
                    zone_bosses.append(stats)
                if zone_bosses:
                    zone_groups.append({
                        'zone_id': rz.get('id'),
                        'zone_name': rz.get('name', ''),
                        'zone_cn': zone_cn,
                        'bosses': zone_bosses,
                    })
        else:
            # fallback: 扁平列表
            bosses = []
            for enc in season.raid_encounters:
                cn_name = RAID_BOSS_CN.get(enc['name'], enc['name'])
                stats = SpecStatsService._compute_raid_stats(
                    season.id, enc['id'], cn_name, class_name, spec_name, full=True
                )
                bosses.append(stats)
            zone_groups = [{'zone_id': 0, 'zone_name': '', 'zone_cn': '', 'bosses': bosses}]

        path = os.path.join(spec_dir, 'raid.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'zone_groups': zone_groups}, f, cls=DecimalEncoder, ensure_ascii=False)

    def _aggregate_leaderboard(self, class_name, spec_name, spec_dir):
        """聚合人物榜，仅输出页面展示用 Top 20。"""
        result = SpecStatsService.get_player_list(
            class_name, spec_name, page=1, page_size=20
        )

        path = os.path.join(spec_dir, 'leaderboard.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(result, f, cls=DecimalEncoder, ensure_ascii=False, default=str)
