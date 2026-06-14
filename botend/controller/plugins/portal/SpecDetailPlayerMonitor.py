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
from botend.wow.talents.view_model import build_talent_view_model

from utils.log import logger


class SpecDetailPlayerMonitor(SpecDetailBase):
    DEFAULT_GEAR_SLOTS = [
        'head', 'neck', 'shoulder', 'shirt', 'chest', 'waist', 'legs', 'feet',
        'wrist', 'hands', 'finger1', 'finger2', 'trinket1', 'trinket2',
        'back', 'main_hand', 'off_hand', 'tabard',
    ]

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
                    existing_qs = PlayerSpecTopPlayer.objects.filter(
                        season_id=season.id, class_name=class_name, spec_name=spec_name
                    )
                    existing_map = {
                        ((row.region or '').lower(), row.realm or '', row.character_name or ''): row
                        for row in existing_qs
                    }
                    seen_keys = set()

                    for i, player in enumerate(players):
                        try:
                            region = player.get('region', '')
                            realm = player.get('realm', '')
                            character_name = player.get('name', '')
                            row_key = ((region or '').lower(), realm, character_name)
                            seen_keys.add(row_key)
                            existing = existing_map.get(row_key)
                            stats_json = existing.stats_json if existing and existing.stats_json else {}
                            stats_status = existing.stats_crawl_status if existing else 0

                            PlayerSpecTopPlayer.objects.update_or_create(
                                season_id=season.id,
                                class_name=class_name,
                                spec_name=spec_name,
                                region=region,
                                realm=realm,
                                character_name=character_name,
                                defaults={
                                    'rank': i + 1,
                                    'score': player.get('score'),
                                    'faction': player.get('faction'),
                                    'race': player.get('race'),
                                    'gender': player.get('gender'),
                                    'guild_name': player.get('guild_name'),
                                    'realm_rank': player.get('realm_rank'),
                                    'avatar_url': player.get('avatar_url'),
                                    'profile_url': player.get('profile_url'),
                                    'achievement_points': player.get('achievement_points'),
                                    'item_level': player.get('item_level'),
                                    'gear_json': self._normalize_gear_list(player.get('gear', [])),
                                    'talents_json': self._normalize_talent_nodes(
                                        player.get('talents', []),
                                        class_name,
                                        spec_name,
                                    ),
                                    'stats_json': stats_json,
                                    'stats_crawl_status': stats_status,
                                    'last_updated': timezone.now(),
                                },
                            )
                            total_inserted += 1
                        except Exception as e:
                            logger.warning(f"[SpecDetailPlayer] 插入失败 {player.get('name')}: {e}")

                    stale_ids = [
                        row.id for key, row in existing_map.items()
                        if key not in seen_keys
                    ]
                    if stale_ids:
                        PlayerSpecTopPlayer.objects.filter(id__in=stale_ids).delete()

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
            'item_level': self._coerce_item_level(char.get('gear')),
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
            normalized_slot = self._normalize_rio_slot(slot)
            result.append({
                'slot': normalized_slot,
                'name': item.get('name', ''),
                'id': item.get('item_id'),
                'icon': self._normalize_icon_name(item.get('icon', '')),
                'itemLevel': item.get('item_level'),
                'quality': item.get('quality', '') or item.get('item_quality', ''),
                'bonusIDs': item.get('bonuses', []) or [],
                'gems': item.get('gems', []) or [],
                'gems_detail': item.get('gems_detail', []) or [],
                'enchants': item.get('enchants', []) or [],
                'enchants_detail': item.get('enchants_detail', []) or [],
                'source': 'raiderio_profile',
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
        
        天赋优先从本地 dungeon_ranking / raid_ranking 表匹配（WCL 数据，最完整）。
        装备与角色资料优先使用 Raider.IO profile（字段更完整）。
        """
        # 先从本地 ranking 表批量匹配
        self._enrich_from_local_rankings(players)

        # 再为所有玩家调用 Raider.IO profile，补角色资料与更完整的装备字段
        for player in players:
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

                gear_items = self._parse_rio_gear(char_data.get('gear'))
                if gear_items:
                    player['gear'] = self._normalize_gear_list(gear_items)

                profile_ilvl = self._coerce_item_level(char_data.get('gear'))
                if profile_ilvl:
                    player['item_level'] = profile_ilvl

                player['achievement_points'] = char_data.get('achievement_points') or player.get('achievement_points')
                player['profile_url'] = char_data.get('profile_url') or player.get('profile_url')
                player['avatar_url'] = char_data.get('thumbnail_url') or player.get('avatar_url')
                player['race'] = char_data.get('race') or player.get('race')
                player['faction'] = char_data.get('faction') or player.get('faction')

                if self._has_display_ready_talents(player.get('talents')):
                    time.sleep(0.2)
                    continue

                structured = self._parse_rio_talents_from_profile(char_data)
                if structured:
                    player['talents'] = self._normalize_talent_nodes(
                        structured,
                        player.get('_class_name', ''),
                        player.get('_spec_name', ''),
                    )

            except Exception as e:
                logger.warning(f"[SpecDetailPlayer] 获取天赋失败 {name}-{realm}@{region}: {e}")

            time.sleep(0.2)  # Raider.IO rate limit

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
        """从本地 dungeon_ranking / raid_ranking 表匹配天赋，必要时回填基础装备。"""
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
                        player['talents'] = self._normalize_talent_nodes(
                            ranking.talents_json,
                            class_name,
                            spec_name,
                        )
                if (not player.get('gear')) and ranking.gear_json and isinstance(ranking.gear_json, list) and ranking.gear_json:
                    player['gear'] = self._normalize_gear_list(ranking.gear_json)

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

    def _crawl_battlenet_stats(self, season_id, class_name=None, spec_name=None, retry_failed=False, limit=None):
        """补充 Battle.net 属性面板"""
        pending = PlayerSpecTopPlayer.objects.filter(season_id=season_id).exclude(region='cn')
        if class_name:
            pending = pending.filter(class_name=class_name)
        if spec_name:
            pending = pending.filter(spec_name=spec_name)
        if retry_failed:
            pending = pending.filter(stats_crawl_status__in=[0, -1])
        else:
            pending = pending.filter(stats_crawl_status=0)
        if limit:
            pending = pending[:limit]

        if not self._get_battlenet_token():
            updated = 0
            for player in pending.iterator(chunk_size=50) if hasattr(pending, 'iterator') else pending:
                if player.stats_crawl_status != -2:
                    player.stats_crawl_status = -2
                    player.save(update_fields=['stats_crawl_status'])
                    updated += 1
            logger.warning(f"[SpecDetailPlayer] Battle.net 未配置，跳过属性采集 {updated} 条")
            return

        success = 0
        fail = 0

        for player in pending.iterator(chunk_size=50) if hasattr(pending, 'iterator') else pending:
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

    @staticmethod
    def _normalize_icon_name(icon_name):
        icon_name = str(icon_name or '').strip()
        if not icon_name:
            return ''
        if '.' in icon_name:
            icon_name = icon_name.rsplit('/', 1)[-1].rsplit('.', 1)[0]
        return icon_name

    def _normalize_gear_list(self, gear_list):
        result = []
        for item in gear_list or []:
            if not isinstance(item, dict):
                continue
            normalized = dict(item)
            normalized['icon'] = self._normalize_icon_name(item.get('icon', ''))
            result.append(normalized)
        if result and all((item.get('slot') or 'unknown') == 'unknown' for item in result):
            for idx, item in enumerate(result):
                if idx >= len(self.DEFAULT_GEAR_SLOTS):
                    break
                item['slot'] = self.DEFAULT_GEAR_SLOTS[idx]
        return result

    @staticmethod
    def _normalize_rio_slot(slot):
        mapping = {
            'mainhand': 'main_hand',
            'offhand': 'off_hand',
        }
        return mapping.get((slot or '').strip().lower(), slot)

    @staticmethod
    def _coerce_item_level(gear_data):
        if not isinstance(gear_data, dict):
            return None
        value = gear_data.get('item_level_equipped') or gear_data.get('item_level_total')
        try:
            return int(value) if value is not None else None
        except Exception:
            return None

    @staticmethod
    def _normalize_talent_nodes(talents, class_name, spec_name):
        if not talents:
            return []
        vm = build_talent_view_model(talents, class_name=class_name, spec_name=spec_name)
        normalized = []
        for node in vm.get('nodes') or []:
            if not isinstance(node, dict):
                continue
            normalized.append({
                'tree_type': node.get('tree_type') or 'spec',
                'talent_code': node.get('talent_code', ''),
                'node_id': node.get('node_id'),
                'talent_id': node.get('talent_id'),
                'spell_id': node.get('spell_id'),
                'display_spell_id': node.get('display_spell_id'),
                'name': node.get('name', ''),
                'icon': node.get('icon', ''),
                'points': node.get('points', 0),
                'max_points': node.get('max_points'),
                'row': node.get('row'),
                'column': node.get('column'),
                'selected': node.get('selected', True),
                'source': node.get('source', 'monitor'),
            })
        return normalized
