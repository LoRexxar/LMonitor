# -*- coding: utf-8 -*-
"""
专精详情页视图
4 个页面：人物榜、玩家详情、M+ 副本统计、团本统计

数据来源：聚合 JSON 文件（由 SpecDetailAggregationMonitor 生成）
无 JSON 时显示「暂时没有内容」，不做实时查询。
"""

import json
import os
from datetime import datetime

from django.views import View
from django.shortcuts import render
from django.http import Http404

from botend.services.spec_stats_service import SpecStatsService
from botend.constants.wow import CLASS_SPEC_MAP, CLASS_CN, SPEC_CN, SPEC_ICON, SPEC_ROLE


AGGREGATED_DIR = os.path.join('media', 'aggregated')


def _validate_spec(class_name, spec_name):
    """验证 class/spec 合法性"""
    specs = CLASS_SPEC_MAP.get(class_name)
    if not specs or spec_name not in specs:
        raise Http404


def _base_context(class_name, spec_name):
    """所有页面共用的上下文"""
    season = SpecStatsService.get_active_season()
    nav = SpecStatsService.get_spec_nav(class_name, spec_name)

    all_specs = []
    for cls, specs in CLASS_SPEC_MAP.items():
        for sp in specs:
            all_specs.append({
                'class_name': cls,
                'spec_name': sp,
                'class_cn': CLASS_CN.get(cls, cls),
                'spec_cn': SPEC_CN.get(sp, sp),
                'icon': SPEC_ICON.get((cls, sp), ''),
                'role': SPEC_ROLE.get((cls, sp), 'dps'),
            })

    return {
        'season': season,
        'nav': nav,
        'class_name': class_name,
        'spec_name': spec_name,
        'all_specs': all_specs,
    }



def _talent_tree_has_hero(detail):
    trees = (((detail or {}).get('talent_popularity_tree') or {}).get('render_model') or {}).get('trees') or []
    return any(t.get('tree_type') == 'hero' and (t.get('nodes') or []) for t in trees)


def _talent_build_popularity_has_builds(detail):
    builds = (((detail or {}).get('talent_build_popularity') or {}).get('builds') or [])
    if not builds:
        return False
    return all('top_players' in build for build in builds if isinstance(build, dict))


def _load_json(season_id, class_name, spec_name, filename):
    """从聚合目录加载 JSON 文件，不存在返回 None"""
    path = os.path.join(AGGREGATED_DIR, str(season_id), class_name, spec_name, filename)
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _raid_overview_json_is_stale(season, zone_groups):
    """判断团本概览 JSON 是否落后于当前 SeasonMeta boss 配置。"""
    expected_ids = {enc.get('id') for enc in (season.raid_encounters or []) if enc.get('id')}
    if not expected_ids:
        return False
    json_ids = {
        boss.get('boss_id')
        for zone in (zone_groups or [])
        for boss in (zone.get('bosses') or [])
        if boss.get('boss_id')
    }
    return not expected_ids.issubset(json_ids)


def _detail_item_metadata_is_stale(detail):
    """旧聚合 JSON 可能缺少装备/宝石/附魔名称或图标，需要实时重算详情。"""
    for key in ('gear_popularity', 'gem_popularity'):
        entries = (detail or {}).get(key) or []
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            display_name = str(entry.get('display_name') or entry.get('name_zh') or entry.get('name') or '').strip()
            icon = str(entry.get('icon') or '').strip()
            item_id = entry.get('id') or entry.get('item_id')
            if item_id and (not icon or not display_name or display_name == f'#{item_id}'):
                return True
    enchant_groups = (detail or {}).get('enchant_popularity') or []
    if isinstance(enchant_groups, list):
        for group in enchant_groups:
            if not isinstance(group, dict):
                continue
            if not group.get('slot_label') or not isinstance(group.get('enchants'), list):
                return True
            for entry in group.get('enchants') or []:
                if not isinstance(entry, dict):
                    continue
                display_name = str(entry.get('display_name') or entry.get('name_zh') or entry.get('name') or '').strip()
                icon = str(entry.get('icon') or '').strip()
                item_id = entry.get('id') or entry.get('item_id')
                if not entry.get('slot_label') or not entry.get('display_label'):
                    return True
                if item_id and (not icon or not display_name or display_name == f'#{item_id}'):
                    return True
    return False


