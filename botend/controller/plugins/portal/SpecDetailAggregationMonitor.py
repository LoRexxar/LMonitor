# -*- coding: utf-8 -*-
"""
专精统计聚合采集器
从数据库计算各专精的副本/团本/人物榜统计，输出 JSON 文件供 Portal 直接读取。
"""

import json
import os
from decimal import Decimal

from botend.controller.BaseScan import BaseScan
from botend.models import SeasonMeta
from botend.constants.wow import CLASS_SPEC_MAP, RAID_BOSS_CN, RAID_ZONE_CN
from botend.services.spec_stats_service import (
    SpecStatsService,
    _lookup_dungeon_cn,
)

from utils.log import logger


class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


class SpecDetailAggregationMonitor(BaseScan):

    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task

    def scan(self, url):
        logger.info("[SpecDetailAggregation] 开始聚合统计")

        season = SeasonMeta.objects.filter(is_active=True).first()
        if not season:
            logger.error("[SpecDetailAggregation] 无活跃赛季，跳过")
            return False

        base_dir = os.path.join('media', 'aggregated', str(season.id))
        total_files = 0

        for class_name, specs in CLASS_SPEC_MAP.items():
            for spec_name in specs:
                spec_dir = os.path.join(base_dir, class_name, spec_name)
                os.makedirs(spec_dir, exist_ok=True)

                self._aggregate_dungeon(season, class_name, spec_name, spec_dir)
                self._aggregate_raid(season, class_name, spec_name, spec_dir)
                self._aggregate_leaderboard(class_name, spec_name, spec_dir)

                total_files += 3

        logger.info(f"[SpecDetailAggregation] 完成: {total_files} 个文件")
        return True

    def _aggregate_dungeon(self, season, class_name, spec_name, spec_dir):
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
        if not season.raid_encounters:
            return

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
        result = SpecStatsService.get_player_list(
            class_name, spec_name, page=1, page_size=500
        )

        path = os.path.join(spec_dir, 'leaderboard.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(result, f, cls=DecimalEncoder, ensure_ascii=False, default=str)
