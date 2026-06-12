# -*- coding: utf-8 -*-
"""
Top 20 人物榜采集器
从 Raider.IO 获取每个专精 Top 20 玩家数据，从 Battle.net 补充属性面板
"""

import time
import logging
from datetime import datetime

from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
from botend.models.spec_detail import SeasonMeta, PlayerSpecTopPlayer
from botend.constants.wow import CLASS_SPEC_MAP

logger = logging.getLogger(__name__)


class SpecDetailPlayerMonitor(SpecDetailBase):

    def __init__(self, req, task):
        super().__init__(req, task)

    def scan(self, url):
        logger.info("[SpecDetailPlayer] 开始采集人物榜")

        season = SeasonMeta.objects.filter(is_active=True).first()
        if not season:
            logger.error("[SpecDetailPlayer] 无活跃赛季，请先运行 SpecDetailSeasonMonitor")
            return False

        rio_season = season.rio_season
        if not rio_season:
            logger.error("[SpecDetailPlayer] SeasonMeta.rio_season 为空")
            return False

        total_inserted = 0

        # 遍历所有专精
        for class_name, specs in CLASS_SPEC_MAP.items():
            for spec_name in specs:
                # 全量覆盖：删除该专精旧数据
                PlayerSpecTopPlayer.objects.filter(
                    season_id=season.id, class_name=class_name, spec_name=spec_name
                ).delete()

                # 从 Raider.IO 获取 Top 20
                players = self._fetch_top_players(class_name, spec_name, rio_season)
                if not players:
                    continue

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
                            last_updated=datetime.now(),
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

    def _fetch_top_players(self, class_name, spec_name, season):
        """从 Raider.IO 获取 Top 20 玩家"""
        data = self.fetch_raiderio_top(class_name, spec_name, season, limit=20)
        if not data:
            return []

        rankings = data.get('rankings', [])
        players = []

        for r in rankings:
            char = r.get('character', {}) or {}
            # Raider.IO rankings API 返回的数据结构
            player = {
                'name': char.get('name', ''),
                'realm': char.get('realm', {}).get('name', '') if isinstance(char.get('realm'), dict) else char.get('realm', ''),
                'region': r.get('region', ''),
                'score': r.get('score', {}).get('score') if isinstance(r.get('score'), dict) else r.get('score'),
                'faction': char.get('faction', ''),
                'race': char.get('race', ''),
                'gender': char.get('gender', ''),
                'guild_name': char.get('guild', {}).get('name', '') if isinstance(char.get('guild'), dict) else '',
                'realm_rank': r.get('realm_rank'),
                'avatar_url': char.get('thumbnail_url', ''),
                'profile_url': char.get('profile_url', ''),
                'achievement_points': char.get('achievement_points'),
                'item_level': char.get('gear', {}).get('item_level_equipped') if isinstance(char.get('gear'), dict) else None,
                'gear': self._parse_rio_gear(char.get('gear')),
                'talents': self._parse_rio_talents(char.get('talents')),
            }
            players.append(player)

        return players

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

    def _parse_rio_talents(self, talent_data):
        """解析 Raider.IO talent 数据"""
        if not talent_data:
            return []
        # Raider.IO 的 talents 可能是不同的格式
        if isinstance(talent_data, list):
            return [{'talentID': t.get('id') or t.get('spell_id'), 'points': t.get('rank', 1)} for t in talent_data]
        if isinstance(talent_data, dict):
            # character_talent 格式
            loadout = talent_data.get('loadout', []) or []
            return [{'talentID': t.get('id'), 'points': t.get('rank', 1)} for t in loadout]
        return []

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
