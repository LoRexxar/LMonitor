# -*- coding: utf-8 -*-
"""
专精统计聚合采集器
从数据库计算各专精的副本/团本/人物榜统计，输出 JSON 文件供 Portal 直接读取。
"""

import json
import os
import tempfile
from contextlib import nullcontext
from functools import partial
from decimal import Decimal
from datetime import date, datetime

from django.conf import settings
from django.utils import timezone

from botend.controller.BaseScan import BaseScan
from botend.models import SeasonMeta
from botend.constants.wow import CLASS_SPEC_MAP, RAID_BOSS_CN, RAID_ZONE_CN
from botend.services.spec_stats_service import (
    SpecStatsService,
)
from botend.services.wcl_collection_cycle import (
    CycleOwnershipLost, begin_aggregation, defer_cycle, fenced_cycle, finish_publication,
    legacy_publication, frozen_season,
)

from utils.log import logger


class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, (date, datetime)):
            return o.isoformat()
        return super().default(o)


def atomic_dump_json(path, payload, **dump_kwargs):
    """原子写 JSON：写入失败或进程中断时保留旧文件。"""
    from pathlib import Path
    from django.conf import settings
    maximum = (int(getattr(settings, 'SPEC_STATS_DETAIL_MAX_BYTES', 64 * 1024 * 1024))
               if Path(path).parent.parent.name == 'stats-details' else None)
    if maximum is not None and maximum < 1024:
        raise ValueError('统计详情文件预算必须至少为1024字节')
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix='.tmp-', suffix='.json', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            # 流式编码并在写入前计量，异常大投影不能把磁盘写满才失败。
            encoder_class = dump_kwargs.pop('cls', json.JSONEncoder)
            encoder = encoder_class(**dump_kwargs)
            written = 0
            for part in encoder.iterencode(payload):
                written += len(part.encode('utf-8'))
                if maximum is not None and written > maximum:
                    raise ValueError('统计详情超过单文件预算，请检查字段体积诊断')
                f.write(part)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def publish_stats(path, payload, class_name, spec_name, *, cycle=None, legacy_season=None):
    """后台统一补全装备与天赋，页面只读取已发布的展示结果。"""
    if payload.get('data_branch', 'retail') != 'retail':
        raise ValueError('正式服统计目录不能发布测试服资料')
    payload['data_branch'] = 'retail'
    # 输入指纹已经包含展示依赖时，在昂贵的展示补全之前验证整个已发布版本。
    from botend.services.spec_stats_snapshot import reusable_projection
    if payload.get('_input_fingerprint'):
        keys = ([f"dungeon-{row['dungeon_id']}" for row in payload.get('dungeons', [])] + ['dungeon-all']
                if os.path.basename(path) == 'dungeon.json' else
                [f"raid-{item['difficulty']}-{boss['boss_id']}" for item in payload.get('difficulties', [])
                 for zone in item.get('zone_groups', []) for boss in zone.get('bosses', [])])
        if reusable_projection(path, payload['_input_fingerprint'], keys) is not None:
            return
    from botend.services.wow_item_display import refresh_aggregate_equipment
    from botend.services.wow_talent_display import refresh_aggregate_talents
    payload = refresh_aggregate_equipment(payload, class_name=class_name, spec_name=spec_name)
    payload = refresh_aggregate_talents(payload, class_name=class_name, spec_name=spec_name)
    payload['generated_at'] = timezone.now().isoformat()
    from botend.services.spec_stats_snapshot import publish_projection
    # Enrichment may read external metadata; only final local publication holds
    # the fence. A replacement owner can never publish through an old callback.
    guard = (fenced_cycle(cycle, phases=('aggregating',)) if cycle else
             legacy_publication(legacy_season) if legacy_season else nullcontext())
    from botend.services.stats_talent_dependencies import publication_metadata_guard
    with guard, publication_metadata_guard(class_name, payload.get('_talent_structure_token')):
        publish_projection(path, payload, lambda target, data: atomic_dump_json(
            target, data, cls=DecimalEncoder, ensure_ascii=False, allow_nan=False,
            separators=(',', ':')))


