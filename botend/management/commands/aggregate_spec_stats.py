# -*- coding: utf-8 -*-
"""
Phase 1: 聚合专精统计数据为 JSON 文件，供前端快速读取。

用法:
    DJANGO_SETTINGS_MODULE=LMonitor.settings_dev .venv/bin/python manage.py aggregate_spec_stats
    DJANGO_SETTINGS_MODULE=LMonitor.settings_dev .venv/bin/python manage.py aggregate_spec_stats --season-id 11
    DJANGO_SETTINGS_MODULE=LMonitor.settings_dev .venv/bin/python manage.py aggregate_spec_stats --class-name Warrior --spec-name Arms
    DJANGO_SETTINGS_MODULE=LMonitor.settings_dev .venv/bin/python manage.py aggregate_spec_stats --force
"""

import hashlib
import json
import os
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand, CommandError

from botend.models import PlayerSpecTopPlayer, SeasonMeta, SpecDungeonRanking, SpecRaidRanking
from botend.constants.wow import CLASS_SPEC_MAP, RAID_BOSS_CN, SLOT_CN


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _percentile(sorted_list, pct):
    """计算百分位数（已排序列表），与 spec_stats_service._percentile 一致。"""
    if not sorted_list:
        return None
    k = (len(sorted_list) - 1) * pct / 100
    f = int(k)
    c = f + 1
    if c >= len(sorted_list):
        return sorted_list[-1]
    return sorted_list[f] + (k - f) * (sorted_list[c] - sorted_list[f])


def _normalize_icon_name(icon):
    """去掉 URL/扩展名等后缀，只留图标文件名。"""
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


def _normalize_gear_items(items):
    """
    统一装备展示字段。与 spec_stats_service._normalize_gear_items 保持一致，
    但不进行中文翻译（聚合 JSON 保留英文原始数据供前端自行翻译）。
    """
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
        item_id = raw.get('id') or raw.get('itemID') or raw.get('item_id')
        if not item_id:
            continue
        icon = _normalize_icon_name(raw.get('icon', ''))
        result.append({
            'slot': slot,
            'name': raw.get('name', ''),
            'id': item_id,
            'icon': icon,
            'itemLevel': raw.get('itemLevel') or raw.get('item_level'),
            'bonusIDs': raw.get('bonusIDs', []),
            'gems': raw.get('gems', []),
            'gems_detail': raw.get('gems_detail', []),
        })
    # 如果所有 slot 都是 unknown，按默认顺序分配
    if result and all(item.get('slot') in ('unknown', 'UNKNOWN', '') for item in result):
        for idx, item in enumerate(result):
            if idx >= len(default_slots):
                break
            item['slot'] = default_slots[idx]
    return result


def _talent_build_key(record):
    """
    返回天赋分组 key：优先 talent_build_code，fallback 到 talents_json 的 hash。
    """
    build_code = (record.get('talent_build_code') or '').strip()
    if build_code:
        return ('build_code', build_code)
    talents = record.get('talents_json') or []
    raw = json.dumps(talents, sort_keys=True, ensure_ascii=False) if talents else ''
    h = hashlib.md5(raw.encode('utf-8')).hexdigest()[:12]
    return ('hash', h)


def _compute_dps_stats(dps_list):
    """从已排序的 DPS 列表计算统计值。"""
    if not dps_list:
        return None
    n = len(dps_list)
    return {
        'avg': round(sum(dps_list) / n, 1),
        'median': round(_percentile(dps_list, 50), 1),
        'p25': round(_percentile(dps_list, 25), 1),
        'p75': round(_percentile(dps_list, 75), 1),
        'max': round(dps_list[-1], 1),
        'min': round(dps_list[0], 1),
        'sample_count': n,
    }


def _compute_kill_time_stats(kt_list):
    """从已排序的 kill_time (ms) 列表计算统计值。"""
    if not kt_list:
        return None
    n = len(kt_list)
    return {
        'avg': round(sum(kt_list) / n, 1),
        'median': round(_percentile(kt_list, 50), 1),
        'p25': round(_percentile(kt_list, 25), 1),
        'p75': round(_percentile(kt_list, 75), 1),
    }


