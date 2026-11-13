"""按首领和难度复用 WCL 日志统计；个人资料变化不再使缓存失效。"""
import sys
import time

from botend.models import SpecRaidRanking
from botend.services import spec_stats_service as stats
from botend.services import wcl_combatant_snapshot as combatants
from botend.services.dungeon_aggregation import _metadata_dependencies
from botend.services.spec_aggregation_cache import load_work, save_work, work_fingerprint_hint

ALGORITHM_VERSION = 'raid-scope-v6-sample-facts'


class RaidInputBuffer:
    """单次发布内复用已读取输入，限额外的范围沿用重新读取，不写任何存储。"""
    def __init__(self, max_bytes=16 * 1024**2):
        self.max_bytes = max_bytes
        self.used_bytes = 0
        self._entries = {}

    @staticmethod
    def _key(scope, selected_ids, source_cutoff):
        return tuple(scope), None if selected_ids is None else tuple(selected_ids), source_cutoff

    def remember(self, scope, selected_ids, source_cutoff, rows, facts, fingerprint):
        value = (rows, facts, fingerprint)
        key = self._key(scope, selected_ids, source_cutoff)
        if key in self._entries:
            return
        # 统计对象树的内存而非重新序列化大 JSON；共享对象只计一次，超额立即停止。
        size, seen, pending = 0, set(), [key, value]
        while pending:
            item = pending.pop()
            if id(item) in seen:
                continue
            seen.add(id(item))
            size += sys.getsizeof(item)
            if self.used_bytes + size > self.max_bytes:
                return
            if isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, (list, tuple)):
                pending.extend(item)
        self._entries[key] = (value, size)
        self.used_bytes += size

    def take(self, scope, selected_ids, source_cutoff):
        entry = self._entries.pop(self._key(scope, selected_ids, source_cutoff), None)
        if entry is None:
            return None
        value, size = entry
        self.used_bytes -= size
        return value


def select_raid_records(season_id, boss_id, class_name, spec_name, difficulty, *, selected_ids=None, hydrate_fields=None):
    """每个首领/难度/专精最多100条有日志的记录；冻结后不换样本。"""
    qs = SpecRaidRanking.objects.filter(season_id=season_id, boss_id=boss_id,
        class_name=class_name, spec_name=spec_name, difficulty=difficulty)
    if selected_ids is None:
        return list(qs.exclude(report_code='').filter(fight_id__gt=0).order_by('-dps', 'pk').values(*(hydrate_fields or ()))[:100])
    if (not isinstance(selected_ids, list) or len(selected_ids) > 100
            or any(type(pk) is not int or pk <= 0 for pk in selected_ids)
            or len(set(selected_ids)) != len(selected_ids)):
        raise ValueError('团本冻结样本引用无效')
    rows = {row['id']: row for row in qs.filter(pk__in=selected_ids).values()}
    if len(rows) != len(selected_ids):
        raise ValueError('团本冻结样本引用已丢失')
    return [rows[pk] for pk in selected_ids if combatants._fight_key(rows[pk])]


def _inputs(season_id, boss_id, class_name, spec_name, difficulty, selected_ids, source_cutoff):
    rows = select_raid_records(season_id, boss_id, class_name, spec_name, difficulty,
                               selected_ids=selected_ids)
    return rows, combatants.load_matched_facts(rows, class_name, spec_name, fetched_before=source_cutoff)


def _fingerprint(rows, facts, boss_name, class_name, spec_name, context=None):
    from .wcl_log_analysis_cache import scope_fingerprint
    dependencies = _metadata_dependencies(rows, facts, class_name, spec_name,
                                          context=context) if rows else {}
    return scope_fingerprint(rows, facts, class_name, spec_name,
        scope=[ALGORITHM_VERSION, boss_name], metadata=dependencies)


def raid_fingerprints(scopes, *, selected_ids=None, source_cutoff=None, prepared_inputs=None,
                      phase_timings=None):
    """同一次发布共享中央展示元数据查询，不读取人物资料。"""
    context = {}
    return [raid_fingerprint(*scope, _context=context,
        selected_ids=selected_ids[f'{scope[-1]}:{scope[1]}'] if selected_ids is not None else None,
        source_cutoff=source_cutoff, _prepared_inputs=prepared_inputs,
        phase_timings=phase_timings) for scope in scopes]


