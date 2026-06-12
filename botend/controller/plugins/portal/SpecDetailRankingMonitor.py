# -*- coding: utf-8 -*-
"""
M+ 副本 + 团本排名采集器
从 WCL v2 GraphQL 获取每个副本/Boss 每个专精的 Top 100 排名原始数据
"""

import time

from datetime import datetime
from django.db import transaction

from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
from botend.models import SeasonMeta, SpecDungeonRanking, SpecRaidRanking
from botend.constants.wow import CLASS_SPEC_MAP

from utils.log import logger


class SpecDetailRankingMonitor(SpecDetailBase):

    def __init__(self, req, task):
        super().__init__(req, task)

    def scan(self, url):
        logger.info("[SpecDetailRanking] 开始采集排名数据")

        season = SeasonMeta.objects.filter(is_active=True).first()
        if not season:
            logger.warning("[SpecDetailRanking] 无活跃赛季，先触发 SeasonMonitor")
            from botend.controller.plugins.portal.SpecDetailSeasonMonitor import SpecDetailSeasonMonitor
            sm = SpecDetailSeasonMonitor(self.req, self.task)
            sm.scan('')
            season = SeasonMeta.objects.filter(is_active=True).first()
        if not season:
            logger.error("[SpecDetailRanking] SeasonMonitor 执行后仍无活跃赛季，跳过")
            return False

        ok = True

        # M+ 副本排名
        if season.mplus_encounters:
            if not self._collect_dungeon_rankings(season):
                ok = False
        else:
            logger.warning("[SpecDetailRanking] 无 M+ 副本数据")

        # 团本排名
        if season.raid_encounters:
            if not self._collect_raid_rankings(season):
                ok = False
        else:
            logger.warning("[SpecDetailRanking] 无团本数据")

        if ok:
            self.task.flag = f"{season.season_key}@rankings@{int(time.time())}"
            self.task.save()

        return ok

    def _collect_dungeon_rankings(self, season):
        """采集 M+ 副本排名"""
        logger.info(f"[SpecDetailRanking] 采集 M+ 排名: {len(season.mplus_encounters)} 副本 x {sum(len(v) for v in CLASS_SPEC_MAP.values())} 专精")

        total = 0
        now = datetime.now()

        with transaction.atomic():
            # 全量覆盖：删除该赛季旧数据
            SpecDungeonRanking.objects.filter(season_id=season.id).delete()

            for encounter in season.mplus_encounters:
                enc_id = encounter['id']
                enc_name = encounter['name']

                for class_name, specs in CLASS_SPEC_MAP.items():
                    for spec_name in specs:
                        rankings = self.fetch_wcl_rankings(enc_id, class_name, spec_name, 'dps')
                        if not rankings:
                            time.sleep(0.3)
                            continue

                        rank_list = rankings.get('rankings', [])
                        for r in rank_list:
                            try:
                                server = r.get('server', {}) or {}
                                report = r.get('report', {}) or {}
                                guild = r.get('guild', {}) or {}

                                SpecDungeonRanking.objects.create(
                                    season_id=season.id,
                                    dungeon_id=enc_id,
                                    dungeon_name=enc_name,
                                    class_name=class_name,
                                    spec_name=spec_name,
                                    character_name=r.get('name', ''),
                                    realm=server.get('name', ''),
                                    region=server.get('region', ''),
                                    dps=r.get('amount', 0),
                                    keystone_level=r.get('hardModeLevel'),
                                    clear_time=r.get('duration'),
                                    score=r.get('score'),
                                    medal=r.get('medal', ''),
                                    affixes=r.get('affixes', []),
                                    talents_json=self.parse_wcl_talents(r.get('talents', [])),
                                    gear_json=self.parse_wcl_gear(r.get('gear', [])),
                                    faction=r.get('faction'),
                                    guild_name=guild.get('name', ''),
                                    report_code=report.get('code', ''),
                                    fight_id=report.get('fightID'),
                                    last_updated=now,
                                )
                                total += 1
                            except Exception as e:
                                logger.warning(f"[SpecDetailRanking] M+ 插入失败: {e}")

                        time.sleep(0.3)  # 限速

        logger.info(f"[SpecDetailRanking] M+ 排名采集完成: {total} 条")
        return True

    def _collect_raid_rankings(self, season):
        """采集团本排名（Mythic only）"""
        logger.info(f"[SpecDetailRanking] 采集团本排名: {len(season.raid_encounters)} Boss x {sum(len(v) for v in CLASS_SPEC_MAP.values())} 专精")

        total = 0
        now = datetime.now()

        with transaction.atomic():
            # 全量覆盖：删除该赛季旧数据
            SpecRaidRanking.objects.filter(season_id=season.id).delete()

            for encounter in season.raid_encounters:
                enc_id = encounter['id']
                enc_name = encounter['name']

                for class_name, specs in CLASS_SPEC_MAP.items():
                    for spec_name in specs:
                        rankings = self.fetch_wcl_rankings(enc_id, class_name, spec_name, 'dps', difficulty='mythic')
                        if not rankings:
                            time.sleep(0.3)
                            continue

                        rank_list = rankings.get('rankings', [])
                        for r in rank_list:
                            try:
                                server = r.get('server', {}) or {}
                                report = r.get('report', {}) or {}
                                guild = r.get('guild', {}) or {}

                                SpecRaidRanking.objects.create(
                                    season_id=season.id,
                                    boss_id=enc_id,
                                    boss_name=enc_name,
                                    class_name=class_name,
                                    spec_name=spec_name,
                                    character_name=r.get('name', ''),
                                    realm=server.get('name', ''),
                                    region=server.get('region', ''),
                                    dps=r.get('amount', 0),
                                    kill_time=r.get('duration'),
                                    talents_json=self.parse_wcl_talents(r.get('talents', [])),
                                    gear_json=self.parse_wcl_gear(r.get('gear', [])),
                                    faction=r.get('faction'),
                                    guild_name=guild.get('name', ''),
                                    report_code=report.get('code', ''),
                                    fight_id=report.get('fightID'),
                                    last_updated=now,
                                )
                                total += 1
                            except Exception as e:
                                logger.warning(f"[SpecDetailRanking] Raid 插入失败: {e}")

                        time.sleep(0.3)  # 限速

        logger.info(f"[SpecDetailRanking] 团本排名采集完成: {total} 条")
        return True
