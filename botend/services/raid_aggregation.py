"""按首领和难度复用 WCL 日志统计；个人资料变化不再使缓存失效。"""
from botend.models import SpecRaidRanking
from botend.services import spec_stats_service as stats
from botend.services import wcl_combatant_snapshot as combatants
from botend.services.dungeon_aggregation import _metadata_dependencies
from botend.services.spec_aggregation_cache import load_work, save_work
from botend.services.spec_stats_snapshot import content_digest

ALGORITHM_VERSION = 'raid-scope-v5-central-talents'


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
    return rows, combatants._load_facts(rows, fetched_before=source_cutoff)


def _fingerprint(rows, facts, boss_name, class_name, spec_name, context=None):
    dependencies = _metadata_dependencies(rows, facts, class_name, spec_name,
                                          context=context) if rows else {}
    return content_digest({'algorithm': ALGORITHM_VERSION, 'name': boss_name,
        'rows': [{k: v for k, v in row.items() if k != 'last_updated'} for row in rows],
        'facts': [(list(key), value) for key, value in sorted(facts.items())],
        'metadata': dependencies})


def raid_fingerprints(scopes, *, selected_ids=None, source_cutoff=None):
    """同一次发布共享中央展示元数据查询，不读取人物资料。"""
    context = {}
    return [raid_fingerprint(*scope, _context=context,
        selected_ids=selected_ids[f'{scope[-1]}:{scope[1]}'] if selected_ids is not None else None,
        source_cutoff=source_cutoff) for scope in scopes]


def raid_fingerprint(season_id, boss_id, boss_name, class_name, spec_name, difficulty,
                     *, _context=None, selected_ids=None, source_cutoff=None):
    rows, facts = _inputs(season_id, boss_id, class_name, spec_name, difficulty, selected_ids, source_cutoff)
    return _fingerprint(rows, facts, boss_name, class_name, spec_name, _context)


def raid_stats(season_id, boss_id, boss_name, class_name, spec_name, difficulty,
               *, fingerprint=None, selected_ids=None, source_cutoff=None):
    """工作结果丢失时重查输入，防止旧指纹标记新数据。"""
    scope = f'raid:{class_name}:{spec_name}:{difficulty}:{boss_id}'
    if fingerprint is not None:
        cached = load_work(season_id, scope, fingerprint)
        if cached is not None:
            return cached, fingerprint, True
    rows, facts = _inputs(season_id, boss_id, class_name, spec_name, difficulty, selected_ids, source_cutoff)
    fingerprint = _fingerprint(rows, facts, boss_name, class_name, spec_name)
    cached = load_work(season_id, scope, fingerprint)
    if cached is not None:
        return cached, fingerprint, True
    enriched = combatants.enrich_dungeon_records(rows, class_name, spec_name, facts=facts)
    result = stats.SpecStatsService._compute_raid_records(
        rows, season_id, boss_id, boss_name, class_name, spec_name, full=True, enriched_records=enriched)
    return save_work(season_id, scope, fingerprint, result), fingerprint, False