class SpecDetailPlayerView(View):
    """人物榜页面"""

    def get(self, request, class_name, spec_name):
        _validate_spec(class_name, spec_name)
        ctx = _base_context(class_name, spec_name)
        season_id = ctx['season'].id if ctx['season'] else None

        if season_id:
            data = SpecStatsService.get_player_list(class_name, spec_name, season_id=season_id)
            ctx.update(data)
            return render(request, 'portal/spec_detail/player_list.html', ctx)

        ctx['players'] = []
        ctx['total'] = 0
        return render(request, 'portal/spec_detail/player_list.html', ctx)


class SpecDetailPlayerDetailView(View):
    """单个玩家详情页"""

    def get(self, request, class_name, spec_name, player_id):
        _validate_spec(class_name, spec_name)
        ctx = _base_context(class_name, spec_name)
        ctx['player_detail'] = SpecStatsService.get_player_detail(player_id)

        if not ctx['player_detail']:
            raise Http404

        return render(request, 'portal/spec_detail/player_detail.html', ctx)


class SpecDetailDungeonView(View):
    """M+ 副本统计页面"""

    def get(self, request, class_name, spec_name):
        _validate_spec(class_name, spec_name)
        ctx = _base_context(class_name, spec_name)
        season_id = ctx['season'].id if ctx['season'] else None
        dungeon_id = request.GET.get('dungeon_id')

        if season_id:
            data = _load_json(season_id, class_name, spec_name, 'dungeon.json')
            if data:
                dungeons = data.get('dungeons', [])
                if dungeon_id:
                    did = int(dungeon_id)
                    detail = next((d for d in dungeons if d.get('dungeon_id') == did), None)
                    if detail:
                        # 兼容旧聚合 JSON：若天赋树缺英雄天赋、新维度缺失或天赋字符串为空，则实时重算该详情对象
                        if (
                            (not _talent_tree_has_hero(detail))
                            or ('secondary_stats' not in detail)
                            or (not _talent_build_popularity_has_builds(detail))
                            or _detail_item_metadata_is_stale(detail)
                        ):
                            detail = SpecStatsService.get_dungeon_detail(did, class_name, spec_name) or detail
                        ctx['dungeon_detail'] = detail
                    else:
                        ctx['dungeons'] = dungeons
                else:
                    ctx['dungeons'] = dungeons
                return render(request, 'portal/spec_detail/dungeon_stats.html', ctx)

        # 无 JSON → 空数据
        return render(request, 'portal/spec_detail/dungeon_stats.html', ctx)


class SpecDetailRaidView(View):
    """团本统计页面"""

    def get(self, request, class_name, spec_name):
        _validate_spec(class_name, spec_name)
        ctx = _base_context(class_name, spec_name)
        season_id = ctx['season'].id if ctx['season'] else None
        boss_id = request.GET.get('boss_id')

        if season_id:
            data = _load_json(season_id, class_name, spec_name, 'raid.json')
            if data:
                zone_groups = data.get('zone_groups', [])
                if _raid_overview_json_is_stale(ctx['season'], zone_groups):
                    zone_groups = SpecStatsService.get_raid_overview(class_name, spec_name, season_id)
                if boss_id:
                    bid = int(boss_id)
                    detail = None
                    for zg in zone_groups:
                        for b in zg.get('bosses', []):
                            if b.get('boss_id') == bid:
                                detail = b
                                break
                        if detail:
                            break
                    if detail:
                        # 兼容旧聚合 JSON：若天赋树缺英雄天赋、新维度缺失或天赋字符串为空，则实时重算该详情对象
                        if (
                            (not _talent_tree_has_hero(detail))
                            or ('secondary_stats' not in detail)
                            or (not _talent_build_popularity_has_builds(detail))
                            or _detail_item_metadata_is_stale(detail)
                        ):
                            detail = SpecStatsService.get_raid_detail(bid, class_name, spec_name) or detail
                        ctx['boss_detail'] = detail
                    else:
                        ctx['zone_groups'] = zone_groups
                else:
                    ctx['zone_groups'] = zone_groups
                return render(request, 'portal/spec_detail/raid_stats.html', ctx)

        # 无 JSON → 空数据
        return render(request, 'portal/spec_detail/raid_stats.html', ctx)
