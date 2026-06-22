# -*- coding: utf-8 -*-
"""
M+ 副本 + 团本排名采集器
从 WCL v2 GraphQL 获取每个副本/Boss 每个专精的 Top 100 排名原始数据
"""

import time

from django.utils import timezone
from django.db import transaction

from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
from botend.models import SeasonMeta, SpecDungeonRanking, SpecRaidRanking
from botend.constants.wow import CLASS_SPEC_MAP
from botend.wow.talents.service import TalentBuildCodeService

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

    def _fetch_rankings_with_retry(self, enc_id, class_name, spec_name, metric='dps', difficulty=None, page=1, max_retries=5):
        """带加强重试的排名获取，处理限流和临时错误"""
        for attempt in range(max_retries):
            rankings = self.fetch_wcl_rankings(enc_id, class_name, spec_name, metric, difficulty=difficulty, page=page)
            if rankings is not None:
                return rankings

            # fetch_wcl_rankings 返回 None 可能是限流或临时错误
            wait = min(2 ** attempt * 3, 60)
            logger.warning(f"[SpecDetailRanking] 获取失败 {enc_id}/{class_name}/{spec_name}/page={page}, "
                          f"重试 {attempt+1}/{max_retries}, 等待 {wait}s")
            time.sleep(wait)

        logger.error(f"[SpecDetailRanking] 获取失败（已耗尽重试）: {enc_id}/{class_name}/{spec_name}/page={page}")
        return None

    def _collect_dungeon_rankings(self, season):
        """采集 M+ 副本排名：保留更多原始日志，聚合阶段再筛选 100 人样本。"""
        logger.info(f"[SpecDetailRanking] 采集 M+ 排名: {len(season.mplus_encounters)} 副本 x {sum(len(v) for v in CLASS_SPEC_MAP.values())} 专精")

        target_per_spec = 300
        total = 0
        empty_talent_total = 0
        now = timezone.now()
        records = []
        combatant_cache = {}

        for encounter in season.mplus_encounters:
            enc_id = encounter['id']
            enc_name = encounter['name']

            for class_name, specs in CLASS_SPEC_MAP.items():
                for spec_name in specs:
                    page = 1
                    collected = 0
                    empty_talent_count = 0

                    while collected < target_per_spec:
                        rankings = self._fetch_rankings_with_retry(enc_id, class_name, spec_name, 'dps', page=page)
                        if not rankings:
                            time.sleep(0.3)
                            break

                        rank_list = rankings.get('rankings', []) or []
                        if not rank_list:
                            break

                        for r in rank_list:
                            if collected >= target_per_spec:
                                break
                            try:
                                server = r.get('server', {}) or {}
                                report = r.get('report', {}) or {}
                                guild = r.get('guild', {}) or {}

                                talents_payload = self._parse_rank_talents(r, combatant_cache)
                                if not talents_payload:
                                    empty_talent_count += 1
                                    empty_talent_total += 1
                                records.append(SpecDungeonRanking(
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
                                    talents_json=talents_payload,
                                    talent_build_code=TalentBuildCodeService.encode_build_code_from_nodes(
                                        talents_json=talents_payload,
                                        class_name=class_name,
                                        spec_name=spec_name,
                                    ),
                                    gear_json=self.parse_wcl_gear(r.get('gear', [])),
                                    faction=r.get('faction'),
                                    guild_name=guild.get('name', ''),
                                    report_code=report.get('code', ''),
                                    fight_id=report.get('fightID'),
                                    last_updated=now,
                                ))
                                collected += 1
                            except Exception as e:
                                logger.warning(f"[SpecDetailRanking] M+ 构建记录失败: {e}")

                        if not rankings.get('hasMorePages') or len(rank_list) == 0:
                            break
                        page += 1
                        time.sleep(0.3)

                    if empty_talent_count:
                        logger.info(
                            f"[SpecDetailRanking] M+ {enc_id}/{class_name}/{spec_name}: "
                            f"{empty_talent_count}/{collected} 条记录缺失天赋数据（保留 DPS/装备样本，天赋聚合会排除）"
                        )
                    logger.info(f"[SpecDetailRanking] M+ {enc_id}/{class_name}/{spec_name}: 拉取 {collected} 条")
                    time.sleep(0.3)  # 限速

        try:
            with transaction.atomic():
                # 全量覆盖：删除该赛季旧数据
                SpecDungeonRanking.objects.filter(season_id=season.id).delete()
                if records:
                    SpecDungeonRanking.objects.bulk_create(records, batch_size=500)
            total = len(records)
        except Exception as e:
            logger.error(f"[SpecDetailRanking] M+ 排名写入失败: {e}")
            return False

        logger.info(f"[SpecDetailRanking] M+ 排名采集完成: {total} 条, 空天赋 {empty_talent_total} 条")
        return True

    def _parse_rank_talents(self, ranking, combatant_cache):
        """优先使用 report CombatantInfo.talentTree，回退到 ranking.talents。"""
        report = ranking.get('report', {}) or {}
        report_code = report.get('code', '')
        fight_id = report.get('fightID') or ranking.get('fightID')
        character_name = ranking.get('name', '')

        if report_code and fight_id and character_name:
            cache_key = (report_code, int(fight_id))
            if cache_key not in combatant_cache:
                combatant_cache[cache_key] = self.fetch_wcl_combatant_info(report_code, fight_id)
            combatant = self._find_combatant_for_ranking(combatant_cache.get(cache_key) or [], ranking)
            talents = self.parse_wcl_talent_tree((combatant or {}).get('talentTree') or [])
            if talents:
                return talents

        return self.parse_wcl_talents(ranking.get('talents', []))

    @staticmethod
    def _find_combatant_for_ranking(combatants, ranking):
        """用 ranking.name 在 CombatantInfo.source.name 中匹配当前角色。"""
        target_name = (ranking.get('name') or '').strip().lower()
        if not target_name:
            return None
        for combatant in combatants:
            source = combatant.get('source') or {}
            source_name = (source.get('name') or combatant.get('name') or '').strip().lower()
            if source_name == target_name:
                return combatant
        return None

    def _collect_raid_rankings(self, season):
        """采集团本排名（Mythic only），每个 boss 独立事务 + bulk_create"""
        logger.info(f"[SpecDetailRanking] 采集团本排名: {len(season.raid_encounters)} Boss x {sum(len(v) for v in CLASS_SPEC_MAP.values())} 专精")

        total = 0
        now = timezone.now()
        combatant_cache = {}

        # Build encounter→zone mapping from raid_zones (if available)
        enc_zone_map = {}
        if season.raid_zones:
            for rz in season.raid_zones:
                for enc in rz.get('encounters', []):
                    enc_zone_map[enc['id']] = {
                        'zone_id': rz.get('id'),
                        'zone_name': rz.get('name', ''),
                    }

        for idx, encounter in enumerate(season.raid_encounters):
            enc_id = encounter['id']
            enc_name = encounter['name']
            zone_info = enc_zone_map.get(enc_id, {})

            # Phase 1: 先收集该 boss 所有数据（无事务）
            records = []
            for class_name, specs in CLASS_SPEC_MAP.items():
                for spec_name in specs:
                    rankings = self._fetch_rankings_with_retry(
                        enc_id, class_name, spec_name, 'dps', difficulty=5
                    )
                    if not rankings:
                        time.sleep(0.3)
                        continue

                    rank_list = rankings.get('rankings', [])
                    for r in rank_list:
                        try:
                            server = r.get('server', {}) or {}
                            report = r.get('report', {}) or {}
                            guild = r.get('guild', {}) or {}

                            talents_payload = self._parse_rank_talents(r, combatant_cache)
                            records.append(SpecRaidRanking(
                                season_id=season.id,
                                boss_id=enc_id,
                                boss_name=enc_name,
                                raid_zone_id=zone_info.get('zone_id'),
                                raid_zone_name=zone_info.get('zone_name', ''),
                                class_name=class_name,
                                spec_name=spec_name,
                                character_name=r.get('name', ''),
                                realm=server.get('name', ''),
                                region=server.get('region', ''),
                                dps=r.get('amount', 0),
                                kill_time=r.get('duration'),
                                talents_json=talents_payload,
                                talent_build_code=TalentBuildCodeService.encode_build_code_from_nodes(
                                    talents_json=talents_payload,
                                    class_name=class_name,
                                    spec_name=spec_name,
                                ),
                                gear_json=self.parse_wcl_gear(r.get('gear', [])),
                                faction=r.get('faction'),
                                guild_name=guild.get('name', ''),
                                report_code=report.get('code', ''),
                                fight_id=report.get('fightID') or r.get('fightID'),
                                last_updated=now,
                            ))
                        except Exception as e:
                            logger.warning(f"[SpecDetailRanking] 构建记录失败: {enc_id}/{class_name}/{spec_name}: {e}")

                    time.sleep(0.3)  # 限速

            # Phase 2: 独立事务写入该 boss 的数据
            if records:
                try:
                    with transaction.atomic():
                        SpecRaidRanking.objects.filter(
                            season_id=season.id, boss_id=enc_id
                        ).delete()
                        SpecRaidRanking.objects.bulk_create(records, batch_size=500)
                    total += len(records)
                    logger.info(f"[SpecDetailRanking] Boss {enc_id} ({enc_name}): {len(records)} 条")
                except Exception as e:
                    logger.error(f"[SpecDetailRanking] Boss {enc_id} ({enc_name}) 写入失败: {e}")
            else:
                logger.warning(f"[SpecDetailRanking] Boss {enc_id} ({enc_name}): 0 条数据")

        logger.info(f"[SpecDetailRanking] 团本排名采集完成: {total} 条")
        return True
