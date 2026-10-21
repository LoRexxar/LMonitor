# -*- coding: utf-8 -*-
"""
M+ 副本 + 团本排名采集器
从 WCL v2 GraphQL 获取每个副本/Boss 每个专精的 Top 100 排名原始数据
"""

import math
import time

from django.utils import timezone
from django.db import transaction

from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
from botend.models import SeasonMeta, SpecDungeonRanking, SpecRaidRanking, MonitorTaskLeaseLost
from botend.constants.wow import CLASS_SPEC_MAP
from botend.services.wcl_collection_cycle import (
    CycleOwnershipLost, begin_collection, checkpoint_scope, defer_cycle,
    fenced_cycle, finish_collection, freeze_samples, frozen_combatant_records,
)

from utils.log import logger


class SpecDetailRankingMonitor(SpecDetailBase):
    DUNGEON_KEY_FIELDS = (
        'class_name', 'spec_name', 'character_name', 'realm', 'region',
        'report_code', 'fight_id',
    )
    DUNGEON_UPDATE_FIELDS = (
        'dungeon_name', 'dps', 'keystone_level', 'clear_time', 'score', 'medal',
        'affixes', 'talents_json', 'talent_build_code', 'gear_json', 'faction',
        'guild_name',
    )
    RAID_KEY_FIELDS = (
        'class_name', 'spec_name', 'character_name', 'realm', 'region',
        'report_code', 'fight_id',
    )
    RAID_UPDATE_FIELDS = (
        'boss_name', 'raid_zone_id', 'raid_zone_name', 'dps', 'kill_time',
        'talents_json', 'talent_build_code', 'gear_json', 'faction', 'guild_name',
    )

    def __init__(self, req, task):
        super().__init__(req, task)

    def scan(self, url):
        logger.info("[SpecDetailRanking] 开始采集排名数据")

        season = SeasonMeta.objects.filter(is_active=True).first()
        if not season:
            logger.warning("[SpecDetailRanking] 无活跃赛季，先触发 SeasonMonitor")
            from botend.controller.plugins.portal.SpecDetailSeasonMonitor import SpecDetailSeasonMonitor
            sm = SpecDetailSeasonMonitor(self.req, self.task)
            sm.scan('')
            season = SeasonMeta.objects.filter(is_active=True).first()
        if not season:
            logger.error("[SpecDetailRanking] SeasonMonitor 执行后仍无活跃赛季，跳过")
            return False

        try:
            self._cycle = begin_collection(season, self.task)
        except CycleOwnershipLost:
            return False
        if self._cycle is None:
            return False
        self._wcl_retry_at = None
        try:
            if self._cycle.phase == 'rankings':
                if season.mplus_encounters and not self._collect_dungeon_rankings(season):
                    defer_cycle(self._cycle, 'dungeon rankings incomplete', self._wcl_retry_at)
                    return False
                if season.raid_encounters:
                    for difficulty, name in ((5, 'Mythic'), (4, 'Heroic')):
                        if not self._collect_raid_rankings(season, difficulty, name):
                            defer_cycle(self._cycle, 'raid rankings incomplete', self._wcl_retry_at)
                            return False
                identities = [(cls, spec) for cls, specs in CLASS_SPEC_MAP.items() for spec in specs]
                freeze_samples(self._cycle, season, identities)

            # All rankings finish before one global report/fight supplement.
            # Retries read frozen IDs, never a newly selected cohort.
            from botend.services.wcl_combatant_snapshot import collect_dungeon_combatants
            records = frozen_combatant_records(self._cycle)
            def save_progress(counts):
                # The collector calls this only on the main thread, at most
                # every 30s plus start/end. Do not alter phase or retry state.
                with fenced_cycle(self._cycle, phases=('combatants',)) as current:
                    current.counts = counts
                    current.save(update_fields=['counts'])
                    self._cycle.counts = counts

            counts = collect_dungeon_combatants(
                records, self, apply=True, batch_size=20, log=logger.info, progress=save_progress)
            ok = finish_collection(self._cycle, counts)
            if ok and self.task is not None:
                self.task.flag = f"{season.season_key}@rankings@{self._cycle.pk}"
                self.task.save(update_fields=['flag'])
            return ok
        except (CycleOwnershipLost, MonitorTaskLeaseLost):
            logger.warning('[SpecDetailRanking] cycle ownership lost; no stale completion')
            return False
        except Exception as exc:
            try:
                defer_cycle(self._cycle, str(exc), self._wcl_retry_at)
            except CycleOwnershipLost:
                pass
            logger.exception('[SpecDetailRanking] collection deferred')
            return False

    def _scope_complete(self, scope):
        cycle = getattr(self, '_cycle', None)
        return cycle is not None and scope in cycle.completed_scopes

    def _checkpoint(self, scope):
        cycle = getattr(self, '_cycle', None)
        if cycle is not None:
            checkpoint_scope(cycle, scope)

    def _scope_transaction(self):
        cycle = getattr(self, '_cycle', None)
        return fenced_cycle(cycle, phases=('rankings',)) if cycle else transaction.atomic()

    def _fetch_rankings_with_retry(self, enc_id, class_name, spec_name, metric='dps', difficulty=None, page=1, max_retries=3):
        """带加强重试的排名获取，处理限流和临时错误"""
        from botend.services.spec_aggregation_cache import load_work, save_work
        from botend.services.spec_stats_snapshot import content_digest
        cycle = getattr(self, '_cycle', None)
        scope = f'ranking-page:{enc_id}:{class_name}:{spec_name}:{metric}:{difficulty}:{page}'
        fingerprint = content_digest(['ranking-page-v1', cycle.pk]) if cycle else None
        if cycle:
            with fenced_cycle(cycle, phases=('rankings',)):
                cached = load_work(cycle.season_id, scope, fingerprint)
            if cached is not None:
                return cached
        for attempt in range(max_retries):
            rankings = self.fetch_wcl_rankings(enc_id, class_name, spec_name, metric, difficulty=difficulty, page=page)
            if rankings is not None:
                if cycle and self._valid_ranking_page(rankings):
                    with fenced_cycle(cycle, phases=('rankings',)):
                        save_work(cycle.season_id, scope, fingerprint, rankings)
                return rankings
            if getattr(self, '_wcl_retry_at', None):
                return None
            if attempt + 1 == max_retries:
                break

            # fetch_wcl_rankings 返回 None 可能是限流或临时错误
            wait = min(2 ** attempt * 3, 60)
            logger.warning(f"[SpecDetailRanking] 获取失败 {enc_id}/{class_name}/{spec_name}/page={page}, "
                          f"重试 {attempt+1}/{max_retries}, 等待 {wait}s")
            time.sleep(wait)

        logger.error(f"[SpecDetailRanking] 获取失败（已耗尽重试）: {enc_id}/{class_name}/{spec_name}/page={page}")
        return None

    @staticmethod
    def _valid_ranking_page(payload):
        """仅保存结构完整的成功页，坏页和临时空页下轮重新请求。"""
        if (not isinstance(payload, dict) or not isinstance(payload.get('rankings'), list)
                or not isinstance(payload.get('hasMorePages'), bool)
                or (not payload['rankings'] and payload['hasMorePages'])):
            return False
        for row in payload['rankings']:
            if not isinstance(row, dict):
                return False
            if any(row.get(key) is not None and not isinstance(row[key], dict)
                   for key in ('server', 'report', 'guild')):
                return False
            if any(row.get(key) is not None and not isinstance(row[key], list)
                   for key in ('talents', 'gear', 'affixes')):
                return False
            value = row.get('amount')
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                return False
            # 请求成功不代表可入库；不将错误字段永久固定在本轮检查点中。
            for key in ('duration', 'hardModeLevel', 'faction', 'fightID'):
                value = row.get(key)
                if value is not None and (type(value) is not int):
                    return False
            if 'name' in row and not isinstance(row['name'], str):
                return False
            for key, fields in (('server', ('name', 'region')), ('report', ('code',)), ('guild', ('name',))):
                if any(field in (row.get(key) or {}) and not isinstance(row[key][field], str) for field in fields):
                    return False
            report = row.get('report') or {}
            if report.get('fightID') is not None and type(report['fightID']) is not int:
                return False
        return True

    def _collect_dungeon_rankings(self, season):
        """采集 M+ 副本排名：按副本成功后替换，失败保留旧数据。"""
        logger.info(f"[SpecDetailRanking] 采集 M+ 排名: {len(season.mplus_encounters)} 副本 x {sum(len(v) for v in CLASS_SPEC_MAP.values())} 专精")

        target_per_spec = 300
        total = 0
        empty_talent_total = 0
        now = timezone.now()
        ok = True

        for encounter in season.mplus_encounters:
            enc_id = encounter['id']
            scope = f'dungeon:{enc_id}'
            if self._scope_complete(scope):
                continue
            enc_name = encounter['name']
            records = []
            encounter_failed = False

            for class_name, specs in CLASS_SPEC_MAP.items():
                if encounter_failed:
                    break
                for spec_name in specs:
                    if encounter_failed:
                        break
                    page = 1
                    collected = 0
                    empty_talent_count = 0

                    while collected < target_per_spec:
                        rankings = self._fetch_rankings_with_retry(enc_id, class_name, spec_name, 'dps', page=page)
                        if rankings is None:
                            encounter_failed = True
                            time.sleep(0.3)
                            break
                        if (not isinstance(rankings, dict)
                                or not isinstance(rankings.get('rankings'), list)
                                or not isinstance(rankings.get('hasMorePages'), bool)):
                            encounter_failed = True
                            logger.error(f'[SpecDetailRanking] M+ {enc_id}: 排名页结构无效')
                            break

                        rank_list = rankings['rankings']
                        if not rank_list:
                            if rankings['hasMorePages']:
                                encounter_failed = True
                                logger.error(f'[SpecDetailRanking] M+ {enc_id}: 分页未结束却返回空页')
                            break

                        for r in rank_list:
                            if collected >= target_per_spec:
                                break
                            try:
                                if not isinstance(r, dict):
                                    raise ValueError('ranking must be an object')
                                for key in ('server', 'report', 'guild'):
                                    if r.get(key) is not None and not isinstance(r[key], dict):
                                        raise ValueError(f'invalid ranking {key}')
                                for key in ('talents', 'gear', 'affixes'):
                                    if r.get(key) is not None and not isinstance(r[key], list):
                                        raise ValueError(f'invalid ranking {key}')
                                amount = r.get('amount')
                                if (isinstance(amount, bool)
                                        or not isinstance(amount, (int, float))
                                        or not math.isfinite(amount)):
                                    raise ValueError('invalid ranking amount')
                                server = r.get('server', {}) or {}
                                report = r.get('report', {}) or {}
                                guild = r.get('guild', {}) or {}

                                # Missing nodes are projected from the central log
                                # snapshot after cohort selection, never fetched per row.
                                talents_payload = self.parse_wcl_talents(r.get('talents', []))
                                if not talents_payload:
                                    empty_talent_count += 1
                                    empty_talent_total += 1
                                records.append(SpecDungeonRanking(
                                    season_id=season.id,
                                    dungeon_id=enc_id,
                                    dungeon_name=enc_name,
                                    class_name=class_name,
                                    spec_name=spec_name,
                                    character_name=r.get('name', ''),
                                    realm=server.get('name', ''),
                                    region=server.get('region', ''),
                                    dps=r.get('amount', 0),
                                    keystone_level=r.get('hardModeLevel'),
                                    clear_time=r.get('duration'),
                                    score=r.get('score'),
                                    medal=r.get('medal', ''),
                                    affixes=r.get('affixes', []),
                                    talents_json=talents_payload,
                                    # WCL ranking payload only provides structured selected nodes,
                                    # not Blizzard's frozen import string.  Do not synthesize a
                                    # share code from an unrelated profile reference: it can omit
                                    # selections and imports as a different build.
                                    talent_build_code='',
                                    gear_json=self.parse_wcl_gear(r.get('gear', [])),
                                    faction=r.get('faction'),
                                    guild_name=guild.get('name', ''),
                                    report_code=report.get('code', ''),
                                    fight_id=report.get('fightID'),
                                    last_updated=now,
                                ))
                                collected += 1
                            except Exception as e:
                                encounter_failed = True
                                logger.warning(f"[SpecDetailRanking] M+ 构建记录失败，整本保旧: {e}")
                                break

                        if encounter_failed:
                            break
                        if not rankings.get('hasMorePages') or len(rank_list) == 0:
                            break
                        page += 1
                        time.sleep(0.3)

                    if empty_talent_count:
                        logger.info(
                            f"[SpecDetailRanking] M+ {enc_id}/{class_name}/{spec_name}: "
                            f"{empty_talent_count}/{collected} 条记录缺失天赋数据（保留 DPS/装备样本，天赋聚合会排除）"
                        )
                    logger.info(f"[SpecDetailRanking] M+ {enc_id}/{class_name}/{spec_name}: 拉取 {collected} 条")
                    time.sleep(0.3)  # 限速

            if encounter_failed:
                ok = False
                logger.error(f"[SpecDetailRanking] M+ {enc_id} ({enc_name}) 采集失败，保留旧数据不覆盖")
                return False
            if not records:
                self._checkpoint(scope)
                logger.info(f"[SpecDetailRanking] M+ {enc_id} ({enc_name}) 权威空榜，保旧并完成范围")
                continue

            try:
                with self._scope_transaction():
                    result = self._sync_ranking_records(
                        model=SpecDungeonRanking,
                        filters={'season_id': season.id, 'dungeon_id': enc_id},
                        records=records,
                        key_fields=self.DUNGEON_KEY_FIELDS,
                        update_fields=self.DUNGEON_UPDATE_FIELDS,
                    )
                    self._checkpoint(scope)
                total += len(records)
                logger.info(
                    f"[SpecDetailRanking] M+ {enc_id} ({enc_name}): "
                    f"新增 {result['created']} / 更新 {result['updated']} / "
                    f"删除 {result['deleted']} / 未变化 {result['unchanged']}"
                )
            except CycleOwnershipLost:
                raise
            except Exception as e:
                ok = False
                logger.error(f"[SpecDetailRanking] M+ {enc_id} ({enc_name}) 写入失败: {e}")

        logger.info(f"[SpecDetailRanking] M+ 排名采集完成: {total} 条, 空天赋 {empty_talent_total} 条")
        return ok

    def _parse_rank_talents(self, ranking, combatant_cache, *, class_name=None, spec_name=None):
        """优先使用 ranking 自带 talents，缺失时再尝试 report CombatantInfo.talentTree。"""
        talents = self.parse_wcl_talents(ranking.get('talents', []))
        if talents:
            return talents

        if class_name and spec_name:
            from botend.services.wcl_combatant_snapshot import (
                collect_dungeon_combatants, _load_facts, _fight_key, match_combatant)
            report, server = ranking.get('report') or {}, ranking.get('server') or {}
            row = {'report_code': report.get('code'), 'fight_id': report.get('fightID') or ranking.get('fightID'),
                   'character_name': ranking.get('name'), 'realm': server.get('name'),
                   'region': server.get('region'), 'class_name': class_name, 'spec_name': spec_name}
            # 团本补天赋也复用中央日志事实和负缓存，避免每个难度重抓同一战斗。
            counts = collect_dungeon_combatants([row], self, apply=True)
            if counts.get('failed') or counts.get('pending'):
                raise ValueError('raid talent fallback incomplete')
            facts = _load_facts([row])
            combatant = match_combatant(facts.get(_fight_key(row), []), row, class_name, spec_name)
            return self.parse_wcl_talent_tree((combatant or {}).get('talentTree') or [])

        report = ranking.get('report', {}) or {}
        report_code = report.get('code', '')
        fight_id = report.get('fightID') or ranking.get('fightID')
        character_name = ranking.get('name', '')

        if report_code and fight_id and character_name:
            cache_key = (report_code, int(fight_id))
            if cache_key not in combatant_cache:
                if len(combatant_cache) >= 256:
                    combatant_cache.clear()
                fetched = self.fetch_wcl_combatant_info(report_code, fight_id)
                if fetched is None:
                    if getattr(self, '_wcl_last_error', '') != 'permission':
                        raise ValueError('raid talent fallback incomplete')
                    # Explicit report permission is unavailable, not a transport failure.
                    fetched = []
                from botend.services.wcl_combatant_snapshot import _validated_events
                fetched = _validated_events(fetched, [int(fight_id)]).get(int(fight_id), [])
                combatant_cache[cache_key] = fetched
            combatant = self._find_combatant_for_ranking(combatant_cache.get(cache_key) or [], ranking)
            talents = self.parse_wcl_talent_tree((combatant or {}).get('talentTree') or [])
            if talents:
                return talents

        return []

    @staticmethod
    def _find_combatant_for_ranking(combatants, ranking):
        """用 ranking.name 在 CombatantInfo.source.name 中匹配当前角色。"""
        target_name = (ranking.get('name') or '').strip().lower()
        if not target_name:
            return None
        for combatant in combatants:
            source = combatant.get('source') or {}
            source_name = (source.get('name') or combatant.get('name') or '').strip().lower()
            if source_name == target_name:
                return combatant
        return None

    def _collect_raid_rankings(self, season, difficulty, difficulty_name):
        """采集一个团本难度；各难度拥有完全隔离的同步范围。"""
        logger.info(f"[SpecDetailRanking] 采集团本 {difficulty_name} 排名: {len(season.raid_encounters)} Boss x {sum(len(v) for v in CLASS_SPEC_MAP.values())} 专精")

        combatant_cache = {}
        total = 0
        now = timezone.now()
        ok = True

        # Build encounter→zone mapping from raid_zones (if available)
        enc_zone_map = {}
        if season.raid_zones:
            for rz in season.raid_zones:
                for enc in rz.get('encounters', []):
                    enc_zone_map[enc['id']] = {
                        'zone_id': rz.get('id'),
                        'zone_name': rz.get('name', ''),
                    }

        for idx, encounter in enumerate(season.raid_encounters):
            enc_id = encounter['id']
            scope = f'raid:{difficulty}:{enc_id}'
            if self._scope_complete(scope):
                continue
            enc_name = encounter['name']
            zone_info = enc_zone_map.get(enc_id, {})

            # Phase 1: 先收集该 boss 所有数据（无事务）
            records = []
            encounter_failed = False
            for class_name, specs in CLASS_SPEC_MAP.items():
                if encounter_failed:
                    break
                for spec_name in specs:
                    rankings = self._fetch_rankings_with_retry(
                        enc_id, class_name, spec_name, 'dps', difficulty=difficulty
                    )
                    if rankings is None:
                        encounter_failed = True
                        break
                    if (not isinstance(rankings, dict)
                            or not isinstance(rankings.get('rankings'), list)
                            or not isinstance(rankings.get('hasMorePages'), bool)
                            or (not rankings['rankings'] and rankings.get('hasMorePages') is not False)):
                        encounter_failed = True
                        break

                    rank_list = rankings.get('rankings', [])
                    for r in rank_list:
                        try:
                            server = r.get('server', {}) or {}
                            report = r.get('report', {}) or {}
                            guild = r.get('guild', {}) or {}

                            talents_payload = self._parse_rank_talents(r, combatant_cache,
                                class_name=class_name, spec_name=spec_name)
                            records.append(SpecRaidRanking(
                                season_id=season.id,
                                boss_id=enc_id,
                                boss_name=enc_name,
                                difficulty=difficulty,
                                raid_zone_id=zone_info.get('zone_id'),
                                raid_zone_name=zone_info.get('zone_name', ''),
                                class_name=class_name,
                                spec_name=spec_name,
                                character_name=r.get('name', ''),
                                realm=server.get('name', ''),
                                region=server.get('region', ''),
                                dps=r.get('amount', 0),
                                kill_time=r.get('duration'),
                                talents_json=talents_payload,
                                # WCL ranking payload only provides structured selected nodes,
                                # not Blizzard's frozen import string.  Do not synthesize a
                                # share code from an unrelated profile reference: it can omit
                                # selections and imports as a different build.
                                talent_build_code='',
                                gear_json=self.parse_wcl_gear(r.get('gear', [])),
                                faction=r.get('faction'),
                                guild_name=guild.get('name', ''),
                                report_code=report.get('code', ''),
                                fight_id=report.get('fightID') or r.get('fightID'),
                                last_updated=now,
                            ))
                        except Exception as e:
                            encounter_failed = True
                            logger.warning(f"[SpecDetailRanking] 构建记录失败: {enc_id}/{class_name}/{spec_name}: {e}")
                            break

                    if encounter_failed:
                        break  # Preserve fallback retry hints; do not request the next spec.
                    time.sleep(0.3)  # 限速

            # Phase 2: 独立事务写入该 boss 的数据
            if encounter_failed:
                ok = False
                logger.error(f"[SpecDetailRanking] Boss {enc_id} ({enc_name}) 采集失败，保留旧数据不覆盖")
                return False
            elif records:
                try:
                    with self._scope_transaction():
                        result = self._sync_ranking_records(
                            model=SpecRaidRanking,
                            filters={'season_id': season.id, 'boss_id': enc_id, 'difficulty': difficulty},
                            records=records,
                            key_fields=self.RAID_KEY_FIELDS,
                            update_fields=self.RAID_UPDATE_FIELDS,
                        )
                        self._checkpoint(scope)
                    total += len(records)
                    logger.info(
                        f"[SpecDetailRanking] Boss {enc_id} ({enc_name})/{difficulty_name}: "
                        f"新增 {result['created']} / 更新 {result['updated']} / "
                        f"删除 {result['deleted']} / 未变化 {result['unchanged']}"
                    )
                except CycleOwnershipLost:
                    raise
                except Exception as e:
                    ok = False
                    logger.error(f"[SpecDetailRanking] Boss {enc_id} ({enc_name}) 写入失败: {e}")
            else:
                self._checkpoint(scope)
                logger.info(f"[SpecDetailRanking] Boss {enc_id} ({enc_name}): 权威空榜，保旧并完成范围")

        logger.info(f"[SpecDetailRanking] 团本排名采集完成: {total} 条")
        return ok

    @staticmethod
    def _ranking_business_key(row, key_fields):
        key = tuple(getattr(row, field) for field in key_fields)
        if (
            (getattr(row, 'character_name', '') or '').strip().lower() == 'anonymous'
            and not (getattr(row, 'realm', '') or '').strip()
            and not (getattr(row, 'region', '') or '').strip()
        ):
            return (*key, getattr(row, 'dps', None))
        return key

    @classmethod
    def _sync_ranking_records(cls, model, filters, records, key_fields, update_fields):
        """按稳定业务键同步排名，只写入新增、变化和已消失的记录。"""
        existing_map = {}
        duplicate_existing_ids = []
        for row in model.objects.filter(**filters):
            key = cls._ranking_business_key(row, key_fields)
            if key in existing_map:
                duplicate_existing_ids.append(row.id)
                continue
            existing_map[key] = row
        incoming_map = {}
        for row in records:
            key = cls._ranking_business_key(row, key_fields)
            # WCL 排名分页不是稳定快照：同一条实名记录可能在采集期间跨页重复。
            # 保留首次出现（排名更靠前）的记录，不因此拒绝整个副本/Boss 快照。
            if key in incoming_map:
                continue
            incoming_map[key] = row

        to_create = []
        to_update = []
        unchanged = 0
        for key, incoming in incoming_map.items():
            existing = existing_map.get(key)
            if existing is None:
                to_create.append(incoming)
                continue
            if not any(
                getattr(existing, field) != getattr(incoming, field)
                for field in update_fields
            ):
                unchanged += 1
                continue
            for field in update_fields:
                setattr(existing, field, getattr(incoming, field))
            existing.last_updated = incoming.last_updated
            to_update.append(existing)

        stale_ids = duplicate_existing_ids + [
            row.id for key, row in existing_map.items()
            if key not in incoming_map
        ]
        if stale_ids:
            model.objects.filter(id__in=stale_ids).delete()
        if to_create:
            model.objects.bulk_create(to_create, batch_size=500)
        if to_update:
            model.objects.bulk_update(
                to_update,
                fields=[*update_fields, 'last_updated'],
                batch_size=500,
            )
        return {
            'created': len(to_create),
            'updated': len(to_update),
            'deleted': len(stale_ids),
            'unchanged': unchanged,
        }
