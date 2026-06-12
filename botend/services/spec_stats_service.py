# -*- coding: utf-8 -*-
"""
专精详情数据聚合 Service
从原始数据表计算统计值（avg/median/p25/p75/选取率/分布）
"""

from collections import Counter
from django.db.models import Avg, Max, Min, StdDev

from botend.models import (
    SeasonMeta, PlayerSpecTopPlayer, SpecDungeonRanking, SpecRaidRanking
)
from botend.constants.wow import CLASS_CN, SPEC_CN, SPEC_ICON, SPEC_ROLE, DUNGEON_CN, RAID_BOSS_CN


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
            'faction', 'race', 'guild_name', 'item_level', 'avatar_url', 'profile_url'
        ))

        return {
            'players': players,
            'total': total,
            'page': page,
            'pages': pages,
        }

    @staticmethod
    def get_player_detail(player_id):
        """单玩家完整详情"""
        player = PlayerSpecTopPlayer.objects.filter(id=player_id).first()
        if not player:
            return None

        return {
            'id': player.id,
            'rank': player.rank,
            'character_name': player.character_name,
            'realm': player.realm,
            'region': player.region,
            'score': player.score,
            'faction': player.faction,
            'race': player.race,
            'gender': player.gender,
            'guild_name': player.guild_name,
            'realm_rank': player.realm_rank,
            'avatar_url': player.avatar_url,
            'profile_url': player.profile_url,
            'achievement_points': player.achievement_points,
            'item_level': player.item_level,
            'gear': player.gear_json or [],
            'talents': player.talents_json or [],
            'stats': player.stats_json or {},
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
            cn_name = DUNGEON_CN.get(enc['name'], enc['name'])
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
        return SpecStatsService._compute_dungeon_stats(
            season_id, dungeon_id, dungeon_name, class_name, spec_name, full=True
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

        if not qs.exists():
            return stats

        # DPS 统计
        dps_agg = qs.aggregate(
            avg=Avg('dps'), max=Max('dps'), min=Min('dps'), stddev=StdDev('dps')
        )
        dps_list = sorted(qs.values_list('dps', flat=True))
        n = len(dps_list)

        stats['sample_size'] = n
        stats['dps'] = {
            'avg': dps_agg['avg'],
            'median': _percentile(dps_list, 50),
            'max': dps_agg['max'],
            'min': dps_agg['min'],
            'p25': _percentile(dps_list, 25),
            'p75': _percentile(dps_list, 75),
            'stddev': dps_agg['stddev'],
        }

        # 钥石等级
        ks_list = sorted([v for v in qs.values_list('keystone_level', flat=True) if v])
        if ks_list:
            stats['keystone'] = {
                'avg': sum(ks_list) / len(ks_list),
                'max': max(ks_list),
                'min': min(ks_list),
            }

        # 通关时间
        ct_list = sorted([v for v in qs.values_list('clear_time', flat=True) if v])
        if ct_list:
            ct_median = _percentile(ct_list, 50)
            ct_avg = sum(ct_list) // len(ct_list)
            stats['clear_time'] = {
                'avg': ct_avg,
                'median': ct_median,
                'avg_fmt': _ms_to_time(ct_avg),
                'median_fmt': _ms_to_time(ct_median),
            }

        # 天赋/装备热门度（概览也展示）
        records = list(qs.values('talents_json', 'gear_json', 'faction'))
        talent_limit = 20 if full else 10
        gear_limit = 5 if full else 3
        stats['talent_popularity'] = _compute_talent_popularity(records, top_n=talent_limit)
        stats['gear_popularity'] = _compute_gear_popularity(records, top_n=gear_limit)

        if full:
            # 详细模式：额外计算种族分布 + Top 5
            full_records = list(qs.values('talents_json', 'gear_json', 'faction', 'guild_name',
                                      'character_name', 'realm', 'region', 'dps',
                                      'keystone_level', 'clear_time', 'score'))
            stats['race_distribution'] = dict(Counter(r.get('faction') for r in full_records))
            stats['top5'] = sorted(full_records, key=lambda r: r['dps'] or 0, reverse=True)[:5]

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

        bosses = []
        for enc in season.raid_encounters:
            cn_name = RAID_BOSS_CN.get(enc['name'], enc['name'])
            stats = SpecStatsService._compute_raid_stats(
                season_id, enc['id'], cn_name, class_name, spec_name
            )
            bosses.append(stats)

        return bosses

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
        return SpecStatsService._compute_raid_stats(
            season_id, boss_id, boss_name, class_name, spec_name, full=True
        )

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
        stats['dps'] = {
            'avg': dps_agg['avg'],
            'median': _percentile(dps_list, 50),
            'max': dps_agg['max'],
            'min': dps_agg['min'],
            'p25': _percentile(dps_list, 25),
            'p75': _percentile(dps_list, 75),
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

        # 天赋/装备热门度（概览也展示）
        records = list(qs.values('talents_json', 'gear_json', 'faction'))
        talent_limit = 20 if full else 10
        gear_limit = 5 if full else 3
        stats['talent_popularity'] = _compute_talent_popularity(records, top_n=talent_limit)
        stats['gear_popularity'] = _compute_gear_popularity(records, top_n=gear_limit)

        if full:
            full_records = list(qs.values('talents_json', 'gear_json', 'faction', 'guild_name',
                                      'character_name', 'realm', 'region', 'dps', 'kill_time'))
            stats['top5'] = sorted(full_records, key=lambda r: r['dps'] or 0, reverse=True)[:5]

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


def _ms_to_time(ms):
    """毫秒转 M:SS 格式"""
    if not ms:
        return None
    total_seconds = int(ms / 1000)
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return f"{minutes}:{seconds:02d}"


def _compute_talent_popularity(records, top_n=20):
    """计算天赋选取率"""
    talent_counts = Counter()
    total = len(records)

    for r in records:
        talents = r.get('talents_json') or []
        for t in talents:
            tid = t.get('talentID')
            if tid:
                talent_counts[tid] += 1

    # 按选取率降序
    result = {}
    for tid, count in talent_counts.most_common(top_n):
        result[str(tid)] = {
            'count': count,
            'pct': round(count / total * 100, 1) if total else 0,
        }
    return result


def _compute_gear_popularity(records, top_n=5):
    """计算装备选取率（每个槽位 Top N）"""
    slot_items = {}  # slot → Counter(itemID)
    slot_item_info = {}  # (slot, itemID) → {name, icon}

    for r in records:
        gear = r.get('gear_json') or []
        for g in gear:
            slot = g.get('slot', 'unknown')
            item_id = g.get('id')
            if not item_id:
                continue
            if slot not in slot_items:
                slot_items[slot] = Counter()
            slot_items[slot][item_id] += 1
            # 记录装备信息（第一次出现即可）
            key = (slot, item_id)
            if key not in slot_item_info:
                slot_item_info[key] = {
                    'name': g.get('name', ''),
                    'icon': g.get('icon', ''),
                    'itemLevel': g.get('itemLevel'),
                }

    total = len(records)
    result = {}
    for slot, counter in slot_items.items():
        items = []
        for item_id, count in counter.most_common(top_n):
            info = slot_item_info.get((slot, item_id), {})
            items.append({
                'itemID': item_id,
                'name': info.get('name', ''),
                'icon': info.get('icon', ''),
                'itemLevel': info.get('itemLevel'),
                'count': count,
                'pct': round(count / total * 100, 1) if total else 0,
            })
        result[slot] = items

    return result
