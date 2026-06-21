# -*- coding: utf-8 -*-
"""
专精详情数据聚合 Service
从原始数据表计算统计值（avg/median/p25/p75/选取率/分布）
"""

from collections import Counter, defaultdict
from django.db.models import Avg, Max, Min, StdDev

from botend.models import (
    SeasonMeta, PlayerSpecTopPlayer, SpecDungeonRanking, SpecRaidRanking, WowItemSnapshot,
    WowTalentNodeMetadata,
)
from botend.constants.wow import CLASS_CN, SPEC_CN, SPEC_ICON, SPEC_ROLE, DUNGEON_CN, RAID_BOSS_CN, RAID_ZONE_CN, SLOT_CN, RACE_CN, ENCHANT_CN, GEM_STAT_CN, QUALITY_CN
from botend.wow.talents.parser import normalize_talent_payload
from botend.wow.talents.metadata import TalentMetadataProvider, dedupe_talent_option_nodes, normalize_talent_option_spell_id
from botend.wow.talents.models import TREE_COLUMNS, TalentBuildStateModel, TalentNodeModel, TalentTreeModel, TalentTreeSetModel
from botend.wow.talents.render import build_talent_render_model
from botend.wow.talents.view_model import build_talent_view_model
from botend.wow.talents.service import TalentBuildCodeService

import re


def _translate_gem_name(name):
    """将宝石英文名翻译为中文，如 '16 Crit & 7 Mast' → '16 暴击 & 7 精通'"""
    if not name:
        return name
    result = name
    for en, cn in GEM_STAT_CN.items():
        result = result.replace(en, cn)
    return result


def _translate_enchant_name(name):
    """将附魔英文名翻译为中文"""
    if not name:
        return name
    return ENCHANT_CN.get(name, name)


def _translate_quality(quality):
    """将品质值翻译为中文"""
    if quality is None:
        return ''
    return QUALITY_CN.get(quality, str(quality))


def _translate_race(race):
    """将种族英文名翻译为中文"""
    if not race:
        return race
    return RACE_CN.get(race, race)


def _aggregate_gems(gear_items):
    """从装备列表聚合宝石，按名称去重计数"""
    gem_counter = {}  # name -> {name, icon, id, count}
    for item in gear_items:
        gems = item.get('gems_detail') or []
        for g in gems:
            if not isinstance(g, dict):
                continue
            name = g.get('name') or f"#{g.get('id', '?')}"
            if name not in gem_counter:
                gem_counter[name] = {
                    'name': name,
                    'icon': _normalize_icon_name(g.get('icon', '')),
                    'id': g.get('id'),
                    'count': 0,
                }
            gem_counter[name]['count'] += 1
    result = sorted(gem_counter.values(), key=lambda x: -x['count'])
    return result


def _aggregate_enchants(gear_items):
    """从装备列表聚合附魔，按槽位展示"""
    result = []
    for item in gear_items:
        enchants = item.get('enchants_detail') or []
        for e in enchants:
            if not isinstance(e, dict):
                continue
            name = e.get('name') or f"#{e.get('id', '?')}"
            result.append({
                'name': name,
                'icon': _normalize_icon_name(e.get('icon', '')),
                'id': e.get('id'),
                'slot': item.get('slot', ''),
            })
    return result



def _coerce_item_id(value):
    try:
        parsed = int(str(value).strip())
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _wowhead_item_url(item_id):
    item_id = _coerce_item_id(item_id)
    return f'https://www.wowhead.com/cn/item={item_id}' if item_id else ''


def _has_cjk(value):
    """是否包含中文/中日韩字符。避免把英文 fallback 当作中文名显示。"""
    return bool(re.search(r'[\u3400-\u9fff]', str(value or '')))


def _collect_item_ids_from_records(records, include_gear=True, include_gems=True, include_enchants=True):
    ids = set()
    for record in records or []:
        gear = record.get('gear_json') or []
        if not isinstance(gear, list):
            continue
        for item in gear:
            if not isinstance(item, dict):
                continue
            if include_gear:
                item_id = _coerce_item_id(item.get('id') or item.get('itemID') or item.get('item_id'))
                if item_id:
                    ids.add(item_id)
            if include_gems:
                for gem in (item.get('gems_detail') or []) + (item.get('gems') or []):
                    if isinstance(gem, dict):
                        item_id = _coerce_item_id(gem.get('id'))
                        if item_id:
                            ids.add(item_id)
            if include_enchants:
                for ench in item.get('enchants_detail') or []:
                    if isinstance(ench, dict):
                        item_id = _coerce_item_id(ench.get('id'))
                        if item_id:
                            ids.add(item_id)
    if not ids:
        return {}
    try:
        return {
            int(row.item_id): row
            for row in WowItemSnapshot.objects.filter(item_id__in=ids)
        }
    except Exception:
        # 兼容迁移尚未执行的环境：页面先用 gear_json 中的原始名称渲染，部署 migrate 后自动读中文元数据。
        return {}


def _item_snapshot_payload(item_id, fallback_name='', fallback_icon='', fallback_description='', fallback_quality=0, snapshots=None):
    item_id = _coerce_item_id(item_id)
    snapshot = (snapshots or {}).get(item_id) if item_id else None
    name = fallback_name or ''
    description = fallback_description or ''
    icon = _normalize_icon_name(fallback_icon or '')
    quality = fallback_quality or 0
    if snapshot:
        name = snapshot.name or name
        description = snapshot.description or description
        icon = snapshot.icon or icon
        quality = snapshot.quality or quality
    raw_name_zh = snapshot.name_zh if snapshot else ''
    raw_description_zh = snapshot.description_zh if snapshot else ''
    # 旧数据曾把英文 fallback 写进 name_zh；只有真正含中文时才当中文显示。
    name_zh = raw_name_zh if _has_cjk(raw_name_zh) else ''
    description_zh = raw_description_zh if _has_cjk(raw_description_zh) else ''
    display_name = name_zh or name or (f'#{item_id}' if item_id else '')
    return {
        'id': item_id,
        'name': name,
        'name_zh': name_zh,
        'display_name': display_name,
        'description': description,
        'description_zh': description_zh,
        'display_description': description_zh or description,
        'icon': icon,
        'quality': quality,
        'wowhead_url': _wowhead_item_url(item_id),
    }


def _normalize_icon_name(icon):
    icon = str(icon or '').strip()
    if not icon:
        return ''
    icon = icon.split('?', 1)[0].strip()
    icon = icon.rsplit('/', 1)[-1].strip()
    while '.' in icon:
        base, ext = icon.rsplit('.', 1)
        if ext.lower() in {'jpg', 'jpeg', 'png', 'gif', 'webp'}:
            icon = base
            continue
        break
    return icon.strip()