def _compute_talent_distribution(records):
    """
    按天赋构建分组统计：优先用 talent_build_code，fallback 到 talents_json hash。
    返回按 count 降序排列的列表。
    """
    groups = defaultdict(lambda: {'count': 0, 'dps_sum': 0.0, 'key_type': '', 'key_value': ''})
    total = 0

    for r in records:
        key_type, key_value = _talent_build_key(r)
        g = groups[key_value]
        g['count'] += 1
        g['dps_sum'] += r.get('dps') or 0
        g['key_type'] = key_type
        g['key_value'] = key_value
        total += 1

    result = []
    for key_value, g in sorted(groups.items(), key=lambda x: -x[1]['count']):
        avg_dps = round(g['dps_sum'] / g['count'], 1) if g['count'] else 0
        result.append({
            'build': key_value,
            'count': g['count'],
            'pct': round(g['count'] / total * 100, 1) if total else 0,
            'avg_dps': avg_dps,
        })
    return result


def _compute_gear_popularity(records, top_n=10):
    """
    从 gear_json 聚合装备热门度。返回 {slot: [{name, id, icon, count, pct, avg_itemLevel}]}。
    """
    # (slot, itemID) → {count, name, icon, ilvl_sum}
    slot_items = defaultdict(lambda: {'count': 0, 'name': '', 'icon': '', 'ilvl_sum': 0.0, 'ilvl_count': 0})
    total = 0

    for r in records:
        gear = _normalize_gear_items(r.get('gear_json') or [])
        for g in gear:
            slot = g.get('slot', 'unknown')
            item_id = g.get('id')
            if not item_id:
                continue
            key = (slot, item_id)
            slot_items[key]['count'] += 1
            if not slot_items[key]['name'] and g.get('name'):
                slot_items[key]['name'] = g['name']
            if not slot_items[key]['icon'] and g.get('icon'):
                slot_items[key]['icon'] = g['icon']
            ilvl = g.get('itemLevel')
            if ilvl:
                try:
                    ilvl = float(ilvl)
                    slot_items[key]['ilvl_sum'] += ilvl
                    slot_items[key]['ilvl_count'] += 1
                except (ValueError, TypeError):
                    pass
        total += 1

    # 按槽位分组
    by_slot = defaultdict(list)
    for (slot, item_id), info in slot_items.items():
        avg_ilvl = round(info['ilvl_sum'] / info['ilvl_count'], 1) if info['ilvl_count'] else None
        by_slot[slot].append({
            'itemID': item_id,
            'name': info['name'],
            'icon': info['icon'],
            'count': info['count'],
            'pct': round(info['count'] / total * 100, 1) if total else 0,
            'avg_itemLevel': avg_ilvl,
        })

    result = {}
    for slot, items in by_slot.items():
        result[slot] = sorted(items, key=lambda x: -x['count'])[:top_n]

    return result


def _compute_gem_distribution(records):
    """
    从 gear_json 聚合宝石分布。返回按 count 降序的列表。
    """
    gem_counter = Counter()  # gem_id → count
    gem_info = {}  # gem_id → {name, icon}

    for r in records:
        gear = r.get('gear_json') or []
        for g in gear:
            gems_detail = g.get('gems_detail') or []
            for gem in gems_detail:
                if not isinstance(gem, dict):
                    continue
                gem_id = gem.get('id') or gem.get('gemID')
                if not gem_id:
                    continue
                gem_counter[gem_id] += 1
                if gem_id not in gem_info:
                    gem_info[gem_id] = {
                        'name': gem.get('name', ''),
                        'icon': _normalize_icon_name(gem.get('icon', '')),
                    }

    result = []
    for gem_id, count in gem_counter.most_common(20):
        info = gem_info.get(gem_id, {})
        result.append({
            'id': gem_id,
            'name': info.get('name', ''),
            'icon': info.get('icon', ''),
            'count': count,
        })
    return result


def _compute_race_distribution(players):
    """从 PlayerSpecTopPlayer 计算种族分布。"""
    counter = Counter()
    for p in players:
        race = p.get('race')
        if race:
            counter[race] += 1
    return dict(counter.most_common())


def _compute_faction_distribution(players):
    """从 PlayerSpecTopPlayer 计算阵营分布。"""
    counter = Counter()
    for p in players:
        faction = p.get('faction')
        if isinstance(faction, int):
            faction = 'alliance' if faction == 0 else 'horde'
        faction = (str(faction) or '').lower().strip()
        if faction in ('alliance', 'horde'):
            counter[faction] += 1
    return dict(counter)