def raid_fingerprint(season_id, boss_id, boss_name, class_name, spec_name, difficulty,
                     *, _context=None, selected_ids=None, source_cutoff=None, _prepared_inputs=None,
                     phase_timings=None):
    started = time.monotonic()
    rows, facts = _inputs(season_id, boss_id, class_name, spec_name, difficulty, selected_ids, source_cutoff)
    if phase_timings is not None:
        phase_timings['raid_input'] += time.monotonic() - started
    started = time.monotonic()
    fingerprint = _fingerprint(rows, facts, boss_name, class_name, spec_name, _context)
    if phase_timings is not None:
        phase_timings['raid_fingerprint'] += time.monotonic() - started
    if _prepared_inputs is not None:
        scope = f'raid:{class_name}:{spec_name}:{difficulty}:{boss_id}'
        # Expected hits need no inputs. Reserve the same bounded buffer for
        # likely misses, including unknown headers. Hints are NOT hit evidence:
        # raid_stats always validates the cache, or safely rereads on overflow.
        if work_fingerprint_hint(season_id, scope) != fingerprint:
            _prepared_inputs.remember((season_id, boss_id, boss_name, class_name, spec_name, difficulty),
                                      selected_ids, source_cutoff, rows, facts, fingerprint)
    return fingerprint


def raid_stats(season_id, boss_id, boss_name, class_name, spec_name, difficulty,
               *, fingerprint=None, selected_ids=None, source_cutoff=None, prepared_inputs=None,
               phase_timings=None):
    """优先消费本轮指纹对应的同一份输入；只有旧指纹时仍重查，避免错标数据。"""
    scope = f'raid:{class_name}:{spec_name}:{difficulty}:{boss_id}'
    prepared = (prepared_inputs.take((season_id, boss_id, boss_name, class_name, spec_name, difficulty),
                                     selected_ids, source_cutoff) if prepared_inputs is not None else None)
    checked_fingerprint = fingerprint
    started = time.monotonic()
    if fingerprint is not None:
        cached = load_work(season_id, scope, fingerprint)
        if phase_timings is not None:
            phase_timings['raid_work_read'] += time.monotonic() - started
        if cached is not None:
            return cached, fingerprint, True
    if prepared is not None and prepared[2] == fingerprint:
        rows, facts, fingerprint = prepared
    else:
        started = time.monotonic()
        rows, facts = _inputs(season_id, boss_id, class_name, spec_name, difficulty, selected_ids, source_cutoff)
        if phase_timings is not None:
            phase_timings['raid_input'] += time.monotonic() - started
        started = time.monotonic()
        fingerprint = _fingerprint(rows, facts, boss_name, class_name, spec_name)
        if phase_timings is not None:
            phase_timings['raid_fingerprint'] += time.monotonic() - started
    if fingerprint != checked_fingerprint:
        started = time.monotonic()
        cached = load_work(season_id, scope, fingerprint)
        if phase_timings is not None:
            phase_timings['raid_work_read'] += time.monotonic() - started
        if cached is not None:
            return cached, fingerprint, True
    from .wcl_log_analysis_cache import prepare_cached_logs, reusable_log_statistics
    log_statistics = reusable_log_statistics(season_id, scope, fingerprint)
    enriched, prepared_logs, provider = prepare_cached_logs(rows, class_name, spec_name,
        season_id=season_id, scope=scope, facts=facts, timings=phase_timings)
    result = stats.SpecStatsService._compute_raid_records(
        rows, season_id, boss_id, boss_name, class_name, spec_name, full=True,
        enriched_records=enriched, phase_timings=phase_timings,
        prepared_log_records=prepared_logs, talent_provider=provider,
        cached_log_statistics=log_statistics)
    started = time.monotonic()
    saved = save_work(season_id, scope, fingerprint, result)
    if phase_timings is not None:
        phase_timings['raid_work_write'] += time.monotonic() - started
    return saved, fingerprint, False
