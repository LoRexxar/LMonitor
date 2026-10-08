# -*- coding: utf-8 -*-
"""
专精统计聚合采集器
从数据库计算各专精的副本/团本/人物榜统计，输出 JSON 文件供 Portal 直接读取。
"""

import json
import os
import tempfile
from decimal import Decimal
from datetime import date, datetime

from django.conf import settings
from django.utils import timezone

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
        if isinstance(o, (date, datetime)):
            return o.isoformat()
        return super().default(o)


def atomic_dump_json(path, payload, **dump_kwargs):
    """原子写 JSON：写入失败或进程中断时保留旧文件。"""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix='.tmp-', suffix='.json', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(payload, f, **dump_kwargs)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def publish_stats(path, payload, class_name, spec_name):
    """后台统一补全装备与天赋，页面只读取已发布的展示结果。"""
    from botend.services.wow_item_display import refresh_aggregate_equipment
    from botend.services.wow_talent_display import refresh_aggregate_talents
    payload = refresh_aggregate_equipment(payload, class_name=class_name, spec_name=spec_name)
    payload = refresh_aggregate_talents(payload, class_name=class_name, spec_name=spec_name)
    payload['generated_at'] = timezone.now().isoformat()
    atomic_dump_json(path, payload, cls=DecimalEncoder, ensure_ascii=False)


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

        base_dir = os.path.join(getattr(settings, 'MEDIA_ROOT', '') or 'media', 'aggregated', str(season.id))
        total_files = 0

        for class_name, specs in CLASS_SPEC_MAP.items():
            for spec_name in specs:
                spec_dir = os.path.join(base_dir, class_name, spec_name)
                os.makedirs(spec_dir, exist_ok=True)

                self._aggregate_dungeon(season, class_name, spec_name, spec_dir)
                self._aggregate_raid(season, class_name, spec_name, spec_dir)
                self._aggregate_leaderboard(season.id, class_name, spec_name, spec_dir)

                total_files += 3

        logger.info(f"[SpecDetailAggregation] 完成: {total_files} 个文件")
        return True

    @staticmethod
    def _aggregate_dungeon(season, class_name, spec_name, spec_dir):
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
        summary = SpecStatsService.get_dungeon_summary(class_name, spec_name, season.id)
        publish_stats(path, {'dungeons': dungeons, 'summary': summary}, class_name, spec_name)

    @staticmethod
    def _aggregate_raid(season, class_name, spec_name, spec_dir):
        if not season.raid_encounters:
            return

        def aggregate_difficulty(difficulty):
            if season.raid_zones:
                zone_groups = []
                for rz in season.raid_zones:
                    zone_cn = RAID_ZONE_CN.get(rz.get('name', ''), rz.get('name', ''))
                    zone_bosses = []
                    for enc in rz.get('encounters', []):
                        cn_name = RAID_BOSS_CN.get(enc['name'], enc['name'])
                        stats = SpecStatsService._compute_raid_stats(
                            season.id, enc['id'], cn_name, class_name, spec_name, full=True,
                            difficulty=difficulty,
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
                return zone_groups

            bosses = []
            for enc in season.raid_encounters:
                cn_name = RAID_BOSS_CN.get(enc['name'], enc['name'])
                bosses.append(SpecStatsService._compute_raid_stats(
                    season.id, enc['id'], cn_name, class_name, spec_name, full=True,
                    difficulty=difficulty,
                ))
            return [{'zone_id': 0, 'zone_name': '', 'zone_cn': '', 'bosses': bosses}]

        mythic_zones = aggregate_difficulty(5)
        heroic_zones = aggregate_difficulty(4)

        path = os.path.join(spec_dir, 'raid.json')
        publish_stats(path, {
            'zone_groups': mythic_zones,
            'difficulties': [
                {'difficulty': 5, 'label': '史诗团本表现', 'zone_groups': mythic_zones},
                {'difficulty': 4, 'label': '英雄团本表现', 'zone_groups': heroic_zones},
            ],
        }, class_name, spec_name)

    @staticmethod
    def refresh_leaderboard_projection(season_id, class_name, spec_name):
        """只刷新人物榜投影；供10分钟巅峰榜轻量更新复用。"""
        media_root = getattr(settings, 'MEDIA_ROOT', '') or 'media'
        spec_dir = os.path.join(media_root, 'aggregated', str(season_id), class_name, spec_name)
        return SpecDetailAggregationMonitor._aggregate_leaderboard(
            season_id, class_name, spec_name, spec_dir,
        )

    @staticmethod
    def _aggregate_leaderboard(season_id, class_name, spec_name, spec_dir):
        result = SpecStatsService.get_player_list(
            class_name, spec_name, season_id=season_id, page=1, page_size=20
        )
        # last_updated 是人物内容时间；榜单投影必须展示本次排名刷新时间。
        result['updated_at'] = timezone.now().isoformat()

        path = os.path.join(spec_dir, 'leaderboard.json')
        publish_stats(path, result, class_name, spec_name)
        return path