def _compute_stat_distribution(players):
    """从 PlayerSpecTopPlayer.stats_json 计算属性分布（avg/median）。"""
    stat_keys = ['crit', 'haste', 'mastery', 'versatility']
    accumulators = {k: [] for k in stat_keys}

    for p in players:
        stats = p.get('stats_json') or {}
        for k in stat_keys:
            val = stats.get(k)
            if isinstance(val, dict):
                pct = val.get('pct')
                if pct is not None:
                    try:
                        accumulators[k].append(float(pct))
                    except (ValueError, TypeError):
                        pass
            elif val is not None:
                try:
                    accumulators[k].append(float(val))
                except (ValueError, TypeError):
                    pass

    result = {}
    for k in stat_keys:
        values = sorted(accumulators[k])
        if not values:
            continue
        result[k] = {
            'avg': round(sum(values) / len(values), 2),
            'median': round(_percentile(values, 50), 2),
        }
    return result


def _compute_guild_distribution(records, top_n=15):
    """计算公会分布（按出现频次降序）。"""
    counter = Counter()
    for r in records:
        guild = (r.get('guild_name') or '').strip()
        if guild:
            counter[guild] += 1
    result = []
    for name, count in counter.most_common(top_n):
        result.append({'name': name, 'count': count})
    return result


# ---------------------------------------------------------------------------
# Main command
# ---------------------------------------------------------------------------

