"""首页公开数据动态：只统计可见内容，不把监控检查时间当作更新。"""
from datetime import datetime, time
from zoneinfo import ZoneInfo

from django.db.models import Count, Max, Q
from django.http import JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import cache_page

from botend.journal_models import JournalState
from botend.models import (
    MythicDungeonDataVersion, SpecDungeonRanking, SpecRaidRanking,
    WowHotfixReport, WowItemVariantSnapshot, WowSkillDiffReport,
    WowTalentNodeMetadata,
)
from botend.services.gear_builder import active_season as gear_active_season
from botend.services.mplus_dps_rankings_service import active_season
from botend.wow.talents.versioning import TalentVersionResolver


SITE_TIMEZONE = ZoneInfo('Asia/Shanghai')


def build_site_updates(now=None):
    now = now or timezone.now()
    local_now = now.astimezone(SITE_TIMEZONE)
    start = datetime.combine(local_now.date(), time.min, tzinfo=SITE_TIMEZONE)
    items = []

    def add(key, label, url, updated_at, *, count=None, detail=''):
        updated_at = updated_at if updated_at and updated_at <= now else None
        today = bool(updated_at and updated_at >= start)
        status = 'today' if today else 'older' if updated_at else 'empty'
        summary = (f'今日新增 {count} 份报告' if count is not None else '今日已同步') if today else (
            '今日暂无更新' if updated_at else '暂无数据'
        )
        items.append({
            'key': key, 'label': label, 'url': url, 'status': status,
            'summary': summary, 'detail': detail,
            'updated_at': updated_at.astimezone(SITE_TIMEZONE).isoformat() if updated_at else None,
            'updated_label': updated_at.astimezone(SITE_TIMEZONE).strftime('%m-%d %H:%M') if updated_at else '',
            'today_count': count if count is not None else None,
        })

    for key, label, model, detail_route in (
        ('skill_diffs', '改动挖掘', WowSkillDiffReport, 'wow-skill-diff'),
        ('hotfixes', '热修报告', WowHotfixReport, 'wow-hotfix-report'),
    ):
        reports = model.objects.filter(created_at__lte=now).exclude(
            content_md='', content_html_path='',
        )
        stats = reports.aggregate(latest=Max('created_at'), today=Count('id', filter=Q(created_at__gte=start)))
        latest = reports.order_by('-created_at', '-id').first()
        add(key, label, f'/portal/{detail_route}/{latest.pk}/' if latest else '/portal/wow-updates/',
            stats['latest'], count=stats['today'], detail=getattr(latest, 'summary_title', '') or getattr(latest, 'to_build', ''))

    journal = JournalState.objects.filter(key='wow-zhCN', active_release__status='completed').select_related('active_release').first()
    release = journal.active_release if journal else None
    add('journal', '冒险手册', '/portal/adventure-journal/', release.completed_at if release else None,
        detail=release.build if release else '')

    season = active_season()
    dungeon_ids = [item.get('id') for item in (season.mplus_encounters or []) if isinstance(item, dict)] if season else []
    for key, label, model, url, filters in (
        ('mplus', '大秘境 DPS', SpecDungeonRanking, '/portal/mplus/dps-rankings/', {'dungeon_id__in': dungeon_ids}),
        ('raid', '团本数据', SpecRaidRanking, '/portal/specs/', {}),
    ):
        updated = model.objects.filter(season_id=season.pk, last_updated__lte=now, **filters).aggregate(
            latest=Max('last_updated'))['latest'] if season else None
        add(key, label, url, updated, detail=season.season_name if season else '')

    gear_season = gear_active_season()
    has_gear = bool(gear_season and gear_season.gear_batch_key and WowItemVariantSnapshot.objects.filter(
        season=gear_season, batch_key=gear_season.gear_batch_key).exists())
    add('gear', '装备目录', '/portal/gear-builder/', gear_season.gear_synced_at if has_gear else None)

    mdt = MythicDungeonDataVersion.objects.filter(is_active=True).order_by('-imported_at', '-id').first()
    add('mdt', 'MDT 副本数据', '/portal/mythic-planner/',
        mdt.imported_at if mdt and mdt.dungeons.exists() else None, detail=mdt.label if mdt else '')

    version = TalentVersionResolver.get_default()
    talent_time = WowTalentNodeMetadata.objects.filter(talent_version=version, last_updated__lte=now).aggregate(
        latest=Max('last_updated'))['latest'] if version else None
    add('talents', '天赋数据', '/portal/talents/', talent_time, detail=version.label if version else '')

    # 今日更新置顶；其余保持固定顺序，方便重复访问时寻找同一板块。
    items.sort(key=lambda item: item['status'] != 'today')
    return {
        'date': local_now.date().isoformat(), 'timezone': 'Asia/Shanghai',
        'checked_at': local_now.isoformat(), 'today_modules': sum(item['status'] == 'today' for item in items),
        'items': items,
    }


@method_decorator(cache_page(60), name='dispatch')
class PortalSiteUpdatesAPIView(View):
    def get(self, request):
        return JsonResponse(build_site_updates())
