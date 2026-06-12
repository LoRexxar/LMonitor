# -*- coding: utf-8 -*-
"""
专精详情页视图
3 个独立页面：人物榜、M+ 副本统计、团本统计
"""

from django.views import View
from django.shortcuts import render
from django.http import Http404

from botend.services.spec_stats_service import SpecStatsService
from botend.constants.wow import CLASS_SPEC_MAP, CLASS_CN, SPEC_CN, SPEC_ICON, SPEC_ROLE


def _validate_spec(class_name, spec_name):
    """验证 class/spec 合法性"""
    specs = CLASS_SPEC_MAP.get(class_name)
    if not specs or spec_name not in specs:
        raise Http404


def _base_context(class_name, spec_name):
    """所有页面共用的上下文"""
    season = SpecStatsService.get_active_season()
    nav = SpecStatsService.get_spec_nav(class_name, spec_name)

    # 所有专精列表（用于侧栏导航）
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


class SpecDetailPlayerView(View):
    """人物榜页面"""

    def get(self, request, class_name, spec_name):
        _validate_spec(class_name, spec_name)

        ctx = _base_context(class_name, spec_name)
        page = int(request.GET.get('page', 1))

        player_data = SpecStatsService.get_player_list(class_name, spec_name, page=page)
        ctx.update(player_data)

        # 如果有 player_id 参数，加载详情
        player_id = request.GET.get('player_id')
        if player_id:
            ctx['player_detail'] = SpecStatsService.get_player_detail(int(player_id))

        return render(request, 'portal/spec_detail/player_list.html', ctx)


class SpecDetailDungeonView(View):
    """M+ 副本统计页面"""

    def get(self, request, class_name, spec_name):
        _validate_spec(class_name, spec_name)

        ctx = _base_context(class_name, spec_name)

        # 获取某个副本的详情
        dungeon_id = request.GET.get('dungeon_id')
        if dungeon_id:
            ctx['dungeon_detail'] = SpecStatsService.get_dungeon_detail(
                int(dungeon_id), class_name, spec_name
            )
        else:
            ctx['dungeons'] = SpecStatsService.get_dungeon_overview(class_name, spec_name)

        return render(request, 'portal/spec_detail/dungeon_stats.html', ctx)


class SpecDetailRaidView(View):
    """团本统计页面"""

    def get(self, request, class_name, spec_name):
        _validate_spec(class_name, spec_name)

        ctx = _base_context(class_name, spec_name)

        # 获取某个 Boss 的详情
        boss_id = request.GET.get('boss_id')
        if boss_id:
            ctx['boss_detail'] = SpecStatsService.get_raid_detail(
                int(boss_id), class_name, spec_name
            )
        else:
            ctx['bosses'] = SpecStatsService.get_raid_overview(class_name, spec_name)

        return render(request, 'portal/spec_detail/raid_stats.html', ctx)