class Command(BaseCommand):
    help = '聚合专精统计数据为 JSON 文件（Phase 1），供前端快速读取'

    def add_arguments(self, parser):
        parser.add_argument('--season-id', type=int, default=None,
                            help='指定赛季 ID，默认取当前活跃赛季')
        parser.add_argument('--class-name', default=None,
                            help='职业英文名（如 Warrior），不指定则处理全部职业')
        parser.add_argument('--spec-name', default=None,
                            help='专精英文名（如 Arms），不指定则处理该职业全部专精')
        parser.add_argument('--force', action='store_true',
                            help='覆盖已存在的 JSON 文件')

    def handle(self, *args, **options):
        force = options['force']
        filter_class = options['class_name']
        filter_spec = options['spec_name']

        # 确定赛季
        if options['season_id']:
            season = SeasonMeta.objects.filter(id=options['season_id']).first()
            if not season:
                raise CommandError(f'找不到赛季 ID={options["season_id"]}')
        else:
            season = SeasonMeta.objects.filter(is_active=True).first()
            if not season:
                raise CommandError('没有活跃赛季')

        season_id = season.id
        self.stdout.write(f'[聚合] 赛季 ID={season_id} ({season.season_key})')

        # 输出目录
        base_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))))),
            'media', 'aggregated', str(season_id))

        # 确定要处理的 (class, spec) 组合
        combos = []
        for cls, specs in CLASS_SPEC_MAP.items():
            if filter_class and cls != filter_class:
                continue
            for spec in specs:
                if filter_spec and spec != filter_spec:
                    continue
                combos.append((cls, spec))

        if not combos:
            raise CommandError('没有匹配的职业/专精组合')

        self.stdout.write(f'[聚合] 共 {len(combos)} 个职业/专精组合')

        # ===== Phase 1: 团本 =====
        self.stdout.write('\n===== 团本聚合 =====')
        self._aggregate_raid(season_id, base_dir, combos, force)

        # ===== Phase 2: M+ 副本 =====
        self.stdout.write('\n===== M+ 副本聚合 =====')
        self._aggregate_dungeon(season_id, base_dir, combos, force)

        # ===== Phase 3: 人物榜 =====
        self.stdout.write('\n===== 人物榜聚合 =====')
        self._aggregate_leaderboard(season_id, base_dir, combos, force)

        self.stdout.write(self.style.SUCCESS('\n[聚合] 全部完成'))

    # -----------------------------------------------------------------------
    # 团本
    # -----------------------------------------------------------------------
    def _aggregate_raid(self, season_id, base_dir, combos, force):
        # 收集所有 encounter_id
        all_boss_ids = set(
            SpecRaidRanking.objects.filter(season_id=season_id)
            .values_list('boss_id', flat=True).distinct()
        )
        if not all_boss_ids:
            self.stdout.write('  无团本数据')
            return

        # 构建 boss_id → boss_name 映射
        boss_name_map = {}
        for row in (SpecRaidRanking.objects.filter(season_id=season_id)
                    .values('boss_id', 'boss_name').distinct()):
            boss_name_map[row['boss_id']] = row['boss_name']

        total_files = 0
        for class_name, spec_name in combos:
            out_dir = os.path.join(base_dir, 'raid', f'{class_name}_{spec_name}')
            for boss_id in all_boss_ids:
                out_path = os.path.join(out_dir, f'{boss_id}.json')
                if not force and os.path.exists(out_path):
                    continue

                qs = SpecRaidRanking.objects.filter(
                    season_id=season_id, boss_id=boss_id,
                    class_name=class_name, spec_name=spec_name,
                )
                if not qs.exists():
                    continue

                records = list(qs.values(
                    'dps', 'kill_time', 'talents_json', 'talent_build_code',
                    'gear_json', 'faction', 'guild_name', 'character_name',
                ))
                if not records:
                    continue

                # DPS 统计
                dps_list = sorted([r['dps'] for r in records if r.get('dps')])
                dps_stats = _compute_dps_stats(dps_list)

                # Kill time 统计
                kt_list = sorted([r['kill_time'] for r in records if r.get('kill_time')])
                kt_stats = _compute_kill_time_stats(kt_list)

                # 天赋分布
                talent_dist = _compute_talent_distribution(records)

                # 装备热门度
                gear_pop = _compute_gear_popularity(records, top_n=10)

                # 宝石分布
                gem_dist = _compute_gem_distribution(records)

                # 阵营分布
                faction_dist = _compute_faction_distribution(records)

                # 公会分布
                guild_dist = _compute_guild_distribution(records)

                boss_name = boss_name_map.get(boss_id, '')
                cn_name = RAID_BOSS_CN.get(boss_name, boss_name)

                payload = {
                    'season_id': season_id,
                    'encounter_id': boss_id,
                    'boss_name': boss_name,
                    'boss_name_cn': cn_name,
                    'class_name': class_name,
                    'spec_name': spec_name,
                    'sample_count': len(records),
                    'dps': dps_stats,
                    'talents': talent_dist[:20],
                    'gear': gear_pop,
                    'race': {},  # raid 数据无种族字段
                    'factions': faction_dist,
                    'guilds': guild_dist,
                    'kill_time': kt_stats,
                    'stats': {},  # raid 数据无属性面板
                    'gems': gem_dist,
                }

                os.makedirs(out_dir, exist_ok=True)
                with open(out_path, 'w', encoding='utf-8') as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                total_files += 1

        self.stdout.write(f'  团本：已生成 {total_files} 个 JSON 文件')

    # -----------------------------------------------------------------------
    # M+ 副本
    # -----------------------------------------------------------------------
    def _aggregate_dungeon(self, season_id, base_dir, combos, force):
        all_dungeon_ids = set(
            SpecDungeonRanking.objects.filter(season_id=season_id)
            .values_list('dungeon_id', flat=True).distinct()
        )
        if not all_dungeon_ids:
            self.stdout.write('  无 M+ 数据')
            return

        # dungeon_id → dungeon_name 映射
        dungeon_name_map = {}
        for row in (SpecDungeonRanking.objects.filter(season_id=season_id)
                    .values('dungeon_id', 'dungeon_name').distinct()):
            dungeon_name_map[row['dungeon_id']] = row['dungeon_name']

        total_files = 0
        for class_name, spec_name in combos:
            out_dir = os.path.join(base_dir, 'dungeon', f'{class_name}_{spec_name}')
            for dungeon_id in all_dungeon_ids:
                out_path = os.path.join(out_dir, f'{dungeon_id}.json')
                if not force and os.path.exists(out_path):
                    continue

                qs = SpecDungeonRanking.objects.filter(
                    season_id=season_id, dungeon_id=dungeon_id,
                    class_name=class_name, spec_name=spec_name,
                )
                if not qs.exists():
                    continue

                records = list(qs.values(
                    'dps', 'clear_time', 'keystone_level', 'score',
                    'talents_json', 'talent_build_code',
                    'gear_json', 'faction', 'guild_name', 'character_name',
                ))
                if not records:
                    continue

                # DPS 统计
                dps_list = sorted([r['dps'] for r in records if r.get('dps')])
                dps_stats = _compute_dps_stats(dps_list)

                # 通关时间统计
                ct_list = sorted([r['clear_time'] for r in records if r.get('clear_time')])
                ct_stats = _compute_kill_time_stats(ct_list)

                # 钥石等级统计
                ks_list = sorted([r['keystone_level'] for r in records if r.get('keystone_level')])
                ks_stats = None
                if ks_list:
                    ks_stats = {
                        'avg': round(sum(ks_list) / len(ks_list), 1),
                        'max': max(ks_list),
                        'min': min(ks_list),
                    }

                # M+ 分数统计
                score_list = sorted([r['score'] for r in records if r.get('score')])
                score_stats = _compute_dps_stats(score_list)  # 复用 same structure
                if score_stats:
                    # rename dps fields to score fields
                    score_stats = {
                        'avg': score_stats['avg'],
                        'median': score_stats['median'],
                        'p25': score_stats['p25'],
                        'p75': score_stats['p75'],
                        'max': score_stats['max'],
                        'min': score_stats['min'],
                        'sample_count': score_stats['sample_count'],
                    }

                # 天赋分布
                talent_dist = _compute_talent_distribution(records)

                # 装备热门度
                gear_pop = _compute_gear_popularity(records, top_n=10)

                # 宝石分布
                gem_dist = _compute_gem_distribution(records)

                # 阵营分布
                faction_dist = _compute_faction_distribution(records)

                # 公会分布
                guild_dist = _compute_guild_distribution(records)

                dungeon_name = dungeon_name_map.get(dungeon_id, '')

                payload = {
                    'season_id': season_id,
                    'dungeon_id': dungeon_id,
                    'dungeon_name': dungeon_name,
                    'class_name': class_name,
                    'spec_name': spec_name,
                    'sample_count': len(records),
                    'dps': dps_stats,
                    'clear_time': ct_stats,
                    'keystone': ks_stats,
                    'score': score_stats,
                    'talents': talent_dist[:20],
                    'gear': gear_pop,
                    'race': {},
                    'factions': faction_dist,
                    'guilds': guild_dist,
                    'stats': {},
                    'gems': gem_dist,
                }

                os.makedirs(out_dir, exist_ok=True)
                with open(out_path, 'w', encoding='utf-8') as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                total_files += 1

        self.stdout.write(f'  M+：已生成 {total_files} 个 JSON 文件')

    # -----------------------------------------------------------------------
    # 人物榜（Leaderboard）
    # -----------------------------------------------------------------------
    def _aggregate_leaderboard(self, season_id, base_dir, combos, force):
        total_files = 0
        for class_name, spec_name in combos:
            out_dir = os.path.join(base_dir, 'leaderboard')
            out_path = os.path.join(out_dir, f'{class_name}_{spec_name}.json')
            if not force and os.path.exists(out_path):
                continue

            players = list(PlayerSpecTopPlayer.objects.filter(
                season_id=season_id, class_name=class_name, spec_name=spec_name,
            ).order_by('rank').values(
                'rank', 'character_name', 'realm', 'region', 'score',
                'faction', 'race', 'guild_name', 'item_level',
                'gear_json', 'stats_json', 'talent_build_code',
            ))

            if not players:
                continue

            # 装备热门度（从所有玩家 gear_json 汇总）
            gear_pop = _compute_gear_popularity(players, top_n=10)

            # 宝石分布
            gem_dist = _compute_gem_distribution(players)

            # 种族分布
            race_dist = _compute_race_distribution(players)

            # 阵营分布
            faction_dist = _compute_faction_distribution(players)

            # 属性分布
            stat_dist = _compute_stat_distribution(players)

            # 公会分布
            guild_dist = _compute_guild_distribution(players)

            # 玩家列表（精简版，不含完整 gear/stats JSON）
            player_list = []
            for p in players:
                player_list.append({
                    'rank': p['rank'],
                    'character_name': p['character_name'],
                    'realm': p['realm'],
                    'region': p['region'],
                    'score': p['score'],
                    'faction': p['faction'],
                    'race': p['race'],
                    'guild_name': p['guild_name'],
                    'item_level': p['item_level'],
                })

            payload = {
                'season_id': season_id,
                'class_name': class_name,
                'spec_name': spec_name,
                'sample_count': len(players),
                'players': player_list,
                'gear': gear_pop,
                'race': race_dist,
                'factions': faction_dist,
                'stats': stat_dist,
                'guilds': guild_dist,
                'gems': gem_dist,
            }

            os.makedirs(out_dir, exist_ok=True)
            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            total_files += 1

        self.stdout.write(f'  人物榜：已生成 {total_files} 个 JSON 文件')