class SpecStatsService:
    """所有页面数据的统一入口"""

    # ========== 通用 ==========

    @staticmethod
    def get_active_season():
        return SeasonMeta.objects.filter(is_active=True).first()

    @staticmethod
    def get_spec_nav(class_name, spec_name):
        """页面导航数据"""
        return {
            'class_name': class_name,
            'spec_name': spec_name,
            'class_cn': CLASS_CN.get(class_name, class_name),
            'spec_cn': SPEC_CN.get(spec_name, spec_name),
            'spec_icon': SPEC_ICON.get((class_name, spec_name), ''),
            'role': SPEC_ROLE.get((class_name, spec_name), 'dps'),
        }

    # ========== 人物榜 ==========

    @staticmethod
    def get_player_list(class_name, spec_name, season_id=None, page=1, page_size=100):
        """Top 20 玩家列表（分页）"""
        if not season_id:
            season = SeasonMeta.objects.filter(is_active=True).first()
            if not season:
                return {'players': [], 'total': 0, 'page': page, 'pages': 0}
            season_id = season.id

        qs = PlayerSpecTopPlayer.objects.filter(
            season_id=season_id, class_name=class_name, spec_name=spec_name
        ).order_by('rank')

        total = qs.count()
        pages = (total + page_size - 1) // page_size
        start = (page - 1) * page_size
        players = list(qs[start:start + page_size].values(
            'id', 'rank', 'character_name', 'realm', 'region', 'score',
            'faction', 'race', 'guild_name', 'item_level', 'avatar_url', 'profile_url', 'last_updated'
        ))

        updated_at = None
        if players:
            updated_at = players[0].get('last_updated')

        return {
            'players': players,
            'total': total,
            'page': page,
            'pages': pages,
            'updated_at': updated_at,
        }

    @staticmethod
    def get_player_detail(player_id):
        """单玩家完整详情"""
        player = PlayerSpecTopPlayer.objects.filter(id=player_id).first()
        if not player:
            return None

        talent_view = TalentBuildCodeService.build_api_view(
            talent_build_code=getattr(player, 'talent_build_code', ''),
            talents_json=player.talents_json or [],
            class_name=player.class_name,
            spec_name=player.spec_name,
        )
        talent_vm = talent_view.get('talent_view_model') or {}
        gear_payload = _resolve_player_gear(player)
        player_stats = player.stats_json or {}

        # 从 gear_json 计算平均装等
        gear_items = gear_payload.get('items') or []
        ilvls = [int(g.get('itemLevel', 0)) for g in gear_items if g.get('itemLevel')]
        avg_ilvl = round(sum(ilvls) / len(ilvls), 1) if ilvls else None

        # 统计宝石总数
        total_gems = sum(len(g.get('gems', [])) for g in gear_items)

        # 阵营中文
        faction_cn = {'alliance': '联盟', 'horde': '部落'}.get(
            (player.faction or '').lower(), player.faction or ''
        )

        # 种族中文
        race_cn = _translate_race(player.race)

        return {
            'id': player.id,
            'rank': player.rank,
            'character_name': player.character_name,
            'realm': player.realm,
            'region': player.region,
            'score': player.score,
            'faction': player.faction,
            'faction_cn': faction_cn,
            'race': player.race,
            'race_cn': race_cn,
            'gender': player.gender,
            'guild_name': player.guild_name,
            'realm_rank': player.realm_rank,
            'avatar_url': player.avatar_url,
            'profile_url': player.profile_url,
            'achievement_points': player.achievement_points,
            'item_level': player.item_level or avg_ilvl,
            'gear': gear_items,
            'gear_source': gear_payload['source'],
            'total_gems': total_gems,
            'talents': talent_vm['nodes'],
            'talent_groups': talent_vm['trees'],
            'talent_code': talent_view.get('talent_build_code', ''),
            'talent_build_code': talent_view.get('talent_build_code', ''),
            'has_talent_build_code': talent_view.get('has_talent_build_code', False),
            'talent_parse_status': talent_view.get('talent_parse_status', 'missing'),
            'talent_render_model': talent_view.get('talent_render_model') or {},
            'stats': player_stats,
            'stats_source': _describe_player_stats_source(player),
            'last_updated': player.last_updated,
            'aggregated_gems': _aggregate_gems(gear_items),
            'aggregated_enchants': _aggregate_enchants(gear_items),
        }

    # ========== M+ 副本统计 ==========

    @staticmethod
    def get_dungeon_overview(class_name, spec_name, season_id=None):
        """该专精在 8 个副本的统计概览"""
        if not season_id:
            season = SeasonMeta.objects.filter(is_active=True).first()
            if not season:
                return []
            season_id = season.id

        encounters = []
        season = SeasonMeta.objects.filter(id=season_id).first()
        if not season or not season.mplus_encounters:
            return []

        for enc in season.mplus_encounters:
            cn_name = _lookup_dungeon_cn(enc['name'])
            stats = SpecStatsService._compute_dungeon_stats(
                season_id, enc['id'], cn_name, class_name, spec_name
            )
            encounters.append(stats)

        return encounters

    @staticmethod
    def get_dungeon_detail(dungeon_id, class_name, spec_name, season_id=None):
        """单副本详情"""
        if not season_id:
            season = SeasonMeta.objects.filter(is_active=True).first()
            if not season:
                return None
            season_id = season.id

        qs = SpecDungeonRanking.objects.filter(
            season_id=season_id, dungeon_id=dungeon_id,
            class_name=class_name, spec_name=spec_name
        )
        if not qs.exists():
            return None

        dungeon_name = qs.first().dungeon_name
        cn_name = _lookup_dungeon_cn(dungeon_name)
        return SpecStatsService._compute_dungeon_stats(
            season_id, dungeon_id, cn_name, class_name, spec_name, full=True
        )

    @staticmethod
    def _compute_dungeon_stats(season_id, dungeon_id, dungeon_name, class_name, spec_name, full=False):
        """从原始数据计算副本统计"""
        qs = SpecDungeonRanking.objects.filter(
            season_id=season_id, dungeon_id=dungeon_id,
            class_name=class_name, spec_name=spec_name
        )

        stats = {
            'dungeon_id': dungeon_id,
            'dungeon_name': dungeon_name,
            'sample_size': 0,
        }

        selected_records = _select_dungeon_sample_records(qs, max_samples=100)
        if not selected_records:
            return stats

        # DPS 统计
        dps_list = sorted([r.get('dps') or 0 for r in selected_records])
        n = len(dps_list)

        stats['sample_size'] = n
        p25 = _percentile(dps_list, 25)
        p75 = _percentile(dps_list, 75)
        median = _percentile(dps_list, 50)
        dps_min = min(dps_list)
        dps_max = max(dps_list)
        dps_avg = sum(dps_list) / n if n else 0
        dps_stddev = _stddev(dps_list)
        dps_range = dps_max - dps_min if dps_max and dps_min else 1

        stats['dps'] = {
            'avg': dps_avg,
            'median': median,
            'max': dps_max,
            'min': dps_min,
            'p25': p25,
            'p75': p75,
            'stddev': dps_stddev,
            # 百分比字段（给前端 DPS 分布条用）
            'p25_pct': round((p25 - dps_min) / dps_range * 100, 1) if p25 and dps_range else 0,
            'iqr_pct': round((p75 - p25) / dps_range * 100, 1) if p75 and p25 and dps_range else 0,
            'median_pct': round((median - dps_min) / dps_range * 100, 1) if median and dps_range else 50,
        }

        # 钥石等级
        ks_list = sorted([r.get('keystone_level') for r in selected_records if r.get('keystone_level')])
        if ks_list:
            stats['keystone'] = {
                'avg': sum(ks_list) / len(ks_list),
                'max': max(ks_list),
                'min': min(ks_list),
            }

        # 通关时间
        ct_list = sorted([r.get('clear_time') for r in selected_records if r.get('clear_time')])
        if ct_list:
            ct_median = _percentile(ct_list, 50)
            ct_avg = sum(ct_list) // len(ct_list)
            stats['clear_time'] = {
                'avg': ct_avg,
                'median': ct_median,
                'avg_fmt': _ms_to_time(ct_avg),
                'median_fmt': _ms_to_time(ct_median),
            }

        # M+ 分数统计
        score_list = sorted([r.get('score') for r in selected_records if r.get('score')])
        if score_list:
            stats['score'] = {
                'avg': sum(score_list) / len(score_list),
                'median': _percentile(score_list, 50),
                'max': max(score_list),
                'min': min(score_list),
                'p25': _percentile(score_list, 25),
                'p75': _percentile(score_list, 75),
            }

        # 阵营分布
        faction_counter = Counter(r.get('faction') for r in selected_records if r.get('faction') is not None and r.get('faction') != -1)
        if faction_counter:
            total_factions = sum(faction_counter.values())
            stats['faction_distribution'] = {
                str(f): {'count': cnt, 'pct': round(cnt / total_factions * 100, 1)}
                for f, cnt in faction_counter.most_common()
            }

        # 天赋/装备热门度（概览也展示）
        records = selected_records
        talent_limit = 20 if full else 10
        gear_limit = 5 if full else 3
        usage_snapshot = _build_talent_usage_snapshot(records, class_name, spec_name)
        stats['talent_sample_size'] = usage_snapshot.get('total', 0)
        stats['talent_popularity'] = _compute_talent_popularity(records, top_n=talent_limit)
        stats['talent_usage'] = _compute_talent_usage(
            records,
            class_name,
            spec_name,
            top_n=talent_limit,
            snapshot=usage_snapshot,
        )
        stats['talent_popularity_tree'] = _compute_talent_popularity_tree(
            records,
            class_name,
            spec_name,
            top_n=talent_limit,
            snapshot=usage_snapshot,
        )
        stats['talent_build_popularity'] = _compute_talent_build_popularity(
            records,
            class_name,
            spec_name,
            top_n=20 if full else 5,
        )

        # 装备/宝石/附魔使用率：按当前详情页 ranking 样本统计（100 人里几个人使用）。
        # gear_detail_records 优先用人物榜 Raider.IO gear 回填，补齐 slot/gems_detail/enchants_detail；长度不变，分母仍是 ranking 样本数。
        gear_detail_records = _merge_player_profile_gear(records, season_id, class_name, spec_name)
        stats['gear_popularity'] = _compute_gear_popularity(gear_detail_records, top_n=gear_limit)
        stats['gem_popularity'] = _compute_gem_popularity(gear_detail_records, top_n=20)
        stats['enchant_popularity'] = _compute_enchant_popularity(gear_detail_records, top_n=20)

        # 人物属性/种族按当前详情页 ranking 样本回填，避免人物榜 Top20/Top200 与 100 人样本混用。
        player_detail_records = _merge_player_profile_fields(
            records,
            season_id,
            class_name,
            spec_name,
            fields=('stats_json', 'race'),
        )
        stats['secondary_stats'] = _compute_secondary_stats_distribution(player_detail_records)
        stats['race_distribution'] = _compute_race_distribution(player_detail_records)

        if full:
            # 详细模式：Top 5 玩家（与统计口径使用同一批筛选样本）
            full_records = [dict(r) for r in selected_records]
            stats['top5'] = sorted(full_records, key=lambda r: r['dps'] or 0, reverse=True)[:5]
            # 格式化 top5 通关时间
            for r in stats['top5']:
                if r.get('clear_time'):
                    r['clear_time_fmt'] = _ms_to_time(r['clear_time'])

        return stats

    # ========== 团本统计 ==========

    @staticmethod
    def get_raid_overview(class_name, spec_name, season_id=None):
        """该专精在团本各 Boss 的统计概览"""
        if not season_id:
            season = SeasonMeta.objects.filter(is_active=True).first()
            if not season:
                return []
            season_id = season.id
        season = SeasonMeta.objects.filter(id=season_id).first()
        if not season or not season.raid_encounters:
            return []
        # Group by zone if raid_zones data is available
        if season.raid_zones:
            zones = []
            for rz in season.raid_zones:
                zone_cn = RAID_ZONE_CN.get(rz.get('name', ''), rz.get('name', ''))
                zone_bosses = []
                for enc in rz.get('encounters', []):
                    cn_name = RAID_BOSS_CN.get(enc['name'], enc['name'])
                    stats = SpecStatsService._compute_raid_stats(
                        season_id, enc['id'], cn_name, class_name, spec_name
                    )
                    zone_bosses.append(stats)
                if zone_bosses:
                    zones.append({
                        'zone_id': rz.get('id'),
                        'zone_name': rz.get('name', ''),
                        'zone_cn': zone_cn,
                        'bosses': zone_bosses,
                    })
            return zones
        else:
            # Fallback: flat list (backward compat)
            bosses = []
            for enc in season.raid_encounters:
                cn_name = RAID_BOSS_CN.get(enc['name'], enc['name'])
                stats = SpecStatsService._compute_raid_stats(
                    season_id, enc['id'], cn_name, class_name, spec_name
                )
                bosses.append(stats)
            return [{'zone_id': 0, 'zone_name': '', 'zone_cn': '', 'bosses': bosses}]

    @staticmethod
    def get_raid_detail(boss_id, class_name, spec_name, season_id=None):
        """单 Boss 详情"""
        if not season_id:
            season = SeasonMeta.objects.filter(is_active=True).first()
            if not season:
                return None
            season_id = season.id

        qs = SpecRaidRanking.objects.filter(
            season_id=season_id, boss_id=boss_id,
            class_name=class_name, spec_name=spec_name
        )
        if not qs.exists():
            return None

        boss_name = qs.first().boss_name
        cn_name = RAID_BOSS_CN.get(boss_name, boss_name)
        stats = SpecStatsService._compute_raid_stats(
            season_id, boss_id, cn_name, class_name, spec_name, full=True
        )
        # Add zone info if available
        zone_rec = qs.first()
        if zone_rec and zone_rec.raid_zone_name:
            stats['raid_zone_id'] = zone_rec.raid_zone_id
            stats['raid_zone_name'] = zone_rec.raid_zone_name
            stats['raid_zone_cn'] = RAID_ZONE_CN.get(zone_rec.raid_zone_name, zone_rec.raid_zone_name)
        return stats

    @staticmethod
    def _compute_raid_stats(season_id, boss_id, boss_name, class_name, spec_name, full=False):
        """从原始数据计算团本统计"""
        qs = SpecRaidRanking.objects.filter(
            season_id=season_id, boss_id=boss_id,
            class_name=class_name, spec_name=spec_name
        )

        stats = {
            'boss_id': boss_id,
            'boss_name': boss_name,
            'sample_size': 0,
        }

        if not qs.exists():
            return stats

        dps_agg = qs.aggregate(avg=Avg('dps'), max=Max('dps'), min=Min('dps'))
        dps_list = sorted(qs.values_list('dps', flat=True))
        n = len(dps_list)

        stats['sample_size'] = n
        p25 = _percentile(dps_list, 25)
        p75 = _percentile(dps_list, 75)
        median = _percentile(dps_list, 50)
        dps_min = dps_agg['min']
        dps_max = dps_agg['max']
        dps_range = dps_max - dps_min if dps_max and dps_min else 1

        stats['dps'] = {
            'avg': dps_agg['avg'],
            'median': median,
            'max': dps_max,
            'min': dps_min,
            'p25': p25,
            'p75': p75,
            'p25_pct': round((p25 - dps_min) / dps_range * 100, 1) if p25 and dps_range else 0,
            'iqr_pct': round((p75 - p25) / dps_range * 100, 1) if p75 and p25 and dps_range else 0,
            'median_pct': round((median - dps_min) / dps_range * 100, 1) if median and dps_range else 50,
        }

        kt_list = sorted([v for v in qs.values_list('kill_time', flat=True) if v])
        if kt_list:
            kt_median = _percentile(kt_list, 50)
            kt_avg = sum(kt_list) // len(kt_list)
            stats['kill_time'] = {
                'avg': kt_avg,
                'median': kt_median,
                'avg_fmt': _ms_to_time(kt_avg),
                'median_fmt': _ms_to_time(kt_median),
            }

        # 阵营分布
        faction_counter = Counter(r for r in qs.values_list('faction', flat=True) if r is not None and r != -1)
        if faction_counter:
            total_factions = sum(faction_counter.values())
            stats['faction_distribution'] = {
                str(f): {'count': cnt, 'pct': round(cnt / total_factions * 100, 1)}
                for f, cnt in faction_counter.most_common()
            }

        # 天赋/装备热门度（概览也展示）
        records = list(qs.values('talents_json', 'talent_build_code', 'gear_json', 'faction', 'character_name', 'realm', 'region'))
        talent_limit = 20 if full else 10
        gear_limit = 5 if full else 3
        usage_snapshot = _build_talent_usage_snapshot(records, class_name, spec_name)
        stats['talent_sample_size'] = usage_snapshot.get('total', 0)
        stats['talent_popularity'] = _compute_talent_popularity(records, top_n=talent_limit)
        stats['talent_usage'] = _compute_talent_usage(
            records,
            class_name,
            spec_name,
            top_n=talent_limit,
            snapshot=usage_snapshot,
        )
        stats['talent_popularity_tree'] = _compute_talent_popularity_tree(
            records,
            class_name,
            spec_name,
            top_n=talent_limit,
            snapshot=usage_snapshot,
        )
        stats['talent_build_popularity'] = _compute_talent_build_popularity(
            records,
            class_name,
            spec_name,
            top_n=20 if full else 5,
        )

        # 装备/宝石/附魔使用率：按当前详情页 ranking 样本统计（100 人里几个人使用）。
        # gear_detail_records 优先用人物榜 Raider.IO gear 回填，补齐 slot/gems_detail/enchants_detail；长度不变，分母仍是 ranking 样本数。
        gear_detail_records = _merge_player_profile_gear(records, season_id, class_name, spec_name)
        stats['gear_popularity'] = _compute_gear_popularity(gear_detail_records, top_n=gear_limit)
        stats['gem_popularity'] = _compute_gem_popularity(gear_detail_records, top_n=20)
        stats['enchant_popularity'] = _compute_enchant_popularity(gear_detail_records, top_n=20)

        # 人物属性/种族按当前详情页 ranking 样本回填，避免人物榜 Top20/Top200 与 100 人样本混用。
        player_detail_records = _merge_player_profile_fields(
            records,
            season_id,
            class_name,
            spec_name,
            fields=('stats_json', 'race'),
        )
        stats['secondary_stats'] = _compute_secondary_stats_distribution(player_detail_records)
        stats['race_distribution'] = _compute_race_distribution(player_detail_records)

        if full:
            full_records = list(qs.values('talents_json', 'gear_json', 'faction', 'guild_name',
                                      'character_name', 'realm', 'region', 'dps', 'kill_time'))
            stats['top5'] = sorted(full_records, key=lambda r: r['dps'] or 0, reverse=True)[:5]
            # 格式化 top5 击杀时间
            for r in stats['top5']:
                if r.get('kill_time'):
                    r['kill_time_fmt'] = _ms_to_time(r['kill_time'])

        return stats