class SpecDetailAggregationMonitor(BaseScan):

    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task

    def scan(self, url):
        logger.info("[SpecDetailAggregation] 开始聚合统计")

        season = SeasonMeta.objects.filter(is_active=True).first()
        if not season:
            logger.error("[SpecDetailAggregation] 无活跃赛季，跳过")
            return False

        base_dir = os.path.join(getattr(settings, 'MEDIA_ROOT', '') or 'media', 'aggregated', str(season.id))
        try:
            cycle = begin_aggregation(season, self.task)
        except CycleOwnershipLost:
            return False
        try:
            # This source is independent of WCL, including on the daily 06:00 run.
            for class_name, specs in CLASS_SPEC_MAP.items():
                for spec_name in specs:
                    spec_dir = os.path.join(base_dir, class_name, spec_name)
                    self._aggregate_leaderboard(season.id, class_name, spec_name, spec_dir)
            if cycle is None:
                from botend.services.wcl_collection_cycle import active_cycle
                pending = active_cycle(season.pk)
                logger.info('[SpecDetailAggregation] 团本战斗统计未启动：%s；仅刷新人物榜',
                            f'周期={pending.pk}，阶段={pending.phase}，重试时间={pending.next_retry_at}'
                            if pending else '无待发布周期且无需要补建的已完成周期')
                return True
            logger.info('[SpecDetailAggregation] 启动普通团本统计：周期=%s，数据截止=%s',
                        getattr(cycle, 'pk', '未知'), cycle.collected_at)
            season = frozen_season(cycle, season)
            for class_name, specs in CLASS_SPEC_MAP.items():
                for spec_name in specs:
                    spec_dir = os.path.join(base_dir, class_name, spec_name)
                    self._aggregate_dungeon(season, class_name, spec_name, spec_dir, cycle=cycle)
                    self._aggregate_raid(season, class_name, spec_name, spec_dir,
                                         publish=partial(publish_stats, cycle=cycle))
            finish_publication(cycle)
            return True
        except CycleOwnershipLost:
            logger.warning('[SpecDetailAggregation] cycle ownership lost; stop publication')
            return False
        except Exception as exc:
            # Exception text may include credentials from upstream metadata URLs.
            error = f'publication_failed:{type(exc).__name__}'
            if cycle is not None:
                try:
                    defer_cycle(cycle, error, retry_at=getattr(exc, 'retry_at', None))
                except CycleOwnershipLost:
                    pass
            logger.error('[SpecDetailAggregation] %s', error)
            return False

    @staticmethod
    def _aggregate_dungeon(season, class_name, spec_name, spec_dir, *, cycle=None, publish=None):
        if not season.mplus_encounters:
            return

        from botend.services.dungeon_aggregation import aggregate_dungeon
        kwargs = {}
        if cycle is not None:
            with fenced_cycle(cycle, phases=('aggregating',)):
                pass
            kwargs = {'selected_ids': cycle.selected_samples[f'{class_name}/{spec_name}'],
                      'source_cutoff': cycle.collected_at}
        return aggregate_dungeon(season, class_name, spec_name,
            os.path.join(spec_dir, 'dungeon.json'),
            publish or (partial(publish_stats, cycle=cycle) if cycle else publish_stats), **kwargs)

    @staticmethod
    def _aggregate_raid(season, class_name, spec_name, spec_dir, *, publish=None):
        if not season.raid_encounters and not season.raid_zones:
            return

        from botend.services.raid_aggregation import raid_stats
        from botend.services.spec_stats_snapshot import content_digest
        from botend.services.stats_talent_dependencies import structure_token
        token = structure_token(class_name)
        fingerprints = []
        reused = 0

        def compute(enc, cn_name, difficulty):
            nonlocal reused
            result, fingerprint, hit = raid_stats(
                season.id, enc['id'], cn_name, class_name, spec_name, difficulty)
            fingerprints.append([difficulty, enc['id'], fingerprint])
            reused += int(hit)
            return result

        def aggregate_difficulty(difficulty):
            if season.raid_zones:
                zone_groups = []
                for rz in season.raid_zones:
                    zone_cn = RAID_ZONE_CN.get(rz.get('name', ''), rz.get('name', ''))
                    zone_bosses = []
                    for enc in rz.get('encounters', []):
                        cn_name = RAID_BOSS_CN.get(enc['name'], enc['name'])
                        stats = compute(enc, cn_name, difficulty)
                        stats['raid_zone_id'] = rz.get('id')
                        stats['raid_zone_name'] = rz.get('name', '')
                        stats['raid_zone_cn'] = zone_cn
                        zone_bosses.append(stats)
                    if zone_bosses:
                        zone_groups.append({
                            'zone_id': rz.get('id'),
                            'zone_name': rz.get('name', ''),
                            'zone_cn': zone_cn,
                            'bosses': zone_bosses,
                        })
                return zone_groups

            bosses = []
            for enc in season.raid_encounters:
                cn_name = RAID_BOSS_CN.get(enc['name'], enc['name'])
                bosses.append(compute(enc, cn_name, difficulty))
            return [{'zone_id': 0, 'zone_name': '', 'zone_cn': '', 'bosses': bosses}]

        mythic_zones = aggregate_difficulty(5)
        heroic_zones = aggregate_difficulty(4)

        path = os.path.join(spec_dir, 'raid.json')
        (publish or publish_stats)(path, {
            '_talent_structure_token': token,
            '_input_fingerprint': content_digest({'algorithm': 'raid-publication-v4-current-metadata', 'scopes': fingerprints,
                'zones': season.raid_zones, 'encounters': season.raid_encounters}),
            'zone_groups': mythic_zones,
            'difficulties': [
                {'difficulty': 5, 'label': '史诗团本表现', 'zone_groups': mythic_zones},
                {'difficulty': 4, 'label': '英雄团本表现', 'zone_groups': heroic_zones},
            ],
        }, class_name, spec_name)
        logger.info('[SpecDetailAggregation] %s/%s 团本计算复用 %s/%s',
                    class_name, spec_name, reused, len(fingerprints))

    @staticmethod
    def refresh_leaderboard_projection(season_id, class_name, spec_name):
        """只刷新人物榜投影；供10分钟巅峰榜轻量更新复用。"""
        media_root = getattr(settings, 'MEDIA_ROOT', '') or 'media'
        spec_dir = os.path.join(media_root, 'aggregated', str(season_id), class_name, spec_name)
        return SpecDetailAggregationMonitor._aggregate_leaderboard(
            season_id, class_name, spec_name, spec_dir,
        )

    @staticmethod
    def _aggregate_leaderboard(season_id, class_name, spec_name, spec_dir):
        result = SpecStatsService.get_player_list(
            class_name, spec_name, season_id=season_id, page=1, page_size=20
        )
        # last_updated 是人物内容时间；榜单投影必须展示本次排名刷新时间。
        result['updated_at'] = timezone.now().isoformat()

        path = os.path.join(spec_dir, 'leaderboard.json')
        publish_stats(path, result, class_name, spec_name)
        return path
