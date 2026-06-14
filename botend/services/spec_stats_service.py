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
from botend.wow.talents.parser import normalize_talent_payload
from botend.wow.talents.metadata import TalentMetadataProvider
from botend.wow.talents.models import TREE_COLUMNS, TalentBuildStateModel, TalentNodeModel, TalentTreeModel, TalentTreeSetModel
from botend.wow.talents.render import build_talent_render_model
from botend.wow.talents.view_model import build_talent_view_model
from botend.wow.talents.service import TalentBuildCodeService


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
            'gear': gear_payload['items'],
            'gear_source': gear_payload['source'],
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
        usage_snapshot = _build_talent_usage_snapshot(records, class_name, spec_name)
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
        usage_snapshot = _build_talent_usage_snapshot(records, class_name, spec_name)
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


def _talent_tree_label(tree_type):
    mapping = {
        'class': '职业天赋',
        'spec': '专精天赋',
        'hero': '英雄天赋',
        'build_code': '导入代码',
    }
    return mapping.get(tree_type or 'spec', tree_type or '天赋')


def _build_talent_usage_snapshot(records, class_name, spec_name):
    """聚合统计页热门天赋所需的节点、使用率与父子连线。"""
    total = len(records)
    provider = TalentMetadataProvider()
    usage = {}
    canonical_nodes = {}
    parent_edges = defaultdict(Counter)

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
    }


def _compute_talent_popularity(records, top_n=20):
    """计算天赋选取率（以 spellID 为 key）"""
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


def _compute_talent_popularity_tree(records, class_name, spec_name, top_n=20, snapshot=None):
    """把热门天赋聚合成可直接给模板消费的 render_model。"""
    snapshot = snapshot or _build_talent_usage_snapshot(records, class_name, spec_name)
    usage_list = snapshot.get('usage_list') or []
    if not usage_list:
        return {}

    canonical_nodes = snapshot.get('canonical_nodes') or {}
    parent_edges = snapshot.get('parent_edges') or {}
    usage_map = snapshot.get('usage_map') or {}
    highlighted_keys = [item['node_key'] for item in usage_list[:top_n] if item.get('node_key')]
    if not highlighted_keys:
        return {}

    included_keys = set()
    pending = list(highlighted_keys)
    while pending:
        node_key = pending.pop()
        if node_key in included_keys:
            continue
        included_keys.add(node_key)
        for parent_key, _count in parent_edges.get(node_key, Counter()).most_common():
            if parent_key in canonical_nodes and parent_key not in included_keys:
                pending.append(parent_key)

    grouped_nodes = defaultdict(list)
    preserved_parent_edges = 0
    missing_parent_edges = 0

    for node_key in included_keys:
        base_node = canonical_nodes.get(node_key)
        if not base_node:
            continue
        usage_item = usage_map.get(node_key, {})
        parent_ids = []
        seen_parent_ids = set()
        for parent_key, _count in parent_edges.get(node_key, Counter()).most_common():
            parent_node = canonical_nodes.get(parent_key)
            parent_id = parent_node.key if parent_node else None
            if (
                not parent_node
                or parent_key not in included_keys
                or parent_node.tree_type != base_node.tree_type
                or not parent_id
            ):
                missing_parent_edges += 1
                continue
            if parent_id in seen_parent_ids:
                continue
            seen_parent_ids.add(parent_id)
            parent_ids.append(parent_id)
            preserved_parent_edges += 1

        grouped_nodes[base_node.tree_type or 'spec'].append(TalentNodeModel.from_raw({
            **base_node.to_dict(),
            'parents': parent_ids,
            'points': 1 if node_key in highlighted_keys else 0,
            'selected': node_key in highlighted_keys,
        }))

    trees = []
    for tree_type in _iter_render_tree_types(grouped_nodes):
        nodes = sorted(
            grouped_nodes[tree_type],
            key=lambda item: (
                item.row if item.row is not None else 999,
                item.column if item.column is not None else 999,
                item.node_id or item.talent_id or item.spell_id or 0,
                item.name or '',
            ),
        )
        default_columns = TREE_COLUMNS.get(tree_type, 8)
        rows = [node.row for node in nodes if node.row is not None]
        columns = [node.column for node in nodes if node.column is not None]
        trees.append(TalentTreeModel(
            tree_type=tree_type,
            nodes=nodes,
            grid_columns=max([default_columns, *columns]) if columns else default_columns,
            grid_rows=max(rows) if rows else max(1, (len(nodes) + default_columns - 1) // default_columns),
            synthetic_layout=not any(rows or columns),
        ))

    build_state = TalentBuildStateModel(
        source_type='stats',
        source_id=':'.join(part for part in [class_name, spec_name, 'popularity'] if part),
        selected_nodes={key for key in highlighted_keys if key in included_keys},
        node_ranks={key: 1 for key in highlighted_keys if key in included_keys},
    )
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
    _attach_usage_to_render_model(render_model, usage_map, highlighted_keys)

    return {
        'sample_size': snapshot.get('total', 0),
        'highlighted_node_count': len(highlighted_keys),
        'rendered_node_count': len(included_keys),
        'preserved_parent_edges': preserved_parent_edges,
        'missing_parent_edges': missing_parent_edges,
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
    }
    node_data = provider.merge_into_node(node_data, class_name=class_name, spec_name=spec_name)
    node = TalentNodeModel.from_raw(node_data)
    if node.key is None:
        return None
    return node


def _build_talent_node_key(node):
    node_identity = node.key
    if node_identity is None:
        return ''
    return f'{node.tree_type or "spec"}:{node_identity}'


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
        node_payload['count'] = usage_item.get('count', 0)
        node_payload['usage_pct'] = usage_item.get('usage_pct', 0)
        node_payload['pct'] = usage_item.get('usage_pct', 0)
        node_payload['is_highlighted'] = node_payload.get('node_key') in highlighted_set
        return node_payload

    for node in render_model.get('nodes', []):
        _merge(node)
    for tree in render_model.get('trees', []):
        for node in tree.get('nodes', []):
            _merge(node)


def _iter_render_tree_types(grouped_nodes):
    ordered = ['class', 'spec', 'hero']
    yielded = set()
    for tree_type in ordered:
        if tree_type in grouped_nodes:
            yielded.add(tree_type)
            yield tree_type
    for tree_type in sorted(grouped_nodes.keys()):
        if tree_type not in yielded:
            yield tree_type


def _coerce_positive_int(value):
    try:
        parsed = int(str(value).strip())
    except Exception:
        return None
    return parsed if parsed > 0 else None

def _normalize_gear_items(items):
    """统一装备展示字段并过滤明显无效项。"""
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
        result.append({
            'slot': SLOT_CN.get(slot, '' if slot == 'unknown' else slot),
            'name': item_name,
            'id': raw.get('id') or raw.get('itemID') or raw.get('item_id'),
            'icon': icon,
            'itemLevel': raw.get('itemLevel') or raw.get('item_level'),
            'quality': raw.get('quality', ''),
            'bonusIDs': raw.get('bonusIDs', []),
            'gems': raw.get('gems', []),
            'gems_detail': raw.get('gems_detail', []),
            'enchants': raw.get('enchants', []),
            'enchants_detail': raw.get('enchants_detail', []),
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
                    'icon': _normalize_icon_name(g.get('icon', '')),
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
