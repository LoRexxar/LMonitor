"""按首领及难度复用团本计算，人物与中央展示资料变化立即失效。"""
from botend.models import SpecRaidRanking
from botend.services import spec_stats_service as stats
from botend.services.dungeon_aggregation import _metadata_dependencies
from botend.services.spec_aggregation_cache import load_work, save_work
from botend.services.spec_stats_snapshot import content_digest

ALGORITHM_VERSION = 'raid-scope-v3-current-metadata'


def raid_fingerprints(scopes):
    """Check one publication with request-local common dependencies.

    Discard the context before returning: a later miss must recheck inputs.
    """
    context = {}
    return [raid_fingerprint(*scope, _context=context) for scope in scopes]


def raid_fingerprint(season_id, boss_id, boss_name, class_name, spec_name, difficulty, *, _context=None):
    """Validate exact current inputs without decoding cached result bodies."""
    rows = list(SpecRaidRanking.objects.filter(season_id=season_id, boss_id=boss_id,
        class_name=class_name, spec_name=spec_name, difficulty=difficulty).order_by('pk').values())
    keys = {tuple((row.get(field) or '').lower() for field in ('region', 'realm', 'character_name'))
            for row in rows}
    context = _context if _context is not None else {}
    profile_cache = context.setdefault('profiles', {})
    profiles = stats._selected_player_profiles(keys, season_id, class_name, spec_name,
        ('gear_json', 'stats_json', 'race'), profile_cache) if rows else {}
    dependencies = _metadata_dependencies([*rows, *profiles.values()], {}, class_name, spec_name,
                                          context=context) if rows else {}
    fingerprint = content_digest({'algorithm': ALGORITHM_VERSION, 'name': boss_name,
        'rows': [{k: v for k, v in row.items() if k != 'last_updated'} for row in rows],
        'profiles': sorted(profiles.items()), 'metadata': dependencies})
    return fingerprint


def raid_stats(season_id, boss_id, boss_name, class_name, spec_name, difficulty, *, fingerprint=None):
    """An already-checked fingerprint is only a hint for exact work reuse.

    A missing/corrupt work body always rechecks current inputs before computing;
    never attach a previously captured identity to newly read source facts.
    """
    scope = f'raid:{class_name}:{spec_name}:{difficulty}:{boss_id}'
    if fingerprint is not None:
        cached = load_work(season_id, scope, fingerprint)
        if cached is not None:
            return cached, fingerprint, True
    fingerprint = raid_fingerprint(season_id, boss_id, boss_name, class_name, spec_name, difficulty)
    cached = load_work(season_id, scope, fingerprint)
    if cached is not None:
        return cached, fingerprint, True
    result = stats.SpecStatsService._compute_raid_stats(
        season_id, boss_id, boss_name, class_name, spec_name, full=True, difficulty=difficulty)
    return save_work(season_id, scope, fingerprint, result), fingerprint, False
