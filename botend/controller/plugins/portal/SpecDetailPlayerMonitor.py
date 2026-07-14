# -*- coding: utf-8 -*-
"""
人物榜采集器
从 Raider.IO 获取每个专精榜单玩家数据，从 Battle.net 补充前排玩家属性面板
"""

import time
import unicodedata

from django.utils import timezone
from django.db import IntegrityError, transaction

from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
from botend.models import SeasonMeta, PlayerSpecTopPlayer
from botend.constants.wow import CLASS_SPEC_MAP
from botend.wow.talents.view_model import build_talent_view_model
from botend.wow.talents.service import TalentBuildCodeService

from utils.log import logger


class SpecDetailPlayerMonitor(SpecDetailBase):
    PLAYER_RANKING_LIMIT = 20
    PROFILE_ENRICH_LIMIT = 20
    RANKING_SAMPLE_BACKFILL_LIMIT = 100
    PROFILE_UPDATE_FIELDS = (
        'class_name', 'rank', 'score', 'faction', 'race', 'gender', 'guild_name',
        'realm_rank', 'avatar_url', 'profile_url', 'achievement_points', 'item_level',
        'gear_json', 'talents_json', 'talent_build_code', 'stats_json',
        'stats_crawl_status',
    )

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
                # 从 Raider.IO 获取榜单玩家
                players = self._fetch_top_players(class_name, spec_name, rio_season)
                if not players:
                    continue

                with transaction.atomic():
                    existing_qs = PlayerSpecTopPlayer.objects.filter(
                        season_id=season.id, spec_name=spec_name
                    )
                    existing_map = {
                        self._profile_identity_key(row.region, row.realm, row.character_name): row
                        for row in existing_qs
                    }
                    existing_map.pop(None, None)
                    seen_keys = set()

                    for i, player in enumerate(players):
                        try:
                            region = player.get('region', '')
                            realm = player.get('realm', '')
                            character_name = player.get('name', '')
                            row_key = self._profile_identity_key(region, realm, character_name)
                            seen_keys.add(row_key)
                            existing = existing_map.get(row_key)
                            stats_json = existing.stats_json if existing and existing.stats_json else {}
                            stats_status = existing.stats_crawl_status if existing else 0

                            defaults = {
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
                                'talent_build_code': player.get('talent_build_code', '') or TalentBuildCodeService.extract_build_code(
                                    talents_json=player.get('talents', [])
                                ),
                                'talents_json': self._normalize_talent_nodes(
                                    player.get('talents', []),
                                    class_name,
                                    spec_name,
                                ),
                                'stats_json': stats_json,
                                'stats_crawl_status': stats_status,
                                'last_updated': timezone.now(),
                            }
                            self._preserve_complete_talents_when_new_payload_is_downgrade(existing, defaults, class_name, spec_name)
                            if existing:
                                defaults['class_name'] = class_name
                                self._save_changed_profile(existing, defaults)
                            else:
                                profile = PlayerSpecTopPlayer(
                                    season_id=season.id,
                                    region=(region or '').strip().lower(),
                                    realm=(realm or '').strip(),
                                    character_name=(character_name or '').strip(),
                                    class_name=class_name,
                                    spec_name=spec_name,
                                )
                                for field, value in defaults.items():
                                    setattr(profile, field, value)
                                self._save_profile_safely(profile)
                            total_inserted += 1
                        except Exception as e:
                            logger.warning(f"[SpecDetailPlayer] 插入失败 {player.get('name')}: {e}")

                self._backfill_ranking_sample_profiles(
                    season.id,
                    class_name,
                    spec_name,
                    limit=self.RANKING_SAMPLE_BACKFILL_LIMIT,
                )

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
        """从 Raider.IO 获取全球榜单玩家，优先使用世界榜。"""
        players = self._fetch_world_top_players(class_name, spec_name, season)

        if len(players) < self.PLAYER_RANKING_LIMIT:
            logger.warning(
                f"[SpecDetailPlayer] world 榜单不足 {self.PLAYER_RANKING_LIMIT} 条，回退到多地区聚合: {class_name}/{spec_name}"
            )
            players = self._fetch_top_players_from_regions(class_name, spec_name, season)

        # 只有前排玩家需要补充装备/天赋/属性等昂贵 profile 数据；race/faction 在榜单响应里已包含。
        self._enrich_talents(players[:self.PROFILE_ENRICH_LIMIT])

        return players

    def _fetch_world_top_players(self, class_name, spec_name, season):
        """直接抓取 Raider.IO 世界榜。"""
        data = self.fetch_raiderio_top(
            class_name, spec_name, season, region='world', limit=self.PLAYER_RANKING_LIMIT, page=0
        )
        if not data:
            return []

        rankings = data.get('rankings', {}) or {}
        ranked_characters = rankings.get('rankedCharacters', []) if isinstance(rankings, dict) else []

        result = []
        for r in ranked_characters[:self.PLAYER_RANKING_LIMIT]:
            player = self._build_player_from_ranking(class_name, spec_name, r)
            if player:
                result.append(player)
        return result

    def _fetch_top_players_from_regions(self, class_name, spec_name, season):
        """兜底策略：按地区抓取第一页，再按分数聚合。"""
        all_players = []

        for region in self.REGIONS:
            data = self.fetch_raiderio_top(
                class_name, spec_name, season, region=region, limit=self.PLAYER_RANKING_LIMIT, page=0
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
        return all_players[:self.PLAYER_RANKING_LIMIT]

    def _backfill_ranking_sample_profiles(self, season_id, class_name, spec_name, limit=100):
        """按当前 ranking 样本定向补齐人物资料缓存，避免盲目扩大人物榜采集。"""
        from botend.models import PlayerSpecTopPlayer, SpecDungeonRanking, SpecRaidRanking

        sample_map = {}
        ranking_fields = ('region', 'realm', 'character_name', 'faction', 'gear_json', 'talent_build_code', 'talents_json', 'guild_name')

        dungeon_ids = SpecDungeonRanking.objects.filter(
            season_id=season_id,
            class_name=class_name,
            spec_name=spec_name,
        ).values_list('dungeon_id', flat=True).distinct()
        for dungeon_id in dungeon_ids:
            rows = SpecDungeonRanking.objects.filter(
                season_id=season_id,
                dungeon_id=dungeon_id,
                class_name=class_name,
                spec_name=spec_name,
            ).order_by('-dps').values(*ranking_fields)[:limit]
            self._add_profile_rows_to_sample_map(sample_map, rows)

        boss_ids = SpecRaidRanking.objects.filter(
            season_id=season_id,
            class_name=class_name,
            spec_name=spec_name,
        ).values_list('boss_id', flat=True).distinct()
        for boss_id in boss_ids:
            rows = SpecRaidRanking.objects.filter(
                season_id=season_id,
                boss_id=boss_id,
                class_name=class_name,
                spec_name=spec_name,
            ).order_by('-dps').values(*ranking_fields)[:limit]
            self._add_profile_rows_to_sample_map(sample_map, rows)

        if not sample_map:
            return 0

        existing = {}
        for profile in PlayerSpecTopPlayer.objects.filter(
            season_id=season_id,
            spec_name=spec_name,
        ):
            key = self._profile_identity_key(profile.region, profile.realm, profile.character_name)
            if key:
                existing[key] = profile

        updated = 0
        for key, row in sample_map.items():
            profile = existing.get(key)
            needs_profile = self._profile_needs_rio_enrichment(profile)
            original_values = None
            if not profile:
                profile = PlayerSpecTopPlayer(
                    season_id=season_id,
                    region=(row.get('region') or '').strip().lower(),
                    realm=(row.get('realm') or '').strip(),
                    character_name=(row.get('character_name') or '').strip(),
                    class_name=class_name,
                    spec_name=spec_name,
                    rank=None,
                    stats_crawl_status=0,
                )
            else:
                original_values = {
                    field: getattr(profile, field)
                    for field in self.PROFILE_UPDATE_FIELDS
                }

            self._apply_ranking_row_to_profile(profile, row)
            if needs_profile and (profile.region or '').lower() != 'cn':
                self._enrich_profile_model_from_raiderio(profile)
                time.sleep(0.2)

            if profile.pk:
                values = {
                    field: getattr(profile, field)
                    for field in self.PROFILE_UPDATE_FIELDS
                }
                values['last_updated'] = timezone.now()
                for field, value in (original_values or {}).items():
                    setattr(profile, field, value)
                changed = self._save_changed_profile(profile, values)
            else:
                profile.last_updated = timezone.now()
                self._save_profile_safely(profile)
                changed = True
            existing[key] = profile
            updated += int(changed)

        logger.info(f"[SpecDetailPlayer] 定向补齐 ranking 样本: {class_name}/{spec_name} {updated} 条")
        return updated

    def _add_profile_rows_to_sample_map(self, sample_map, rows):
        for row in rows:
            key = self._profile_identity_key(row.get('region'), row.get('realm'), row.get('character_name'))
            if key and key not in sample_map:
                sample_map[key] = row

    @staticmethod
    def _normalize_identity_part(value):
        value = (value or '').strip().lower()
        if not value:
            return ''
        normalized = unicodedata.normalize('NFKD', value)
        return ''.join(ch for ch in normalized if not unicodedata.combining(ch))

    @classmethod
    def _profile_identity_key(cls, region, realm, character_name):
        region = cls._normalize_identity_part(region)
        realm = cls._normalize_identity_part(realm)
        character_name = cls._normalize_identity_part(character_name)
        if not region or not realm or not character_name:
            return None
        return region, realm, character_name

    def _save_profile_safely(self, profile):
        try:
            with transaction.atomic():
                profile.save()
            return profile
        except IntegrityError:
            existing = self._find_existing_profile_for_unique_key(profile)
            if not existing:
                raise
            values = {
                field: getattr(profile, field)
                for field in self.PROFILE_UPDATE_FIELDS
            }
            values['last_updated'] = profile.last_updated
            self._save_changed_profile(existing, values)
            profile.id = existing.id
            return existing

    @staticmethod
    def _save_changed_profile(profile, values):
        """只持久化发生实质变化的字段；时间戳本身不触发写入。"""
        changed_fields = [
            field
            for field, value in values.items()
            if field != 'last_updated' and getattr(profile, field) != value
        ]
        if not changed_fields:
            return False
        for field in changed_fields:
            setattr(profile, field, values[field])
        if 'last_updated' in values:
            profile.last_updated = values['last_updated']
            changed_fields.append('last_updated')
        profile.save(update_fields=changed_fields)
        return True

    def _find_existing_profile_for_unique_key(self, profile):
        target_key = self._profile_identity_key(profile.region, profile.realm, profile.character_name)
        for existing in PlayerSpecTopPlayer.objects.filter(
            season_id=profile.season_id,
            spec_name=profile.spec_name,
        ):
            if self._profile_identity_key(existing.region, existing.realm, existing.character_name) == target_key:
                return existing
        return None

    @staticmethod
    def _profile_needs_rio_enrichment(profile):
        if not profile:
            return True
        gear = profile.gear_json or []
        has_profile_gear = any(
            isinstance(item, dict) and (item.get('gems_detail') or item.get('enchants_detail') or item.get('source') == 'raiderio_profile')
            for item in gear
        )
        return not has_profile_gear or not profile.talent_build_code

    def _apply_ranking_row_to_profile(self, profile, row):
        if row.get('faction') is not None and profile.faction in (None, ''):
            profile.faction = str(row.get('faction'))
        if row.get('guild_name') and not profile.guild_name:
            profile.guild_name = row.get('guild_name')
        if row.get('talent_build_code') and not profile.talent_build_code:
            profile.talent_build_code = row.get('talent_build_code')
        if row.get('talents_json') and not profile.talents_json:
            profile.talents_json = self._normalize_talent_nodes(row.get('talents_json'), profile.class_name, profile.spec_name)
        if row.get('gear_json') and not profile.gear_json:
            profile.gear_json = self._normalize_gear_list(row.get('gear_json'))

    @classmethod
    def _preserve_complete_talents_when_new_payload_is_downgrade(cls, existing, defaults, class_name, spec_name):
        """Keep older complete structured talents when a refresh payload regresses.

        Raider.IO/ranking refreshes may contain a valid build code but a stale
        structured talents list that is missing 12.1 apex nodes. If saved as-is,
        the next render can lose the previous complete structured state. Preserve
        the old talents_json only when it has current-spec apex points and the new
        structured payload does not; keep the newer build code because it can be
        decoded by the service with the compatible metadata version.
        """
        if not existing or not isinstance(defaults, dict):
            return False
        old_talents = getattr(existing, 'talents_json', None)
        new_talents = defaults.get('talents_json')
        if not old_talents or not new_talents:
            return False
        if not cls._talents_payload_has_spec_apex_points(old_talents, class_name, spec_name):
            return False
        if cls._talents_payload_has_spec_apex_points(new_talents, class_name, spec_name):
            return False
        defaults['talents_json'] = old_talents
        return True

    @staticmethod
    def _talents_payload_has_spec_apex_points(talents_json, class_name, spec_name):
        try:
            payload = TalentBuildCodeService.build_full_payload(
                class_name=class_name,
                spec_name=spec_name,
                talent_build_code='',
                talents_json=talents_json,
            )
        except Exception:
            return False
        for node in payload or []:
            if not isinstance(node, dict) or not node.get('is_apex_talent'):
                continue
            if int(node.get('points') or 0) > 0:
                return True
        return False

    def _enrich_profile_model_from_raiderio(self, profile):
        char_data = self.fetch_raiderio_character(profile.region, profile.realm, profile.character_name)
        if not char_data:
            return False

        gear_items = self._parse_rio_gear(char_data.get('gear'))
        if gear_items:
            profile.gear_json = self._normalize_gear_list(gear_items)
            item_level = self._coerce_item_level(char_data.get('gear'))
            if item_level:
                profile.item_level = item_level
        profile.achievement_points = char_data.get('achievement_points') or profile.achievement_points
        profile.profile_url = char_data.get('profile_url') or profile.profile_url
        profile.avatar_url = char_data.get('thumbnail_url') or profile.avatar_url
        profile.race = char_data.get('race') or profile.race
        profile.faction = char_data.get('faction') or profile.faction

        talent_build_code = self._extract_rio_talent_build_code(char_data)
        if talent_build_code:
            profile.talent_build_code = talent_build_code
        elif not profile.talents_json:
            structured = self._parse_rio_talents_from_profile(char_data)
            if structured:
                profile.talents_json = self._normalize_talent_nodes(structured, profile.class_name, profile.spec_name)
        return True

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
        talent_build_code = self._extract_rio_talent_build_code(char)

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
            'talent_build_code': talent_build_code,
            'talents': self._parse_rio_talents(talent_build_code),
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

    @staticmethod
    def _extract_rio_talent_build_code(char_data):
        """兼容 Raider.IO 新旧字段，提取天赋导入字符串。"""
        if not isinstance(char_data, dict):
            return ''
        loadout_obj = char_data.get('talentLoadout')
        if isinstance(loadout_obj, dict):
            loadout_text = loadout_obj.get('loadout_text') or loadout_obj.get('loadoutText')
            if loadout_text:
                return str(loadout_text).strip()
        return str(char_data.get('talentLoadoutText') or '').strip()

    def _enrich_talents(self, players):
        """为前排玩家补充结构化天赋+装备数据。
        
        执行顺序：
        1. 先调用 Raider.IO profile 获取装备+天赋（数据最完整，有 enchants_detail/gems_detail）
        2. 再从本地 ranking 表补充缺失的天赋（WCL 的天赋数据更可靠）
        """
        # 先为所有玩家调用 Raider.IO profile，获取装备+天赋（优先级最高）
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

                self._pending_build_code = None  # 重置 build_code

                gear_items = self._parse_rio_gear(char_data.get('gear'))
                if gear_items:
                    # 始终用 Raider.IO profile 覆盖（数据更完整：有 enchants_detail/gems_detail/slot）
                    player['gear'] = self._normalize_gear_list(gear_items)

                profile_ilvl = self._coerce_item_level(char_data.get('gear'))
                if profile_ilvl:
                    player['item_level'] = profile_ilvl

                player['achievement_points'] = char_data.get('achievement_points') or player.get('achievement_points')
                player['profile_url'] = char_data.get('profile_url') or player.get('profile_url')
                player['avatar_url'] = char_data.get('thumbnail_url') or player.get('avatar_url')
                player['race'] = char_data.get('race') or player.get('race')
                player['faction'] = char_data.get('faction') or player.get('faction')

                talent_build_code = self._extract_rio_talent_build_code(char_data)
                if talent_build_code:
                    player['talent_build_code'] = talent_build_code

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
                # 如果从 talentLoadout 提取到了 build_code，存储它
                if hasattr(self, '_pending_build_code') and self._pending_build_code:
                    player['talent_build_code'] = self._pending_build_code
                    self._pending_build_code = None

            except Exception as e:
                logger.warning(f"[SpecDetailPlayer] 获取天赋失败 {name}-{realm}@{region}: {e}")

            time.sleep(0.2)  # Raider.IO rate limit

        # 再从本地 ranking 表补充缺失的天赋（WCL 的 talents_json 更可靠）
        self._enrich_from_local_rankings(players)

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
                if getattr(ranking, 'talent_build_code', '') and not player.get('talent_build_code'):
                    player['talent_build_code'] = ranking.talent_build_code
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
        # 优先使用 talentLoadout。loadout_text 只适合作为导入代码保存；展示/统计仍需
        # 解析同一对象里的结构化 loadout，因为 12.1 顶峰天赋是同一个 TraitNode
        # 下多个 entry 的点数池（如 1+2+1=4），部分 Raider.IO loadout_text 解码会
        # 丢失这类点池状态。
        loadout_obj = char_data.get('talentLoadout')
        if loadout_obj and isinstance(loadout_obj, dict):
            loadout_text = loadout_obj.get('loadout_text', '')
            if loadout_text:
                self._pending_build_code = loadout_text
            structured_loadout = self._parse_rio_talent_loadout_nodes(loadout_obj.get('loadout'))
            if structured_loadout:
                return structured_loadout

        # 回退到旧的 talents 字段
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

    @staticmethod
    def _parse_rio_talent_loadout_nodes(loadout_entries):
        """解析 Raider.IO talentLoadout.loadout 结构化节点。

        Raider.IO 中 node.id 是 Blizzard TraitNode ID；node.entries[*].id 才是
        TraitNodeEntry ID。LMonitor 的 DB2 元数据里普通/顶峰渲染节点使用 entry id
        作为 node_id、TraitNode ID 作为 talent_id，因此这里必须同时保留两者。
        顶峰天赋（entry type=13）会以一个 TraitNode 下多个 entry 表示，总点数在
        外层 rank 上，max_points 为各 entry maxRanks 之和。
        """
        if not isinstance(loadout_entries, list):
            return []

        result = []
        for item in loadout_entries:
            if not isinstance(item, dict):
                continue
            node = item.get('node') or {}
            entries = node.get('entries') or []
            if not isinstance(node, dict) or not isinstance(entries, list) or not entries:
                continue

            try:
                entry_index = int(item.get('entryIndex') or 0)
            except (TypeError, ValueError):
                entry_index = 0
            if entry_index < 0 or entry_index >= len(entries):
                entry_index = 0
            selected_entry = entries[entry_index] or {}
            if not isinstance(selected_entry, dict):
                continue

            rank = SpecDetailPlayerMonitor._coerce_positive_int(item.get('rank'), default=1)
            if rank <= 0:
                continue
            max_points = SpecDetailPlayerMonitor._coerce_positive_int(selected_entry.get('maxRanks'), default=1)
            is_apex = any(SpecDetailPlayerMonitor._coerce_positive_int(entry.get('type'), default=0) == 13 for entry in entries if isinstance(entry, dict))
            if is_apex:
                max_points = sum(
                    SpecDetailPlayerMonitor._coerce_positive_int(entry.get('maxRanks'), default=1)
                    for entry in entries
                    if isinstance(entry, dict)
                ) or max_points

            spell = selected_entry.get('spell') or {}
            tree_type = SpecDetailPlayerMonitor._infer_tree_type_from_rio_node(node)
            result.append({
                'tree_type': tree_type,
                'node_id': selected_entry.get('id'),
                'talentID': node.get('id'),
                'talent_id': node.get('id'),
                'spellID': spell.get('id') if isinstance(spell, dict) else None,
                'spell_id': spell.get('id') if isinstance(spell, dict) else None,
                'display_spell_id': spell.get('id') if isinstance(spell, dict) else None,
                'name': spell.get('name', '') if isinstance(spell, dict) else '',
                'icon': spell.get('icon', '') if isinstance(spell, dict) else '',
                'points': rank,
                'max_points': max_points,
                'row': node.get('posY') or node.get('row'),
                'column': node.get('posX') or node.get('col'),
                'choice_selection': entry_index if len(entries) > 1 and not is_apex else None,
                'source': 'raiderio_loadout',
            })
        return result

    @staticmethod
    def _infer_tree_type_from_rio_node(node):
        subtree_id = SpecDetailPlayerMonitor._coerce_positive_int((node or {}).get('subTreeId'), default=0)
        if subtree_id:
            return 'hero'
        row = SpecDetailPlayerMonitor._coerce_positive_int((node or {}).get('row'), default=0)
        col = SpecDetailPlayerMonitor._coerce_positive_int((node or {}).get('col'), default=0)
        # Raider.IO 的 Warrior/Fury 等 profile 中职业树位于左侧低 col，专精树位于右侧高 col。
        # 后续 metadata merge 会以 node_id/talent_id 修正更精确的 tree_type；这里仅用于
        # 没有命中元数据时的合理初始分类。
        if row >= 11 and col >= 8:
            return 'hero_anchor'
        return 'spec' if col >= 10 else 'class'

    @staticmethod
    def _coerce_positive_int(value, default=0):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed >= 0 else default

    def _crawl_battlenet_stats(self, season_id, class_name=None, spec_name=None, retry_failed=False, limit=None):
        """补充 Battle.net 属性面板"""
        pending = PlayerSpecTopPlayer.objects.filter(season_id=season_id).exclude(region='cn')
        if class_name:
            pending = pending.filter(class_name=class_name)
        if spec_name:
            pending = pending.filter(spec_name=spec_name)
        if retry_failed:
            pending = pending.filter(stats_crawl_status__in=[0, -1, -2])
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
            if (node.get('tree_type') or '') == 'build_code':
                continue
            normalized.append({
                'tree_type': node.get('tree_type') or 'spec',
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