# ========== 辅助函数 ==========

def _percentile(sorted_list, pct):
    """计算百分位数（已排序列表）"""
    if not sorted_list:
        return None
    k = (len(sorted_list) - 1) * pct / 100
    f = int(k)
    c = f + 1
    if c >= len(sorted_list):
        return sorted_list[-1]
    return sorted_list[f] + (k - f) * (sorted_list[c] - sorted_list[f])


def _stddev(values):
    """计算样本标准差，避免筛选后的内存样本再走 ORM aggregate。"""
    if len(values) <= 1:
        return 0
    avg = sum(values) / len(values)
    variance = sum((v - avg) ** 2 for v in values) / (len(values) - 1)
    return variance ** 0.5


def _select_dungeon_sample_records(qs, max_samples=100):
    """
    为 M+ 聚合选择最终样本。

    规则：先按钥石层数从高到低分层，每层按 DPS 从高到低排序；每层只保留 DPS >= 该层中位数的日志，
    再从高层到低层累积，按 region+realm+character_name 去重，最终最多 max_samples 个玩家样本。
    """
    fields = (
        'talents_json', 'talent_build_code', 'gear_json', 'faction', 'guild_name',
        'character_name', 'realm', 'region', 'dps', 'keystone_level', 'clear_time', 'score',
    )
    rows = list(qs.values(*fields))
    if not rows:
        return []

    by_level = defaultdict(list)
    for row in rows:
        level = row.get('keystone_level') or 0
        by_level[level].append(row)

    selected = []
    seen_players = set()
    for level in sorted(by_level.keys(), reverse=True):
        level_rows = sorted(by_level[level], key=lambda r: r.get('dps') or 0, reverse=True)
        dps_values = sorted([r.get('dps') or 0 for r in level_rows])
        median_dps = _percentile(dps_values, 50)
        for row in level_rows:
            if len(selected) >= max_samples:
                return selected
            if median_dps is not None and (row.get('dps') or 0) < median_dps:
                continue
            player_key = (
                (row.get('region') or '').strip().lower(),
                (row.get('realm') or '').strip().lower(),
                (row.get('character_name') or '').strip().lower(),
            )
            if player_key in seen_players:
                continue
            seen_players.add(player_key)
            selected.append(row)

    return selected


def _ms_to_time(ms):
    """毫秒转 M:SS 格式"""
    if not ms:
        return None
    total_seconds = int(ms / 1000)
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return f"{minutes}:{seconds:02d}"


def _lookup_dungeon_cn(name):
    """Robust dungeon name → Chinese translation lookup.

    Tries direct match, case-insensitive match, and slug-to-name match
    (e.g. 'algeth-ar-academy' → "Algeth'ar Academy").
    """
    if not name:
        return name

    # 1. Direct match
    if name in DUNGEON_CN:
        return DUNGEON_CN[name]

    # 2. Case-insensitive match
    name_lower = name.lower()
    for key, val in DUNGEON_CN.items():
        if key.lower() == name_lower:
            return val

    # 3. Slug-to-name match (normalise away hyphens, apostrophes, spaces, commas)
    def _norm(s):
        return s.lower().replace("'", "").replace("-", "").replace(" ", "").replace(",", "")

    name_norm = _norm(name)
    for key, val in DUNGEON_CN.items():
        if _norm(key) == name_norm:
            return val

    return name


