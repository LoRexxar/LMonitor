# -*- coding: utf-8 -*-
"""
Top 20 人物榜采集器
从 Raider.IO 获取每个专精 Top 20 玩家数据，从 Battle.net 补充属性面板
"""

import time

from django.utils import timezone
from django.db import transaction

from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
from botend.models import SeasonMeta, PlayerSpecTopPlayer
from botend.constants.wow import CLASS_SPEC_MAP

from utils.log import logger


class SpecDetailPlayerMonitor(SpecDetailBase):

    def __init__(self, req, task):
        super().__init__(req, task)

    def scan(self, url):
        logger.info("[SpecDetailPlayer] 开始采集人物榜")

        season = SeasonMeta.objects.filter(is_active=True).first()
        if not season:
            logger.warning("[SpecDetailPlayer] 无活跃赛季，先触发 SeasonMonitor")
            from botend.controller.plugins.portal.SpecDetailSeasonMonitor import SpecDetailSeasonMonitor
            sm = SpecDetailSeasonMonitor(self.req, self.task)
            sm.scan('')
            season = SeasonMeta.objects.filter(is_active=True).first()
        if not season:
            logger.error("[SpecDetailPlayer] SeasonMonitor 执行后仍无活跃赛季，跳过")
            return False

        rio_season = season.rio_season
        if not rio_season:
            logger.error("[SpecDetailPlayer] SeasonMeta.rio_season 为空")
            return False

        total_inserted = 0

        # 遍历所有专精
        for class_name, specs in CLASS_SPEC_MAP.items():
            for spec_name in specs:
                # 从 Raider.IO 获取 Top 20
                players = self._fetch_top_players(class_name, spec_name, rio_season)
                if not players:
                    continue

                with transaction.atomic():
                    # 全量覆盖：删除该专精旧数据
                    PlayerSpecTopPlayer.objects.filter(
                        season_id=season.id, class_name=class_name, spec_name=spec_name
                    ).delete()

                    # 存入数据库
                    for i, player in enumerate(players):
                        try:
                            PlayerSpecTopPlayer.objects.create(
                                season_id=season.id,
                                region=player.get('region', ''),
                                realm=player.get('realm', ''),
                                character_name=player.get('name', ''),
                                class_name=class_name,
                                spec_name=spec_name,
                                rank=i + 1,
                                score=player.get('score'),
                                faction=player.get('faction'),
                                race=player.get('race'),
                                gender=player.get('gender'),
                                guild_name=player.get('guild_name'),
                                realm_rank=player.get('realm_rank'),
                                avatar_url=player.get('avatar_url'),
                                profile_url=player.get('profile_url'),
                                achievement_points=player.get('achievement_points'),
                                item_level=player.get('item_level'),
                                gear_json=player.get('gear', []),
                                talents_json=player.get('talents', []),
                                stats_json={},
                                stats_crawl_status=0,
                                last_updated=timezone.now(),
                            )
                            total_inserted += 1
                        except Exception as e:
                            logger.warning(f"[SpecDetailPlayer] 插入失败 {player.get('name')}: {e}")

                time.sleep(0.2)

        logger.info(f"[SpecDetailPlayer] 人物榜采集完成，共 {total_inserted} 条")

        # 补充 Battle.net 属性（排除国服）
        self._crawl_battlenet_stats(season.id)

        self.task.flag = f"{season.season_key}@players={total_inserted}@{int(time.time())}"
        self.task.save()
        return True

    # 需要覆盖的地区列表
    REGIONS = ['us', 'eu', 'tw', 'kr', 'cn']

    def _fetch_top_players(self, class_name, spec_name, season):
        """从 Raider.IO 获取全球 Top 20 玩家（合并所有地区）"""
        all_players = []

        for region in self.REGIONS:
            data = self.fetch_raiderio_top(class_name, spec_name, season, region=region, limit=20)
            if not data:
                continue

            rankings = data.get('rankings', {}) or {}
            ranked_characters = rankings.get('rankedCharacters', []) if isinstance(rankings, dict) else []

            for r in ranked_characters:
                char = r.get('character', {}) or {}
                realm = char.get('realm', {}) or {}
                region_info = char.get('region', {}) or {}
                race = char.get('race', {}) or {}
                guild = r.get('guild', {}) or {}

                path = char.get('path', '')
                profile_url = ('https://raider.io' + path) if path else None

                player = {
                    'name': char.get('name', ''),
                    'realm': realm.get('name', ''),
                    'region': region_info.get('short_name', '') or region_info.get('slug', ''),
                    'score': r.get('score', 0),
                    'faction': char.get('faction', ''),
                    'race': race.get('name', ''),
                    'gender': None,
                    'guild_name': guild.get('name', ''),
                    'realm_rank': None,
                    'avatar_url': None,
                    'profile_url': profile_url,
                    'achievement_points': None,
                    'item_level': None,
                    'gear': [],
                    'talents': self._parse_rio_talents(char.get('talentLoadoutText')),
                }
                all_players.append(player)

            time.sleep(0.3)  # 限速

        # 按 score 降序排列，取全球 Top 20
        all_players.sort(key=lambda p: p.get('score', 0) or 0, reverse=True)
        return all_players[:20]

    def _parse_rio_gear(self, gear_data):
        """解析 Raider.IO gear 数据"""
        if not gear_data or not isinstance(gear_data, dict):
            return []
        items = gear_data.get('items', {}) or {}
        result = []
        for slot, item in items.items():
            if not item:
                continue
            result.append({
                'slot': slot,
                'name': item.get('name', ''),
                'id': item.get('item_id'),
                'icon': item.get('icon', ''),
                'itemLevel': item.get('item_level'),
                'quality': item.get('quality', ''),
                'bonusIDs': [],
                'gems': [],
            })
        return result

    def _parse_rio_talents(self, talent_loadout_text):
        """解析 Raider.IO talentLoadoutText — 直接返回 Blizz talent code 字符串"""
        if not talent_loadout_text:
            return []
        return [talent_loadout_text]

    def _crawl_battlenet_stats(self, season_id):
        """补充 Battle.net 属性面板"""
        pending = PlayerSpecTopPlayer.objects.filter(
            season_id=season_id,
            stats_crawl_status=0
        ).exclude(region='cn')

        success = 0
        fail = 0

        for player in pending[:200]:  # 限制单次处理量
            data = self.fetch_battlenet_stats(player.realm, player.character_name, player.region)
            if data:
                stats = self.parse_battlenet_stats(data)
                if stats:
                    player.stats_json = stats
                    player.stats_crawl_status = 1
                    player.save(update_fields=['stats_json', 'stats_crawl_status'])
                    success += 1
                else:
                    player.stats_crawl_status = -1
                    player.save(update_fields=['stats_crawl_status'])
                    fail += 1
            else:
                player.stats_crawl_status = -1
                player.save(update_fields=['stats_crawl_status'])
                fail += 1

            time.sleep(0.1)

        logger.info(f"[SpecDetailPlayer] Battle.net 属性补充: 成功 {success}, 失败 {fail}")
