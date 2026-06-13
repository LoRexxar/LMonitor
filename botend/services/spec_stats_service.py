# -*- coding: utf-8 -*-
"""
专精详情数据聚合 Service
从原始数据表计算统计值（avg/median/p25/p75/选取率/分布）
"""

from collections import Counter, defaultdict
from django.db.models import Avg, Max, Min, StdDev

from botend.models import (
    SeasonMeta, PlayerSpecTopPlayer, SpecDungeonRanking, SpecRaidRanking
)
from botend.constants.wow import CLASS_CN, SPEC_CN, SPEC_ICON, SPEC_ROLE, DUNGEON_CN, RAID_BOSS_CN, RAID_ZONE_CN, SLOT_CN


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

        normalized_talents = _normalize_talent_nodes(player.talents_json or [])
        talent_groups = _group_talent_nodes(normalized_talents)
        normalized_gear = _normalize_gear_items(player.gear_json or [])

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
            'gear': normalized_gear,
            'talents': normalized_talents,
            'talent_groups': talent_groups,
            'talent_code': next((t.get('talent_code') for t in normalized_talents if t.get('talent_code')), ''),
            'stats': player.stats_json or {},
            'last_updated': player.last_updated,
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

        if not qs.exists():
            return stats

        # DPS 统计
        dps_agg = qs.aggregate(
            avg=Avg('dps'), max=Max('dps'), min=Min('dps'), stddev=StdDev('dps')
        )
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
            'stddev': dps_agg['stddev'],
            # 百分比字段（给前端 DPS 分布条用）
            'p25_pct': round((p25 - dps_min) / dps_range * 100, 1) if p25 and dps_range else 0,
            'iqr_pct': round((p75 - p25) / dps_range * 100, 1) if p75 and p25 and dps_range else 0,
            'median_pct': round((median - dps_min) / dps_range * 100, 1) if median and dps_range else 50,
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
        stats['talent_usage'] = _compute_talent_usage(records, top_n=talent_limit)
        stats['gear_popularity'] = _compute_gear_popularity(records, top_n=gear_limit)

        if full:
            # 详细模式：额外计算种族分布 + Top 5
            full_records = list(qs.values('talents_json', 'gear_json', 'faction', 'guild_name',
                                      'character_name', 'realm', 'region', 'dps',
                                      'keystone_level', 'clear_time', 'score'))
            stats['race_distribution'] = dict(Counter(r.get('faction') for r in full_records))
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

        # 天赋/装备热门度（概览也展示）
        records = list(qs.values('talents_json', 'gear_json', 'faction'))
        talent_limit = 20 if full else 10
        gear_limit = 5 if full else 3
        stats['talent_popularity'] = _compute_talent_popularity(records, top_n=talent_limit)
        stats['talent_usage'] = _compute_talent_usage(records, top_n=talent_limit)
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


def _normalize_talent_nodes(talents):
    """兼容多种旧格式的天赋节点，统一成可展示结构。"""
    result = []
    for raw in talents or []:
        if isinstance(raw, str):
            result.append({
                'tree_type': 'build_code',
                'talent_code': raw,
                'talent_id': None,
                'spell_id': None,
                'name': 'Talent Loadout',
                'icon': '',
                'points': 0,
                'row': None,
                'column': None,
            })
            continue

        if not isinstance(raw, dict):
            continue

        talent_id = raw.get('talent_id') or raw.get('talentID')
        spell_id = raw.get('spell_id') or raw.get('spellID') or talent_id
        name = raw.get('name') or (f'Spell #{spell_id}' if spell_id else '未命名天赋')
        icon = raw.get('icon', '')
        result.append({
            'tree_type': raw.get('tree_type') or raw.get('treeType') or 'spec',
            'talent_code': raw.get('talent_code', ''),
            'talent_id': talent_id,
            'spell_id': spell_id,
            'name': name,
            'icon': icon,
            'points': raw.get('points', 0) or 0,
            'row': raw.get('row') if raw.get('row') is not None else raw.get('tier'),
            'column': raw.get('column'),
        })
    return result


