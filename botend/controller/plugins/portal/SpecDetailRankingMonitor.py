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
from botend.services.wcl_storage_guard import WCLStorageError, check_wcl_storage
from botend.services.wcl_ranking_payload import compact_ranking_talents
from botend.services.wcl_ranking_time import parse_ranking_fight_started_at
from botend.services.wcl_collection_cycle import (
    CycleOwnershipLost, begin_collection, checkpoint_scope, defer_cycle,
    fenced_cycle, finish_collection, freeze_samples,
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
        'fight_started_at',
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
            try:
                check_wcl_storage()
            except WCLStorageError as exc:
                logger.warning('%s', exc)
                return False
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
            check_wcl_storage()
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
            from botend.services.wcl_collection_resume import collect_cycle_combatants
            counts = collect_cycle_combatants(
                self._cycle, self, log=logger.info,
                admission_check=check_wcl_storage)
            ok = finish_collection(self._cycle, counts)
            if ok and self.task is not None:
                self.task.flag = f"{season.season_key}@rankings@{self._cycle.pk}"
                self.task.save(update_fields=['flag'])
            return ok
        except (CycleOwnershipLost, MonitorTaskLeaseLost):
            logger.warning('[SpecDetailRanking] cycle ownership lost; no stale completion')
            return False
        except WCLStorageError as exc:
            try:
                defer_cycle(self._cycle, str(exc))
            except CycleOwnershipLost:
                pass
            logger.warning('[SpecDetailRanking] capacity deferred: %s', exc)
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
        check_wcl_storage()
        cycle = getattr(self, '_cycle', None)
        return fenced_cycle(cycle, phases=('rankings',)) if cycle else transaction.atomic()

    def _fetch_rankings_with_retry(self, enc_id, class_name, spec_name, metric='dps', difficulty=None, page=1, max_retries=3):
        """带加强重试的排名获取，处理限流和临时错误"""
        check_wcl_storage()
        from botend.services.spec_aggregation_cache import load_work, save_work
        from botend.services.spec_stats_snapshot import content_digest
        cycle = getattr(self, '_cycle', None)
        scope = f'ranking-page:{enc_id}:{class_name}:{spec_name}:{metric}:{difficulty}:{page}'
        fingerprint = content_digest(['ranking-page-v2-headers', cycle.pk]) if cycle else None
        if cycle:
            with fenced_cycle(cycle, phases=('rankings',)):
                cached = load_work(cycle.season_id, scope, fingerprint)
            if cached is not None and self._valid_ranking_page(cached):
                return cached
        for attempt in range(max_retries):
            check_wcl_storage()
            rankings = self.fetch_wcl_rankings(
                enc_id, class_name, spec_name, metric, difficulty=difficulty,
                page=page, include_combatant_info=False)
            if rankings is not None:
                if self._valid_ranking_page(rankings):
                    rankings = self._ranking_headers(rankings)
                if cycle and self._valid_ranking_page(rankings):
                    with fenced_cycle(cycle, phases=('rankings',)):
                        check_wcl_storage()
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
            try:
                parse_ranking_fight_started_at(row)
            except ValueError:
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

    @staticmethod
    def _ranking_headers(payload):
        """页面检查点仅保存选样和统计标量，详情统一从中央日志事实读取。"""
        fields = ('name', 'server', 'report', 'fightID', 'amount', 'hardModeLevel',
                  'duration', 'score', 'medal', 'affixes', 'faction', 'guild', 'startTime')
        return {'hasMorePages': payload['hasMorePages'],
                'rankings': [{key: row[key] for key in fields if key in row}
                             for row in payload['rankings']]}

    def _select_ranking_headers(self, enc_id, class_name, spec_name, *, difficulty=None):
        """按来源顺序跳过无日志、身份缺失和重复人物，满100条立即停止。"""
        from botend.services.wcl_combatant_snapshot import _fight_key, _realm
        from botend.services.spec_stats_snapshot import content_digest
        selected, seen_players, seen_pages = [], set(), set()
        page = 1
        while len(selected) < 100:
            payload = self._fetch_rankings_with_retry(
                enc_id, class_name, spec_name, 'dps', difficulty=difficulty, page=page)
            if not self._valid_ranking_page(payload):
                return None
            page_digest = content_digest(self._ranking_headers(payload)['rankings'])
            if page_digest in seen_pages:
                cycle = getattr(self, '_cycle', None)
                if cycle:
                    from botend.services.spec_aggregation_cache import save_work
                    scope = f'ranking-page:{enc_id}:{class_name}:{spec_name}:dps:{difficulty}:{page}'
                    # 覆盖异常页检查点，下一轮重取该页，保留此前有效页。
                    with fenced_cycle(cycle, phases=('rankings',)):
                        check_wcl_storage()
                        save_work(cycle.season_id, scope,
                                  content_digest(['ranking-page-v2-headers', cycle.pk]), None)
                logger.error('[SpecDetailRanking] 榜单重复返回同页，停止并保留旧数据: %s/%s/%s',
                             enc_id, class_name, spec_name)
                return None
            seen_pages.add(page_digest)
            for row in payload['rankings']:
                server, report = row.get('server') or {}, row.get('report') or {}
                player = (str(server.get('region') or '').strip().casefold(),
                          _realm(server.get('name')), str(row.get('name') or '').strip().casefold())
                if (not all(player) or player[2] == 'anonymous' or player in seen_players
                        or not _fight_key({'report_code': report.get('code'),
                            'fight_id': report.get('fightID') or row.get('fightID')})):
                    continue
                seen_players.add(player)
                selected.append(row)
                if len(selected) == 100:
                    break
            if len(selected) == 100 or not payload['hasMorePages']:
                break
            page += 1
            time.sleep(0.3)
        logger.info('[SpecDetailRanking] 轻量选样 %s/%s/%s/%s: 页数=%s 入选=%s',
                    enc_id, class_name, spec_name, difficulty, page, len(selected))
        return selected

    @staticmethod
    def _header_values(row):
        server, report, guild = row.get('server') or {}, row.get('report') or {}, row.get('guild') or {}
        return dict(character_name=row.get('name', ''), realm=server.get('name', ''),
                    region=server.get('region', ''), dps=row['amount'], faction=row.get('faction'),
                    guild_name=guild.get('name', ''), report_code=report.get('code', ''),
                    fight_id=report.get('fightID') or row.get('fightID'))

    def _collect_dungeon_rankings(self, season):
        """先采集各专精的100条轻量日志引用，范围失败时保留旧数据。"""
        for encounter in season.mplus_encounters:
            enc_id, enc_name = encounter['id'], encounter['name']
            scope = f'dungeon:{enc_id}'
            if self._scope_complete(scope):
                continue
            records = []
            for class_name, specs in CLASS_SPEC_MAP.items():
                for spec_name in specs:
                    rows = self._select_ranking_headers(enc_id, class_name, spec_name)
                    if rows is None:
                        logger.error('[SpecDetailRanking] M+ %s 轻量榜单不完整，保留旧数据', enc_id)
                        return False
                    records.extend(SpecDungeonRanking(
                        season_id=season.id, dungeon_id=enc_id, dungeon_name=enc_name,
                        class_name=class_name, spec_name=spec_name,
                        keystone_level=row.get('hardModeLevel'), clear_time=row.get('duration'),
                        score=row.get('score'), medal=row.get('medal', ''), affixes=row.get('affixes', []),
                        fight_started_at=parse_ranking_fight_started_at(row), last_updated=timezone.now(),
                        **self._header_values(row)) for row in rows)
                    time.sleep(0.3)
            if not records:
                self._checkpoint(scope)
                continue
            with self._scope_transaction():
                result = self._sync_ranking_records(
                    model=SpecDungeonRanking, filters={'season_id': season.id, 'dungeon_id': enc_id},
                    records=records, key_fields=self.DUNGEON_KEY_FIELDS,
                    update_fields=self._header_update_fields(self.DUNGEON_UPDATE_FIELDS))
                self._checkpoint(scope)
            logger.info('[SpecDetailRanking] M+ %s 轻量榜单同步: %s', enc_id, result)
        return True

    @staticmethod
    def _header_update_fields(fields):
        # 相同日志已有的装备、天赋继续保留；新日志默认留空，由中央事实提供详情。
        return tuple(name for name in fields if name not in
                     ('talents_json', 'talent_build_code', 'gear_json'))

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
            counts = collect_dungeon_combatants(
                [row], self, apply=True, admission_check=check_wcl_storage)
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
        """团本同样按页补足100条有效引用，各首领和难度独立同步。"""
        zones = {enc['id']: zone for zone in season.raid_zones or []
                 for enc in zone.get('encounters', [])}
        for encounter in season.raid_encounters:
            enc_id, enc_name = encounter['id'], encounter['name']
            scope = f'raid:{difficulty}:{enc_id}'
            if self._scope_complete(scope):
                continue
            zone = zones.get(enc_id, {})
            records = []
            for class_name, specs in CLASS_SPEC_MAP.items():
                for spec_name in specs:
                    rows = self._select_ranking_headers(enc_id, class_name, spec_name, difficulty=difficulty)
                    if rows is None:
                        logger.error('[SpecDetailRanking] 首领 %s/%s 轻量榜单不完整，保留旧数据',
                                     enc_id, difficulty_name)
                        return False
                    records.extend(SpecRaidRanking(
                        season_id=season.id, boss_id=enc_id, boss_name=enc_name, difficulty=difficulty,
                        raid_zone_id=zone.get('id'), raid_zone_name=zone.get('name', ''),
                        class_name=class_name, spec_name=spec_name, kill_time=row.get('duration'),
                        last_updated=timezone.now(), **self._header_values(row)) for row in rows)
                    time.sleep(0.3)
            if not records:
                self._checkpoint(scope)
                continue
            with self._scope_transaction():
                result = self._sync_ranking_records(
                    model=SpecRaidRanking,
                    filters={'season_id': season.id, 'boss_id': enc_id, 'difficulty': difficulty},
                    records=records, key_fields=self.RAID_KEY_FIELDS,
                    update_fields=self._header_update_fields(self.RAID_UPDATE_FIELDS))
                self._checkpoint(scope)
            logger.info('[SpecDetailRanking] 首领 %s/%s 轻量榜单同步: %s', enc_id, difficulty_name, result)
        return True

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
        from itertools import islice

        batch_size = 500
        # Freeze lightweight identities in ONE query, preserving the queryset's
        # ordering. Unordered LIMIT/OFFSET pages can overlap or omit identities.
        # MySQL may buffer this projection, but it contains no large JSON fields.
        existing_query = model.objects.filter(**filters)
        identities = existing_query.only('pk', *key_fields, 'dps')
        existing_map = {}
        duplicate_existing_ids = []
        for row in identities.iterator(chunk_size=batch_size):
            key = cls._ranking_business_key(row, key_fields)
            if key in existing_map:
                duplicate_existing_ids.append(row.pk)
            else:
                existing_map[key] = row.pk
        incoming_map = {}
        for row in records:
            key = cls._ranking_business_key(row, key_fields)
            # WCL 排名分页不是稳定快照：同一条实名记录可能在采集期间跨页重复。
            # 保留首次出现（排名更靠前）的记录，不因此拒绝整个副本/Boss 快照。
            if key in incoming_map:
                continue
            incoming_map[key] = row

        from django.db.models import FloatField, IntegerField
        from copy import copy

        # Only normalize numeric update values at the model's persistence boundary.
        # Business keys (including anonymous DPS), JSON and input objects stay intact.
        numeric_fields = {
            name: model._meta.get_field(name) for name in update_fields
            if name not in key_fields
            and isinstance(model._meta.get_field(name), (IntegerField, FloatField))
        }
        to_create = []
        update_groups = {}
        unchanged = 0
        incoming_items = iter(incoming_map.items())
        while batch := list(islice(incoming_items, batch_size)):
            ids = [existing_map[key] for key, _ in batch if key in existing_map]
            existing_batch = {row.pk: row for row in existing_query.filter(pk__in=ids).only('pk', *update_fields)[:batch_size]}
            for key, incoming in batch:
                values = {name: getattr(incoming, name) for name in update_fields}
                if 'talents_json' in values:
                    values['talents_json'] = compact_ranking_talents(values['talents_json'])
                for name, field in numeric_fields.items():
                    raw = values[name]
                    if isinstance(raw, bool):
                        raise ValueError(f'Invalid numeric ranking field: {name}')
                    value = field.to_python(raw)
                    # Reject non-finite or lossy values, rather than silently repairing
                    # invalid facts. All conversions finish before any snapshot DML.
                    if value is not None and (
                        isinstance(field, FloatField) and not math.isfinite(value)
                        or isinstance(field, IntegerField) and not isinstance(raw, str) and raw != value
                    ):
                        raise ValueError(f'Invalid numeric ranking field: {name}')
                    values[name] = value
                existing = existing_batch.get(existing_map.get(key))
                if (existing is not None and 'fight_started_at' in values
                        and values['fight_started_at'] is None):
                    # The business key includes the exact report/fight. A later
                    # missing field cannot erase that same log's known date.
                    values['fight_started_at'] = existing.fight_started_at
                if existing is None:
                    created = copy(incoming)
                    for name, value in values.items():
                        setattr(created, name, value)
                    to_create.append(created)
                    continue
                changed_fields = tuple(
                    name for name in update_fields
                    if (compact_ranking_talents(getattr(existing, name))
                        if name == 'talents_json' else getattr(existing, name)) != values[name]
                )
                if not changed_fields:
                    unchanged += 1
                    continue
                # Never retain the loaded row (and its unchanged large JSON) in
                # the global write plan. Only changed values belong to an update.
                changed = model(pk=existing.pk, last_updated=incoming.last_updated,
                                **{name: values[name] for name in changed_fields})
                update_groups.setdefault(changed_fields, []).append(changed)
            del existing, existing_batch

        stale_ids = duplicate_existing_ids + [
            row_id for key, row_id in existing_map.items()
            if key not in incoming_map
        ]
        if stale_ids or to_create or update_groups:
            check_wcl_storage()
        if stale_ids:
            model.objects.filter(id__in=stale_ids).delete()
        if to_create:
            model.objects.bulk_create(to_create, batch_size=500)
        for changed_fields, rows in update_groups.items():
            # Django prepares expressions for every object passed to bulk_update
            # before executing its internal batches. Bound the call itself.
            for offset in range(0, len(rows), batch_size):
                model.objects.bulk_update(
                    rows[offset:offset + batch_size],
                    fields=[*changed_fields, 'last_updated'],
                    batch_size=batch_size,
                )
        return {
            'created': len(to_create),
            'updated': sum(len(rows) for rows in update_groups.values()),
            'deleted': len(stale_ids),
            'unchanged': unchanged,
        }
