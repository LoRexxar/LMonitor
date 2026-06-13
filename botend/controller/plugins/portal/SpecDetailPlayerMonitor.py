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
        """从 Raider.IO 获取全球 Top 20 玩家，优先使用世界榜。"""
        top20 = self._fetch_world_top_players(class_name, spec_name, season)

        if len(top20) < 20:
            logger.warning(
                f"[SpecDetailPlayer] world 榜单不足 20 条，回退到多地区聚合: {class_name}/{spec_name}"
            )
            top20 = self._fetch_top_players_from_regions(class_name, spec_name, season)

        # 为 Top 20 玩家补充结构化天赋数据
        self._enrich_talents(top20)

        return top20

    def _fetch_world_top_players(self, class_name, spec_name, season):
        """直接抓取 Raider.IO 世界榜前 20。"""
        data = self.fetch_raiderio_top(
            class_name, spec_name, season, region='world', limit=20, page=0
        )
        if not data:
            return []

        rankings = data.get('rankings', {}) or {}
        ranked_characters = rankings.get('rankedCharacters', []) if isinstance(rankings, dict) else []

        result = []
        for r in ranked_characters[:20]:
            player = self._build_player_from_ranking(class_name, spec_name, r)
            if player:
                result.append(player)
        return result

    def _fetch_top_players_from_regions(self, class_name, spec_name, season):
        """兜底策略：按地区抓取第一页，再按分数聚合。"""
        all_players = []

        for region in self.REGIONS:
            data = self.fetch_raiderio_top(
                class_name, spec_name, season, region=region, limit=20, page=0
            )
            if not data:
                continue

            rankings = data.get('rankings', {}) or {}
            ranked_characters = rankings.get('rankedCharacters', []) if isinstance(rankings, dict) else []

            for r in ranked_characters:
                player = self._build_player_from_ranking(class_name, spec_name, r)
                if player:
                    all_players.append(player)

            time.sleep(0.3)  # 限速

        all_players.sort(key=lambda p: p.get('score', 0) or 0, reverse=True)
        return all_players[:20]

    def _build_player_from_ranking(self, class_name, spec_name, ranking):
        """将 Raider.IO 排名响应中的单个角色转换为统一结构。"""
        char = ranking.get('character', {}) or {}
        if not char:
            return None

        realm = char.get('realm', {}) or {}
        region_info = char.get('region', {}) or {}
        race = char.get('race', {}) or {}
        guild = ranking.get('guild', {}) or {}

        path = char.get('path', '')
        profile_url = ('https://raider.io' + path) if path else None

        return {
            '_class_name': class_name,
            '_spec_name': spec_name,
            'name': char.get('name', ''),
            'realm': realm.get('name', ''),
            'region': region_info.get('short_name', '') or region_info.get('slug', ''),
            'score': ranking.get('score', 0),
            'faction': char.get('faction', ''),
            'race': race.get('name', ''),
            'gender': None,
            'guild_name': guild.get('name', ''),
            'realm_rank': None,
            'avatar_url': None,
            'profile_url': profile_url,
            'achievement_points': None,
            'item_level': None,
            'gear': self._parse_rio_gear(char.get('gear')),
            'talents': self._parse_rio_talents(char.get('talentLoadoutText')),
        }

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
        """解析 Raider.IO talentLoadoutText — 直接返回 Blizz talent code 字符串
        作为 fallback，当结构化数据获取失败时使用"""
        if not talent_loadout_text:
            return []
        return [{
            'tree_type': 'build_code',
            'talent_code': talent_loadout_text,
            'talentID': None,
            'spellID': None,
            'talent_id': None,
            'spell_id': None,
            'name': 'Talent Loadout',
            'icon': '',
            'points': 0,
            'row': None,
            'column': None,
        }]

    def _enrich_talents(self, players):
        """为 Top 20 玩家补充结构化天赋+装备数据。
        
        优先从本地 dungeon_ranking / raid_ranking 表匹配（WCL 数据，最完整）。
        仅对本地没有的玩家调用 Raider.IO 角色 API。
        """
        # 先从本地 ranking 表批量匹配
        self._enrich_from_local_rankings(players)

        # 对仍然没有结构化数据的玩家，调用 Raider.IO 角色 API
        for player in players:
            # 已有完整结构化数据，跳过
            if self._has_display_ready_talents(player.get('talents')):
                continue

            region = player.get('region', '')
            realm = player.get('realm', '')
            name = player.get('name', '')
            if not all([region, realm, name]):
                continue

            if region.lower() == 'cn':
                continue

            try:
                char_data = self.fetch_raiderio_character(region, realm, name)
                if not char_data:
                    continue

                structured = self._parse_rio_talents_from_profile(char_data)
                if structured:
                    player['talents'] = structured

            except Exception as e:
                logger.warning(f"[SpecDetailPlayer] 获取天赋失败 {name}-{realm}@{region}: {e}")

            time.sleep(0.5)  # Raider.IO rate limit

    @staticmethod
    def _has_display_ready_talents(talents):
        """判断当前天赋是否已具备展示所需信息。"""
        if not talents or not isinstance(talents, list):
            return False

        dict_nodes = [
            talent for talent in talents
            if isinstance(talent, dict) and (talent.get('talentID') or talent.get('talent_id'))
        ]
        if not dict_nodes:
            return False

        named_nodes = [
            talent for talent in dict_nodes
            if talent.get('name') and talent.get('icon')
        ]
        return len(named_nodes) >= max(5, len(dict_nodes) // 3)

    def _enrich_from_local_rankings(self, players):
        """从本地 dungeon_ranking / raid_ranking 表匹配天赋+装备"""
        from botend.models import SpecDungeonRanking, SpecRaidRanking

        for player in players:
            name = player.get('name', '')
            class_name = player.get('_class_name', '')
            spec_name = player.get('_spec_name', '')
            if not name or not class_name or not spec_name:
                continue

            # 从 dungeon_ranking 匹配（优先，数据最全）
            ranking = SpecDungeonRanking.objects.filter(
                character_name=name,
                class_name=class_name,
                spec_name=spec_name,
            ).exclude(talents_json='[]').first()

            if not ranking:
                # 从 raid_ranking 匹配
                ranking = SpecRaidRanking.objects.filter(
                    character_name=name,
                    class_name=class_name,
                    spec_name=spec_name,
                ).exclude(talents_json='[]').first()

            if ranking:
                if ranking.talents_json and isinstance(ranking.talents_json, list):
                    if ranking.talents_json and isinstance(ranking.talents_json[0], dict):
                        player['talents'] = ranking.talents_json
                if ranking.gear_json and isinstance(ranking.gear_json, list) and ranking.gear_json:
                    player['gear'] = ranking.gear_json

    def _parse_rio_talents_from_profile(self, char_data):
        """从 Raider.IO 角色 profile 响应中解析结构化天赋数据"""
        talents_obj = char_data.get('talents')
        if not talents_obj or not isinstance(talents_obj, dict):
            return None

        selected = talents_obj.get('selected')
        if not selected or not isinstance(selected, list):
            return None

        result = []
        for entry in selected:
            talent = entry.get('talent') or entry
            if not isinstance(talent, dict):
                continue
            spell = talent.get('spell') or {}
            result.append({
                'tree_type': talent.get('treeType') or talent.get('tree_type') or 'spec',
                'talentID': talent.get('id'),
                'spellID': spell.get('id'),
                'talent_id': talent.get('id'),
                'spell_id': spell.get('id'),
                'name': spell.get('name', ''),
                'icon': spell.get('icon', ''),
                'points': 1,
                'tier': talent.get('tier'),
                'row': talent.get('row') or talent.get('tier'),
                'column': talent.get('column'),
            })

        return result if result else None

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