def _talent_tree_label(tree_type):
    mapping = {
        'class': '职业天赋',
        'spec': '专精天赋',
        'hero': '英雄天赋',
        'build_code': '导入代码',
    }
    return mapping.get(tree_type or 'spec', tree_type or '天赋')


def _talent_tree_render_title(tree_type, hero_index=None):
    if tree_type == 'hero' and hero_index:
        return f'英雄天赋 {hero_index}'
    return _talent_tree_label(tree_type)


def _hero_subtree_display_title(class_name, spec_name, subtree_id, hero_index=None):
    if subtree_id:
        anchor = WowTalentNodeMetadata.objects.filter(
            class_name=class_name or '',
            spec_name=spec_name or '',
            tree_type='hero_anchor',
            db2_subtree_id=subtree_id,
        ).exclude(name='').values('name', 'name_zh').first()
        if anchor:
            return anchor.get('name_zh') or anchor.get('name') or _talent_tree_render_title('hero', hero_index)
    return _talent_tree_render_title('hero', hero_index)


def _hero_subtree_sort_key(subtree_key, nodes):
    count_score = sum((getattr(node, 'count', 0) or 0) for node in nodes)
    pct_score = sum((getattr(node, 'usage_pct', 0) or 0) for node in nodes)
    first_column = min((node.column or 999999 for node in nodes), default=999999)
    return (first_column, -count_score, -pct_score, -len(nodes), subtree_key)


def _has_valid_talent_payload(record):
    """Return True when a ranking record contains real talent nodes.

    WCL M+ rankings occasionally return empty combatant talent data for an
    otherwise valid DPS ranking. Those rows should remain in DPS/gear samples,
    but must not dilute talent usage percentages.
    """
    for raw in record.get('talents_json') or []:
        if isinstance(raw, dict) and raw.get('tree_type') != 'build_code':
            return True
    return False


def _valid_talent_records(records):
    """Filter ranking records to rows that can contribute to talent stats."""
    return [record for record in (records or []) if _has_valid_talent_payload(record)]


def _build_talent_usage_snapshot(records, class_name, spec_name):
    """聚合统计页热门天赋所需的节点、使用率与父子连线。"""
    records = _valid_talent_records(records)
    total = len(records)
    provider = TalentMetadataProvider()
    usage = {}
    canonical_nodes = {}
    parent_edges = defaultdict(Counter)

    hero_subtree_counts = Counter()

    for record in records:
        record_nodes = {}
        identity_lookup = {}
        for raw in record.get('talents_json') or []:
            node = _normalize_stats_talent_node(raw, provider, class_name, spec_name)
            if not node or node.tree_type == 'build_code':
                continue
            node_key = _build_talent_node_key(node)
            if not node_key:
                continue
            existing = record_nodes.get(node_key)
            if existing is None or _score_talent_node(node) >= _score_talent_node(existing):
                record_nodes[node_key] = node

        selected_hero_subtrees = {
            node.db2_subtree_id
            for node in record_nodes.values()
            if node.tree_type == 'hero' and node.db2_subtree_id
        }
        for subtree_id in selected_hero_subtrees:
            hero_subtree_counts[subtree_id] += 1

        for node_key, node in record_nodes.items():
            usage_item = usage.setdefault(node_key, {
                'node_key': node_key,
                'tree_type': node.tree_type or 'spec',
                'tree_label': _talent_tree_label(node.tree_type),
                'spell_id': node.spell_id,
                'talent_id': node.talent_id,
                'node_id': node.node_id,
                'name': node.name or (f"技能ID {node.spell_id or node.talent_id or node.node_id}"),
                'icon': node.icon or '',
                'count': 0,
            })
            usage_item['count'] += 1
            if node_key not in canonical_nodes or _score_talent_node(node) >= _score_talent_node(canonical_nodes[node_key]):
                canonical_nodes[node_key] = node
                usage_item.update({
                    'spell_id': node.spell_id,
                    'talent_id': node.talent_id,
                    'node_id': node.node_id,
                    'name': node.name or usage_item['name'],
                    'icon': node.icon or usage_item['icon'],
                })
            _register_talent_identity(identity_lookup, node, node_key)

        for node_key, node in record_nodes.items():
            for raw_parent in node.parents or []:
                parent_id = _coerce_positive_int(raw_parent)
                parent_key = identity_lookup.get(parent_id) if parent_id is not None else None
                if not parent_key or parent_key == node_key:
                    continue
                parent_edges[node_key][parent_key] += 1

    usage_list = []
    for node_key, item in usage.items():
        pct = round(item['count'] / total * 100, 1) if total else 0
        usage_list.append({
            **item,
            'usage_pct': pct,
            'pct': pct,
        })

    usage_list.sort(key=lambda item: (-item['usage_pct'], item['name']))
    return {
        'total': total,
        'usage_list': usage_list,
        'usage_map': {item['node_key']: item for item in usage_list},
        'canonical_nodes': canonical_nodes,
        'parent_edges': parent_edges,
        'hero_subtree_counts': dict(hero_subtree_counts),
    }


def _compute_talent_popularity(records, top_n=20):
    """计算天赋选取率（以 spellID 为 key）"""
    records = _valid_talent_records(records)
    talent_counts = Counter()
    talent_info = {}  # spellID → {name, icon}
    total = len(records)

    for r in records:
        talents = normalize_talent_payload(r.get('talents_json') or []).get('nodes', [])
        for t in talents:
            if t.get('tree_type') == 'build_code':
                continue
            # 优先用 spellID（Wowhead 用），fallback 到 talentID
            sid = t.get('spell_id') or t.get('talent_id')
            if sid:
                talent_counts[sid] += 1
                if sid not in talent_info:
                    talent_info[sid] = {
                        'name': t.get('name', ''),
                        'icon': t.get('icon', ''),
                    }

    # 按选取率降序
    result = {}
    for sid, count in talent_counts.most_common(top_n):
        info = talent_info.get(sid, {})
        result[str(sid)] = {
            'count': count,
            'pct': round(count / total * 100, 1) if total else 0,
            'name': info.get('name', ''),
            'icon': info.get('icon', ''),
        }
    return result


def _compute_talent_usage(records, class_name, spec_name, top_n=20, snapshot=None):
    """返回更适合页面展示的天赋使用率列表。"""
    snapshot = snapshot or _build_talent_usage_snapshot(records, class_name, spec_name)
    return [dict(item) for item in snapshot['usage_list'][:top_n]]


def _talent_build_record_state(record, provider, class_name, spec_name):
    """Return selected talent node keys and display metadata for one ranking row."""
    nodes_by_key = {}
    hero_nodes_by_subtree = defaultdict(list)
    for raw in record.get('talents_json') or []:
        node = _normalize_stats_talent_node(raw, provider, class_name, spec_name)
        if not node or node.tree_type == 'build_code':
            continue
        node_key = _build_talent_node_key(node)
        if not node_key:
            continue
        if node.tree_type == 'hero':
            hero_nodes_by_subtree[getattr(node, 'db2_subtree_id', 0) or 0].append(node)
            continue
        existing = nodes_by_key.get(node_key)
        if existing is None or _score_talent_node(node) >= _score_talent_node(existing):
            nodes_by_key[node_key] = node

    hero_summary = _build_hero_talent_summary(hero_nodes_by_subtree, class_name, spec_name)
    return {
        'keys': set(nodes_by_key.keys()),
        'nodes': {
            node_key: {
                'node_key': node_key,
                'tree_type': node.tree_type or 'spec',
                'tree_label': _talent_tree_label(node.tree_type),
                'spell_id': node.spell_id,
                'talent_id': node.talent_id,
                'node_id': node.node_id,
                'name': node.name or (f"技能ID {node.spell_id or node.talent_id or node.node_id}"),
                'icon': node.icon or '',
            }
            for node_key, node in nodes_by_key.items()
        },
        'hero_talent_summary': hero_summary,
    }


def _build_hero_talent_summary(hero_nodes_by_subtree, class_name, spec_name):
    """Summarize selected hero subtree names for a build row."""
    if not hero_nodes_by_subtree:
        return []

    subtree_ids = [subtree_id for subtree_id in hero_nodes_by_subtree.keys() if subtree_id]
    anchor_names = {}
    if subtree_ids:
        anchors = WowTalentNodeMetadata.objects.filter(
            class_name=class_name or '',
            spec_name=spec_name or '',
            tree_type='hero_anchor',
            db2_subtree_id__in=subtree_ids,
        ).exclude(name='').values('db2_subtree_id', 'name', 'name_zh')
        for anchor in anchors:
            anchor_names[anchor['db2_subtree_id']] = anchor.get('name_zh') or anchor.get('name') or ''

    summary = []
    for subtree_id, nodes in sorted(hero_nodes_by_subtree.items(), key=lambda item: item[0] or 0):
        node_names = [node.name for node in nodes if node.name]
        fallback_name = node_names[0] if node_names else (f"英雄天赋 {subtree_id}" if subtree_id else '英雄天赋')
        summary.append({
            'subtree_id': subtree_id,
            'name': anchor_names.get(subtree_id) or fallback_name,
            'selected_count': len(nodes),
        })
    return summary