def _group_talent_nodes(nodes):
    """按树类型分组，便于模板展示。"""
    groups = defaultdict(list)
    for node in nodes:
        tree_type = node.get('tree_type') or 'spec'
        groups[tree_type].append(node)

    ordered = []
    for key in ['class', 'spec', 'hero', 'build_code']:
        if groups.get(key):
            ordered.append({
                'key': key,
                'label': _talent_tree_label(key),
                'nodes': sorted(groups[key], key=lambda item: (
                    item.get('row') if item.get('row') is not None else 999,
                    item.get('column') if item.get('column') is not None else 999,
                    item.get('name') or '',
                )),
            })
    for key, nodes_in_group in groups.items():
        if key in {'class', 'spec', 'hero', 'build_code'}:
            continue
        ordered.append({
            'key': key,
            'label': _talent_tree_label(key),
            'nodes': sorted(nodes_in_group, key=lambda item: item.get('name') or ''),
        })
    return ordered


def _talent_tree_label(tree_type):
    mapping = {
        'class': '职业天赋',
        'spec': '专精天赋',
        'hero': '英雄天赋',
        'build_code': '导入代码',
    }
    return mapping.get(tree_type, tree_type or '天赋')


def _compute_talent_popularity(records, top_n=20):
    """计算天赋选取率（以 spellID 为 key）"""
    talent_counts = Counter()
    talent_info = {}  # spellID → {name, icon}
    total = len(records)

    for r in records:
        talents = _normalize_talent_nodes(r.get('talents_json') or [])
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


def _compute_talent_usage(records, top_n=20):
    """返回更适合页面展示的天赋使用率列表。"""
    total = len(records)
    usage = {}

    for record in records:
        seen = set()
        for node in _normalize_talent_nodes(record.get('talents_json') or []):
            if node.get('tree_type') == 'build_code':
                continue
            key = (
                node.get('tree_type') or 'spec',
                node.get('spell_id') or node.get('talent_id'),
            )
            if not key[1] or key in seen:
                continue
            seen.add(key)
            if key not in usage:
                usage[key] = {
                    'tree_type': key[0],
                    'tree_label': _talent_tree_label(key[0]),
                    'spell_id': node.get('spell_id'),
                    'talent_id': node.get('talent_id'),
                    'name': node.get('name') or (f"Spell #{key[1]}"),
                    'icon': node.get('icon', ''),
                    'count': 0,
                }
            usage[key]['count'] += 1

    result = []
    for item in usage.values():
        pct = round(item['count'] / total * 100, 1) if total else 0
        item['usage_pct'] = pct
        item['pct'] = pct
        result.append(item)

    result.sort(key=lambda item: (-item['usage_pct'], item['name']))
    return result[:top_n]


def _normalize_gear_items(items):
    """统一装备展示字段并过滤明显无效项。"""
    result = []
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        slot = raw.get('slot', 'unknown')
        result.append({
            'slot': SLOT_CN.get(slot, slot),
            'name': raw.get('name') or '未知物品',
            'id': raw.get('id') or raw.get('itemID') or raw.get('item_id'),
            'icon': raw.get('icon', ''),
            'itemLevel': raw.get('itemLevel') or raw.get('item_level'),
            'quality': raw.get('quality', ''),
            'bonusIDs': raw.get('bonusIDs', []),
            'gems': raw.get('gems', []),
        })
    return result


def _compute_gear_popularity(records, top_n=5):
    """计算装备选取率（每个槽位 Top N）"""
    slot_items = {}  # slot → Counter(itemID)
    slot_item_info = {}  # (slot, itemID) → {name, icon}

    for r in records:
        gear = r.get('gear_json') or []
        for g in gear:
            slot = g.get('slot', 'unknown')
            cn_slot = SLOT_CN.get(slot, slot)
            item_id = g.get('id')
            if not item_id:
                continue
            if cn_slot not in slot_items:
                slot_items[cn_slot] = Counter()
            slot_items[cn_slot][item_id] += 1
            # 记录装备信息（第一次出现即可）
            key = (cn_slot, item_id)
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