def _compute_talent_build_popularity(records, class_name, spec_name, top_n=20):
    """按天赋导入字符串聚合，并输出与最热门模板字符串的差异。"""
    provider = TalentMetadataProvider()
    build_counter = Counter()
    build_states = {}
    total = 0
    first_seen_order = {}

    for record in _valid_talent_records(records):
        build_code = TalentBuildCodeService.extract_build_code(
            record.get('talent_build_code', ''),
            record.get('talents_json') or [],
        )
        if not build_code:
            continue
        total += 1
        if build_code not in first_seen_order:
            first_seen_order[build_code] = len(first_seen_order)
        build_counter[build_code] += 1
        if build_code not in build_states:
            build_states[build_code] = _talent_build_record_state(record, provider, class_name, spec_name)

    if not build_counter:
        return {
            'total': 0,
            'template_code': '',
            'template_count': 0,
            'builds': [],
        }

    ordered = sorted(build_counter.items(), key=lambda item: (-item[1], first_seen_order.get(item[0], 0)))[:top_n]
    template_code, template_count = ordered[0]
    template_state = build_states.get(template_code) or {'keys': set(), 'nodes': {}}
    template_keys = template_state.get('keys') or set()
    template_nodes = template_state.get('nodes') or {}

    def _node_payload(node_map, node_key):
        return dict(node_map.get(node_key) or template_nodes.get(node_key) or {'node_key': node_key, 'name': node_key, 'icon': ''})

    builds = []
    for index, (build_code, count) in enumerate(ordered, start=1):
        state = build_states.get(build_code) or {'keys': set(), 'nodes': {}}
        keys = state.get('keys') or set()
        nodes = state.get('nodes') or {}
        added_keys = sorted(keys - template_keys, key=lambda key: (nodes.get(key, {}).get('tree_type', ''), nodes.get(key, {}).get('name', key)))
        missing_keys = sorted(template_keys - keys, key=lambda key: (template_nodes.get(key, {}).get('tree_type', ''), template_nodes.get(key, {}).get('name', key)))
        builds.append({
            'rank': index,
            'code': build_code,
            'count': count,
            'pct': round(count / total * 100, 1) if total else 0,
            'is_template': build_code == template_code,
            'hero_talent_summary': state.get('hero_talent_summary') or [],
            'diff_count': len(added_keys) + len(missing_keys),
            'added_talents': [_node_payload(nodes, key) for key in added_keys],
            'missing_talents': [_node_payload(template_nodes, key) for key in missing_keys],
        })

    return {
        'total': total,
        'template_code': template_code,
        'template_count': template_count,
        'builds': builds,
    }


def _compute_talent_popularity_tree(records, class_name, spec_name, top_n=20, snapshot=None):
    """把热门天赋聚合成可直接给模板消费的 render_model。"""
    snapshot = snapshot or _build_talent_usage_snapshot(records, class_name, spec_name)
    usage_list = snapshot.get('usage_list') or []
    usage_map = snapshot.get('usage_map') or {}
    hero_subtree_counts = snapshot.get('hero_subtree_counts') or {}
    total_samples = snapshot.get('total', 0)
    highlighted_keys = [item['node_key'] for item in usage_list[:top_n] if item.get('node_key')]

    # 使用全量节点作为底板
    provider = TalentMetadataProvider()
    full_tree_nodes = provider.get_full_tree_nodes(class_name, spec_name)
    if not full_tree_nodes:
        return {}
    full_tree_nodes = _filter_spec_nodes_by_usage_component(full_tree_nodes, usage_map)

    # 展示层去重/合并：同一坐标视为同一展示节点
    # 按 (tree_type, db2_subtree_id, row, column) 分组，row/column 缺失时用 identity 兜底
    display_groups = defaultdict(list)

    for raw_node in full_tree_nodes:
        tree_type = raw_node.get('tree_type') or 'spec'
        if tree_type == 'hero_anchor':
            continue

        node_id = raw_node.get('node_id')
        talent_id = raw_node.get('talent_id')
        spell_id = raw_node.get('spell_id')
        key = node_id or talent_id or spell_id
        if not key:
            continue

        # 构建展示坐标 key：优先用 row/column，缺失时用 identity 兜底避免乱合并
        row = raw_node.get('row')
        column = raw_node.get('column')
        db2_subtree_id = raw_node.get('db2_subtree_id', 0) or 0

        if row is not None and column is not None:
            display_key = (tree_type, db2_subtree_id, row, column)
        else:
            display_key = (tree_type, db2_subtree_id, 'identity', key)

        display_groups[display_key].append(raw_node)

    # 将全量节点转换为 TalentNodeModel 并按 tree_type 分组
    grouped_nodes = defaultdict(list)
    node_key_map = {}  # node_key -> node，用于快速查找

    for display_key, raw_nodes in display_groups.items():
        # 为每个 raw_node 构建 node_key 并查找 usage
        raw_nodes = dedupe_talent_option_nodes(raw_nodes)
        candidates = []
        for raw_node in raw_nodes:
            tree_type = raw_node.get('tree_type') or 'spec'
            node_id = raw_node.get('node_id')
            talent_id = raw_node.get('talent_id')
            spell_id = raw_node.get('spell_id')
            key = node_id or talent_id or spell_id
            node_key = f'{tree_type}:{key}'
            candidate_keys = _talent_usage_candidate_keys(tree_type, raw_node)
            usage_item = _best_talent_usage_item(candidate_keys, usage_map)
            is_highlighted = any(candidate_key in highlighted_keys for candidate_key in candidate_keys)
            usage_pct = usage_item.get('usage_pct', 0)
            usage_count = usage_item.get('count', 0)
            has_icon = bool(raw_node.get('icon'))
            has_name = bool(raw_node.get('name'))

            candidates.append({
                'raw_node': raw_node,
                'node_key': node_key,
                'is_highlighted': is_highlighted,
                'usage_pct': usage_pct,
                'usage_count': usage_count,
                'has_icon': has_icon,
                'has_name': has_name,
            })

        # 选择 base 节点：优先 highlighted，其次 usage_pct/count 最高，其次有 icon/name
        candidates.sort(key=lambda c: (
            c['is_highlighted'],
            c['usage_pct'],
            c['usage_count'],
            c['has_icon'],
            c['has_name'],
        ), reverse=True)

        base_candidate = candidates[0]
        base_raw_node = base_candidate['raw_node']
        base_node_key = base_candidate['node_key']
        # 同一展示坐标的候选节点只渲染一个图标；使用率/高亮应取该坐标所有候选的最大值。
        # 否则二选一或 hero 节点的 base key 未命中时会显示成全灰 0%。
        display_usage_pct = max((c['usage_pct'] or 0) for c in candidates)
        display_usage_count = max((c['usage_count'] or 0) for c in candidates)
        base_is_highlighted = any(c['is_highlighted'] for c in candidates) or display_usage_count > 0

        # 构建 base 节点
        node = TalentNodeModel.from_raw({
            **base_raw_node,
            'points': 1 if base_is_highlighted else 0,
            'selected': base_is_highlighted,
        })
        node.count = display_usage_count
        node.usage_pct = display_usage_pct
        node.pct = display_usage_pct

        # 合并组中其它节点作为 choice_options
        choice_options = []
        seen_option_keys = set()

        for candidate in candidates:
            option_raw = candidate['raw_node']
            option_spell_id = normalize_talent_option_spell_id(option_raw) or option_raw.get('spell_id')
            option_key = f"{option_raw.get('node_id')}:{option_raw.get('talent_id')}:{option_spell_id}"
            if option_key not in seen_option_keys:
                seen_option_keys.add(option_key)
                option_usage = _best_talent_usage_item(
                    _talent_usage_candidate_keys(option_raw.get('tree_type') or 'spec', option_raw),
                    usage_map,
                )
                option_usage_pct = option_usage.get('usage_pct', 0)
                choice_options.append({
                    'node_id': option_raw.get('node_id'),
                    'talent_id': option_raw.get('talent_id'),
                    'spell_id': option_spell_id,
                    'display_spell_id': option_raw.get('display_spell_id') or option_spell_id,
                    'name': option_raw.get('name', ''),
                    'icon': option_raw.get('icon', ''),
                    'description': option_raw.get('description', '') or '',
                    'description_zh': option_raw.get('description_zh', '') or '',
                    'count': option_usage.get('count', 0),
                    'usage_pct': option_usage_pct,
                    'pct': option_usage_pct,
                    'is_active': (option_usage.get('count', 0) or 0) > 0,
                })

        # 若 option 数 > 1，设置 is_choice_node=True
        if len(choice_options) > 1:
            node.is_choice_node = True
            node.choice_options = choice_options

        tree_type = base_raw_node.get('tree_type') or 'spec'
        grouped_nodes[tree_type].append(node)
        node_key_map[base_node_key] = node
        for candidate in candidates:
            node_key_map[candidate['node_key']] = node

    # 聚合页展示两棵英雄天赋树：与个人页不同，不再只保留使用量最高的一棵。
    hero_subtrees = {}
    if 'hero' in grouped_nodes:
        hero_nodes = grouped_nodes.pop('hero')
        hero_subtrees = _group_hero_subtrees_by_column(hero_nodes)
        hero_subtrees = _filter_hero_subtrees_for_spec(
            hero_subtrees,
            class_name,
            spec_name,
            hero_subtree_counts.keys(),
        )

    # 构建树结构
    trees = []
    ordered_tree_groups = []
    for tree_type in _iter_render_tree_types(grouped_nodes):
        ordered_tree_groups.append((tree_type, grouped_nodes[tree_type], None))
        if tree_type == 'class' and hero_subtrees:
            sorted_hero_items = sorted(
                hero_subtrees.items(),
                key=lambda item: _hero_subtree_sort_key(item[0], item[1]),
            )
            for index, (subtree_key, nodes) in enumerate(sorted_hero_items[:2], start=1):
                ordered_tree_groups.append(('hero', nodes, index))

    if hero_subtrees and not any(tree_type == 'hero' for tree_type, _, _ in ordered_tree_groups):
        sorted_hero_items = sorted(
            hero_subtrees.items(),
            key=lambda item: _hero_subtree_sort_key(item[0], item[1]),
        )
        for index, (subtree_key, nodes) in enumerate(sorted_hero_items[:2], start=1):
            ordered_tree_groups.append(('hero', nodes, index))

    hero_tree_meta = []

    for tree_type, tree_nodes, hero_index in ordered_tree_groups:
        nodes = sorted(
            tree_nodes,
            key=lambda item: (
                item.row if item.row is not None else 999,
                item.column if item.column is not None else 999,
                item.node_id or item.talent_id or item.spell_id or 0,
                item.name or '',
            ),
        )
        subtree_id = None
        subtree_count = 0
        subtree_usage_pct = 0
        subtree_title = _talent_tree_render_title(tree_type, hero_index)
        if tree_type == 'hero':
            subtree_ids = [node.db2_subtree_id for node in nodes if node.db2_subtree_id]
            subtree_id = Counter(subtree_ids).most_common(1)[0][0] if subtree_ids else None
            if subtree_id is not None:
                subtree_count = hero_subtree_counts.get(subtree_id, 0)
                subtree_usage_pct = round(subtree_count / total_samples * 100, 1) if total_samples else 0
                subtree_title = _hero_subtree_display_title(class_name, spec_name, subtree_id, hero_index)
        default_columns = TREE_COLUMNS.get(tree_type, 8)
        rows = [node.row for node in nodes if node.row is not None]
        columns = [node.column for node in nodes if node.column is not None]
        if tree_type == 'hero':
            hero_tree_meta.append({
                'subtree_id': subtree_id,
                'subtree_count': subtree_count,
                'subtree_usage_pct': subtree_usage_pct,
                'pct': subtree_usage_pct,
            })
        trees.append(TalentTreeModel(
            tree_type=tree_type,
            title=subtree_title,
            nodes=nodes,
            grid_columns=max([default_columns, *columns]) if columns else default_columns,
            grid_rows=max(rows) if rows else max(1, (len(nodes) + default_columns - 1) // default_columns),
            synthetic_layout=not any(rows or columns),
        ))

    # 构建 build_state
    highlighted_node_keys = set()
    for node_key in highlighted_keys:
        if node_key in node_key_map:
            node = node_key_map[node_key]
            highlighted_node_keys.add(str(node.key))

    build_state = TalentBuildStateModel(
        source_type='stats',
        source_id=':'.join(part for part in [class_name, spec_name, 'popularity'] if part),
        selected_nodes=highlighted_node_keys,
        node_ranks={str(key): 1 for key in highlighted_node_keys},
    )

    # 构建 render_model
    render_model = build_talent_render_model(
        tree_set=TalentTreeSetModel(
            set_key=':'.join(part for part in [class_name, spec_name] if part),
            class_name=class_name,
            spec_name=spec_name,
            trees=trees,
            layout_mode='three-column',
            meta={},
        ),
        build_state=build_state,
    ).to_dict()

    for tree in render_model.get('trees', []) or []:
        if tree.get('tree_type') != 'hero' or not hero_tree_meta:
            continue
        tree.update(hero_tree_meta.pop(0))

    # 附加使用率数据
    _attach_usage_to_render_model(render_model, usage_map, highlighted_keys)

    total_nodes = sum(len(tree.nodes) for tree in trees)
    preserved_parent_edges = 0
    for tree in render_model.get('trees', []) or []:
        preserved_parent_edges += len(tree.get('paths') or [])
    return {
        'sample_size': snapshot.get('total', 0),
        'highlighted_node_count': len(highlighted_keys),
        'rendered_node_count': total_nodes,
        'preserved_parent_edges': preserved_parent_edges,
        'render_model': render_model,
        'usage': [dict(item) for item in usage_list[:top_n]],
    }


def _normalize_stats_talent_node(raw, provider, class_name, spec_name):
    if isinstance(raw, str) or not isinstance(raw, dict):
        return None
    node_data = {
        'tree_type': raw.get('tree_type') or raw.get('treeType') or 'spec',
        'talent_code': raw.get('talent_code', ''),
        'node_id': raw.get('node_id') or raw.get('nodeID') or raw.get('talent_id') or raw.get('talentID') or raw.get('spell_id') or raw.get('spellID'),
        'talent_id': raw.get('talent_id') or raw.get('talentID'),
        'spell_id': raw.get('spell_id') or raw.get('spellID') or raw.get('talent_id') or raw.get('talentID'),
        'name': raw.get('name') or '',
        'icon': raw.get('icon', ''),
        'points': raw.get('points', 0) or 0,
        'max_points': raw.get('max_points') or raw.get('maxPoints'),
        'row': raw.get('row') if raw.get('row') is not None else raw.get('tier'),
        'column': raw.get('column'),
        'selected': raw.get('selected', False),
        'source': raw.get('source', 'stats'),
        'parents': list(raw.get('parents') or raw.get('parents_json') or []),
        'db2_subtree_id': raw.get('db2_subtree_id') or raw.get('db2SubtreeID') or 0,
    }
    node_data = provider.merge_into_node(node_data, class_name=class_name, spec_name=spec_name)
    node = TalentNodeModel.from_raw(node_data)
    if (node.tree_type or '') == 'hero_anchor':
        return None
    if node.key is None:
        return None
    return node


def _build_talent_node_key(node):
    node_identity = node.key
    if node_identity is None:
        return ''
    return f'{node.tree_type or "spec"}:{node_identity}'


def _talent_usage_candidate_keys(tree_type, raw_node):
    keys = []
    seen = set()
    for value in (
        raw_node.get('node_id'),
        raw_node.get('talent_id'),
        raw_node.get('spell_id'),
        raw_node.get('display_spell_id'),
    ):
        value = _coerce_positive_int(value)
        if value is None:
            continue
        key = f'{tree_type}:{value}'
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def _best_talent_usage_item(candidate_keys, usage_map):
    matched = [usage_map.get(key) for key in candidate_keys if usage_map.get(key)]
    if not matched:
        return {}
    return max(matched, key=lambda item: (item.get('count', 0), item.get('usage_pct', 0)))


def _register_talent_identity(identity_lookup, node, node_key):
    for value in {node.node_id, node.talent_id, node.spell_id, node.key}:
        parsed = _coerce_positive_int(value)
        if parsed is not None:
            identity_lookup[parsed] = node_key


def _score_talent_node(node):
    score = 0
    if node.tree_type and node.tree_type != 'unknown':
        score += 1
    if node.row is not None and node.column is not None:
        score += 4
    if node.parents:
        score += 3
    if node.icon:
        score += 1
    if node.name and not str(node.name).startswith('技能ID '):
        score += 1
    return score


def _attach_usage_to_render_model(render_model, usage_map, highlighted_keys):
    highlighted_set = set(highlighted_keys or [])

    def _merge(node_payload):
        usage_item = usage_map.get(node_payload.get('node_key'), {})
        fallback_count = node_payload.get('count') or 0
        fallback_pct = node_payload.get('usage_pct') or node_payload.get('pct') or 0
        node_payload['count'] = usage_item.get('count', fallback_count)
        node_payload['usage_pct'] = usage_item.get('usage_pct', fallback_pct)
        node_payload['pct'] = usage_item.get('usage_pct', fallback_pct)
        node_payload['is_highlighted'] = node_payload.get('node_key') in highlighted_set or bool(node_payload.get('selected'))
        return node_payload

    for node in render_model.get('nodes', []):
        _merge(node)
    for tree in render_model.get('trees', []):
        for node in tree.get('nodes', []):
            _merge(node)


def _filter_hero_subtrees_for_spec(hero_subtrees, class_name, spec_name, used_subtree_ids):
    if not hero_subtrees:
        return {}

    used_subtree_ids = {subtree_id for subtree_id in used_subtree_ids if subtree_id in hero_subtrees}
    anchor_subtree_ids = set(
        WowTalentNodeMetadata.objects.filter(
            class_name=class_name or '',
            spec_name=spec_name or '',
            tree_type='hero_anchor',
            db2_subtree_id__in=list(hero_subtrees.keys()),
        ).exclude(db2_subtree_id=0).values_list('db2_subtree_id', flat=True)
    )
    preferred_ids = used_subtree_ids | anchor_subtree_ids
    if len(preferred_ids) >= 2:
        return {subtree_id: hero_subtrees[subtree_id] for subtree_id in hero_subtrees if subtree_id in preferred_ids}

    result = {subtree_id: hero_subtrees[subtree_id] for subtree_id in hero_subtrees if subtree_id in preferred_ids}
    for subtree_id, nodes in sorted(hero_subtrees.items(), key=lambda item: _hero_subtree_sort_key(item[0], item[1])):
        if subtree_id in result:
            continue
        result[subtree_id] = nodes
        if len(result) >= 2:
            break
    return result


def _group_hero_subtrees_by_column(hero_nodes):
    """将 hero 节点按 db2_subtree_id 分组为不同子树。

    优先使用 db2_subtree_id，如果都是 0 则回退到 column 分组。
    """
    if not hero_nodes:
        return {}

    # 优先按 db2_subtree_id 分组
    by_subtree_id = {}
    for node in hero_nodes:
        sid = getattr(node, 'db2_subtree_id', 0) or 0
        if sid > 0:
            by_subtree_id.setdefault(sid, []).append(node)

    if by_subtree_id:
        return by_subtree_id

    # 回退到 column 分组
    columns = sorted({(n.column or 0) for n in hero_nodes if (n.column or 0) > 0})
    if not columns:
        return {0: hero_nodes}

    groups = []
    current_group = [columns[0]]
    for i in range(1, len(columns)):
        if columns[i] - columns[i - 1] > 3000:
            groups.append(current_group)
            current_group = [columns[i]]
        else:
            current_group.append(columns[i])
    groups.append(current_group)

    if len(groups) <= 1:
        return {0: hero_nodes}

    column_to_group = {}
    for group_idx, group_cols in enumerate(groups):
        for col in group_cols:
            column_to_group[col] = group_idx

    result = {}
    for node in hero_nodes:
        col = node.column or 0
        group_key = column_to_group.get(col, 0)
        result.setdefault(group_key, []).append(node)

    return result


def _filter_spec_nodes_by_usage_component(full_tree_nodes, usage_map):
    """过滤同一职业 TraitTree 中混入的其它专精 spec 子图。

    DB2 的 TraitTreeID 是按职业划分的，部分 backfill 数据会把同一职业的三个
    spec 子图都写到每个 spec 下。聚合页用全量节点做底板时，如果直接按坐标合并，
    Blood/Frost/Unholy 这类同坐标入口会被误判为 3 选 1。

    当前详情页 records 的 usage_map 只包含真实样本点过的 spec 节点。这里用
    parents_json（父 node_id / entry_id）构建 spec 节点无向图，只保留与实际使用
    节点同一连通分量的 spec 节点；class/hero 节点保持不变。若没有可匹配 usage
    节点，则保持原始行为，避免空数据页面被误删。
    """
    if not full_tree_nodes or not usage_map:
        return full_tree_nodes

    spec_nodes = []
    by_node_id = {}
    adjacency = defaultdict(set)

    for raw_node in full_tree_nodes:
        if (raw_node.get('tree_type') or 'spec') != 'spec':
            continue
        spec_nodes.append(raw_node)
        node_id = _coerce_positive_int(raw_node.get('node_id'))
        if node_id:
            by_node_id[node_id] = raw_node
            adjacency.setdefault(node_id, set())

    if not spec_nodes or not by_node_id:
        return full_tree_nodes

    for raw_node in spec_nodes:
        node_id = _coerce_positive_int(raw_node.get('node_id'))
        if not node_id:
            continue
        for parent_id in raw_node.get('parents') or raw_node.get('parents_json') or []:
            parent_id = _coerce_positive_int(parent_id)
            if not parent_id or parent_id not in by_node_id:
                continue
            adjacency[node_id].add(parent_id)
            adjacency[parent_id].add(node_id)

    usage_node_ids = set()
    for raw_node in spec_nodes:
        node_id = _coerce_positive_int(raw_node.get('node_id'))
        talent_id = _coerce_positive_int(raw_node.get('talent_id'))
        spell_id = _coerce_positive_int(raw_node.get('spell_id'))
        display_spell_id = _coerce_positive_int(raw_node.get('display_spell_id'))
        candidate_keys = [
            f'spec:{value}'
            for value in (node_id, talent_id, spell_id, display_spell_id)
            if value
        ]
        if any((usage_map.get(key) or {}).get('count', 0) > 0 for key in candidate_keys):
            if node_id:
                usage_node_ids.add(node_id)

    if not usage_node_ids:
        return full_tree_nodes

    kept_node_ids = set()
    stack = list(usage_node_ids)
    while stack:
        current = stack.pop()
        if current in kept_node_ids:
            continue
        kept_node_ids.add(current)
        stack.extend(adjacency.get(current, set()) - kept_node_ids)

    if not kept_node_ids:
        return full_tree_nodes

    filtered = []
    for raw_node in full_tree_nodes:
        if (raw_node.get('tree_type') or 'spec') != 'spec':
            filtered.append(raw_node)
            continue
        node_id = _coerce_positive_int(raw_node.get('node_id'))
        if node_id in kept_node_ids:
            filtered.append(raw_node)
    return filtered


def _iter_render_tree_types(grouped_nodes):
    ordered = ['class', 'hero', 'spec']
    yielded = set()
    for tree_type in ordered:
        if tree_type in grouped_nodes:
            yielded.add(tree_type)
            yield tree_type
    for tree_type in sorted(grouped_nodes.keys()):
        if tree_type not in yielded and tree_type != 'hero_anchor':
            yield tree_type


def _coerce_positive_int(value):
    try:
        parsed = int(str(value).strip())
    except Exception:
        return None
    return parsed if parsed > 0 else None


SECONDARY_STAT_LABELS = {
    'crit': '暴击',
    'haste': '急速',
    'mastery': '精通',
    'versatility': '全能',
}


def _compute_numeric_summary(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return {
        'avg': round(sum(values) / len(values), 1),
        'median': round(_percentile(values, 50), 1),
        'min': round(values[0], 1),
        'max': round(values[-1], 1),
        'p25': round(_percentile(values, 25), 1),
        'p75': round(_percentile(values, 75), 1),
    }



def _compute_race_distribution(player_records):
    """聚合当前样本种族分布。缺失种族显式计入未知，避免样本人数凭空丢失。"""
    race_counter = Counter()
    total = 0
    for record in player_records or []:
        race = record.get('race') or 'unknown'
        total += 1
        race_counter[str(race)] += 1
    return [
        {
            'race': race,
            'race_cn': '未知' if race == 'unknown' else _translate_race(race),
            'count': count,
            'pct': round(count / total * 100, 1) if total else 0,
        }
        for race, count in race_counter.most_common(12)
    ]


def _compute_secondary_stats_distribution(player_records):
    """聚合人物榜属性绿字区间。来源 PlayerSpecTopPlayer.stats_json。"""
    buckets = {key: {'pct': [], 'rating': []} for key in SECONDARY_STAT_LABELS}
    sample_size = 0
    for record in player_records or []:
        stats_json = record.get('stats_json') or {}
        if not isinstance(stats_json, dict):
            continue
        has_any = False
        for key in SECONDARY_STAT_LABELS:
            payload = stats_json.get(key) or {}
            if not isinstance(payload, dict):
                continue
            pct = payload.get('pct')
            rating = payload.get('rating')
            try:
                if pct is not None:
                    buckets[key]['pct'].append(float(pct))
                    has_any = True
            except (TypeError, ValueError):
                pass
            try:
                if rating is not None:
                    buckets[key]['rating'].append(float(rating))
                    has_any = True
            except (TypeError, ValueError):
                pass
        if has_any:
            sample_size += 1

    result = []
    for key, label in SECONDARY_STAT_LABELS.items():
        pct_summary = _compute_numeric_summary(buckets[key]['pct'])
        rating_summary = _compute_numeric_summary(buckets[key]['rating'])
        if not pct_summary and not rating_summary:
            continue
        result.append({
            'key': key,
            'label': label,
            'pct': pct_summary,
            'rating': rating_summary,
            'sample_size': sample_size,
        })
    return result


def _normalize_gear_items(items):
    """统一装备展示字段并过滤明显无效项。附魔/宝石/品质自动中文化。"""
    default_slots = [
        'head', 'neck', 'shoulder', 'shirt', 'chest', 'waist', 'legs', 'feet',
        'wrist', 'hands', 'finger1', 'finger2', 'trinket1', 'trinket2',
        'back', 'main_hand', 'off_hand', 'tabard',
    ]
    result = []
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        slot = raw.get('slot', 'unknown')
        item_name = raw.get('name') or '未知物品'
        if item_name == 'Unknown Item':
            item_name = '未知物品'
        icon = _normalize_icon_name(raw.get('icon', ''))

        # 翻译附魔名称
        enchants_detail = raw.get('enchants_detail', []) or []
        translated_enchants = []
        for e in enchants_detail:
            if isinstance(e, dict):
                translated = dict(e)
                translated['name'] = _translate_enchant_name(e.get('name', ''))
                translated_enchants.append(translated)
            else:
                translated_enchants.append(e)

        # 翻译宝石名称
        gems_detail = raw.get('gems_detail', []) or []
        translated_gems = []
        for g in gems_detail:
            if isinstance(g, dict):
                translated = dict(g)
                translated['name'] = _translate_gem_name(g.get('name', ''))
                translated_gems.append(translated)
            else:
                translated_gems.append(g)

        result.append({
            'slot': SLOT_CN.get(slot, '' if slot == 'unknown' else slot),
            'name': item_name,
            'id': raw.get('id') or raw.get('itemID') or raw.get('item_id'),
            'icon': icon,
            'itemLevel': raw.get('itemLevel') or raw.get('item_level'),
            'quality': _translate_quality(raw.get('quality', '')),
            'bonusIDs': raw.get('bonusIDs', []),
            'gems': raw.get('gems', []),
            'gems_detail': translated_gems,
            'enchants': raw.get('enchants', []),
            'enchants_detail': translated_enchants,
            'source': raw.get('source', ''),
        })
    if result and all(item.get('slot') in ('', '未知') for item in result):
        for idx, item in enumerate(result):
            if idx >= len(default_slots):
                break
            item['slot'] = SLOT_CN.get(default_slots[idx], default_slots[idx])
    return result


def _resolve_player_gear(player):
    gear_items = player.gear_json or []
    if gear_items:
        return {
            'items': _normalize_gear_items(gear_items),
            'source': '人物榜 Monitor 落库',
        }

    ranking = SpecDungeonRanking.objects.filter(
        character_name=player.character_name,
        class_name=player.class_name,
        spec_name=player.spec_name,
    ).exclude(gear_json='[]').first()
    if not ranking:
        ranking = SpecRaidRanking.objects.filter(
            character_name=player.character_name,
            class_name=player.class_name,
            spec_name=player.spec_name,
        ).exclude(gear_json='[]').first()
    if ranking:
        return {
            'items': _normalize_gear_items(getattr(ranking, 'gear_json', []) or []),
            'source': '本地 WCL 榜单回填',
        }
    return {
        'items': [],
        'source': '暂无稳定装备来源',
    }


def _describe_player_stats_source(player):
    if player.stats_json:
        return 'Battle.net 属性 Monitor 已采集'
    if player.stats_crawl_status == -2:
        return 'Battle.net 属性 Monitor 未配置'
    if (player.region or '').lower() == 'cn':
        return '国服未接 Battle.net 属性源'
    if player.stats_crawl_status == -1:
        return 'Battle.net 属性 Monitor 采集失败'
    if player.stats_crawl_status == 0:
        return 'Battle.net 属性 Monitor 待采集'
    return '暂无稳定属性来源'


def _merge_player_profile_fields(records, season_id, class_name, spec_name, fields=('race', 'stats_json')):
    """按角色身份把人物榜字段回填到当前 ranking 样本，保持 records 长度不变。"""
    try:
        from botend.models import PlayerSpecTopPlayer
        keys = set()
        for r in records or []:
            key = (
                (r.get('region') or '').lower(),
                (r.get('realm') or '').lower(),
                (r.get('character_name') or '').lower(),
            )
            if all(key):
                keys.add(key)
        if not keys:
            return [dict(r) for r in records or []]

        value_fields = ('region', 'realm', 'character_name', *fields)
        profiles = {}
        for p in PlayerSpecTopPlayer.objects.filter(
            season_id=season_id, class_name=class_name, spec_name=spec_name
        ).values(*value_fields):
            key = (
                (p.get('region') or '').lower(),
                (p.get('realm') or '').lower(),
                (p.get('character_name') or '').lower(),
            )
            if key in keys:
                profiles[key] = p

        merged = []
        for r in records or []:
            row = dict(r)
            key = (
                (row.get('region') or '').lower(),
                (row.get('realm') or '').lower(),
                (row.get('character_name') or '').lower(),
            )
            profile = profiles.get(key) or {}
            for field in fields:
                if profile.get(field) not in (None, '', [], {}):
                    row[field] = profile.get(field)
            merged.append(row)
        return merged
    except Exception:
        return [dict(r) for r in records or []]

def _merge_player_profile_gear(records, season_id, class_name, spec_name):
    """把人物榜 gear_json 的 gems_detail/enchants_detail 按角色匹配回填到 ranking 样本。

    ranking 表通常只有装备和 gems id，没有 enchants_detail；人物榜有完整 gear_json。
    返回长度不变的 records，分母仍是当前详情页的 100 个 ranking 样本。
    """
    try:
        from botend.models import PlayerSpecTopPlayer
        keys = set()
        for r in records or []:
            key = (
                (r.get('region') or '').lower(),
                (r.get('realm') or '').lower(),
                (r.get('character_name') or '').lower(),
            )
            if all(key):
                keys.add(key)
        if not keys:
            return records
        profiles = {}
        for p in PlayerSpecTopPlayer.objects.filter(
            season_id=season_id, class_name=class_name, spec_name=spec_name
        ).values('region', 'realm', 'character_name', 'gear_json'):
            key = (
                (p.get('region') or '').lower(),
                (p.get('realm') or '').lower(),
                (p.get('character_name') or '').lower(),
            )
            if key in keys:
                profiles[key] = p.get('gear_json') or []
        if not profiles:
            return records
        merged = []
        for r in records or []:
            row = dict(r)
            key = (
                (row.get('region') or '').lower(),
                (row.get('realm') or '').lower(),
                (row.get('character_name') or '').lower(),
            )
            profile_gear = profiles.get(key)
            if profile_gear:
                # 人物榜装备包含 gems_detail/enchants_detail，优先用于宝石/附魔聚合。
                row['gear_json'] = profile_gear
            merged.append(row)
        return merged
    except Exception:
        return records


def _compute_gem_popularity(records, top_n=20):
    """计算宝石选取率：按玩家去重，并从 WowItemSnapshot 读取中文名/描述。"""
    snapshots = _collect_item_ids_from_records(records, include_gear=False, include_gems=True, include_enchants=False)
    gem_players = {}  # gem_id → set of player indices
    gem_info = {}
    total = len(records)

    for idx, r in enumerate(records):
        gear = r.get('gear_json') or []
        player_gems = set()
        for g in gear:
            if not isinstance(g, dict):
                continue
            for gem in (g.get('gems_detail') or []) + (g.get('gems') or []):
                if not isinstance(gem, dict):
                    continue
                gem_id = _coerce_item_id(gem.get('id'))
                if not gem_id:
                    continue
                player_gems.add(gem_id)
                if gem_id not in gem_info:
                    payload = _item_snapshot_payload(
                        gem_id,
                        fallback_name=_translate_gem_name(gem.get('name', '') or f'#{gem_id}'),
                        fallback_icon=gem.get('icon', ''),
                        snapshots=snapshots,
                    )
                    gem_info[gem_id] = payload
        for gem_id in player_gems:
            gem_players.setdefault(gem_id, set()).add(idx)

    result = []
    for gem_id, player_set in sorted(gem_players.items(), key=lambda x: len(x[1]), reverse=True)[:top_n]:
        info = gem_info.get(gem_id, {})
        player_count = len(player_set)
        result.append({
            **info,
            'count': player_count,
            'pct': round(player_count / total * 100, 1) if total else 0,
        })
    return result

def _compute_enchant_popularity(enchant_records, top_n=20):
    """计算附魔选取率：按玩家去重，并从 WowItemSnapshot 读取中文名/描述。"""
    snapshots = _collect_item_ids_from_records(enchant_records, include_gear=False, include_gems=False, include_enchants=True)
    enchant_players = {}
    enchant_info = {}
    total = len(enchant_records)

    for idx, r in enumerate(enchant_records):
        gear = r.get('gear_json') or []
        player_enchants = set()
        for g in gear:
            if not isinstance(g, dict):
                continue
            for e in g.get('enchants_detail') or []:
                if not isinstance(e, dict):
                    continue
                eid = _coerce_item_id(e.get('id'))
                if not eid:
                    continue
                player_enchants.add(eid)
                if eid not in enchant_info:
                    payload = _item_snapshot_payload(
                        eid,
                        fallback_name=_translate_enchant_name(e.get('name', '')),
                        fallback_icon=e.get('icon', ''),
                        snapshots=snapshots,
                    )
                    enchant_info[eid] = payload
        for eid in player_enchants:
            enchant_players.setdefault(eid, set()).add(idx)

    result = []
    for eid, player_set in sorted(enchant_players.items(), key=lambda x: len(x[1]), reverse=True)[:top_n]:
        info = enchant_info.get(eid, {})
        player_count = len(player_set)
        result.append({
            **info,
            'count': player_count,
            'pct': round(player_count / total * 100, 1) if total else 0,
        })
    return result

_GEAR_DEFAULT_SLOTS = [
    'head', 'neck', 'shoulder', 'shirt', 'chest', 'waist', 'legs', 'feet',
    'wrist', 'hands', 'finger1', 'finger2', 'trinket1', 'trinket2',
    'back', 'main_hand', 'off_hand', 'tabard',
]


def _compute_gear_popularity(records, top_n=5):
    """计算装备选取率（每个槽位 Top N）：同一玩家同槽位同物品只计 1 人。"""
    snapshots = _collect_item_ids_from_records(records, include_gear=True, include_gems=False, include_enchants=False)
    slot_item_players = {}  # cn_slot → itemID → set(record_idx)
    slot_item_info = {}

    for record_idx, r in enumerate(records):
        gear = r.get('gear_json') or []
        all_unknown = gear and all(
            (g.get('slot', 'unknown') in ('unknown', '')) for g in gear if isinstance(g, dict)
        )
        player_slot_items = set()
        for idx, g in enumerate(gear):
            if not isinstance(g, dict):
                continue
            slot = g.get('slot', 'unknown')
            if all_unknown and slot in ('unknown', ''):
                slot = _GEAR_DEFAULT_SLOTS[idx] if idx < len(_GEAR_DEFAULT_SLOTS) else f'slot_{idx}'
            cn_slot = SLOT_CN.get(slot, slot)
            item_id = _coerce_item_id(g.get('id') or g.get('itemID') or g.get('item_id'))
            if not item_id:
                continue
            player_slot_items.add((cn_slot, item_id))
            key = (cn_slot, item_id)
            if key not in slot_item_info:
                slot_item_info[key] = _item_snapshot_payload(
                    item_id,
                    fallback_name=g.get('name', ''),
                    fallback_icon=g.get('icon', ''),
                    fallback_quality=g.get('quality') or 0,
                    snapshots=snapshots,
                )
                slot_item_info[key]['itemLevel'] = g.get('itemLevel')
        for cn_slot, item_id in player_slot_items:
            slot_item_players.setdefault(cn_slot, {}).setdefault(item_id, set()).add(record_idx)

    total = len(records)
    result = {}
    for slot, item_players in slot_item_players.items():
        items = []
        sorted_items = sorted(item_players.items(), key=lambda x: len(x[1]), reverse=True)[:top_n]
        for item_id, player_set in sorted_items:
            info = slot_item_info.get((slot, item_id), {})
            count = len(player_set)
            items.append({
                'itemID': item_id,
                **info,
                'itemLevel': info.get('itemLevel'),
                'count': count,
                'pct': round(count / total * 100, 1) if total else 0,
            })
        result[slot] = items

    return result
