"""辅助配装的获取范围；复用中央手册顺序与套装来源，不复制装备事实。"""
from collections import defaultdict

from botend.journal_models import JournalEncounter, JournalState
from botend.services.gear_builder_tier_sources import tier_set_sources


def mythic_raid_final_encounters():
    """Return each known raid's encounter IDs and final two Mythic encounters.

    Orders are instance-local. Raids without difficulty 16 are explicitly known
    non-Mythic raids, not additional 'last two' bosses in a season-wide ordering.
    Missing/ambiguous records remain unresolved instead of inventing boss IDs.
    """
    release = JournalState.objects.filter(pk='wow-zhCN').values('active_release_id')[:1]
    groups = defaultdict(list)
    for row in JournalEncounter.objects.filter(
        instance__release_id=release, instance__kind='raid',
    ).values('instance__journal_id', 'journal_id', 'order', 'payload'):
        groups[row['instance__journal_id']].append(row)
    result = {}
    for instance_id, rows in groups.items():
        if any(not isinstance(row['payload'].get('difficulty_ids'), list) or
               not row['payload']['difficulty_ids'] for row in rows):
            continue
        mythic = [row for row in rows if 16 in row['payload']['difficulty_ids']]
        if len({row['order'] for row in mythic}) != len(mythic):
            continue
        last = sorted(mythic, key=lambda row: row['order'])[-2:]
        result[instance_id] = ({row['journal_id'] for row in rows}, {row['journal_id'] for row in last})
    return result


def obtainable_without_mythic_last_two(variant, raids, *, sources=None):
    if str(variant.upgrade_track or '').casefold() != 'myth':
        return True
    if sources is None:
        metadata = {**(variant.item.metadata or {}), **(variant.metadata or {})}
        sources = tier_set_sources(metadata, variant.item.slot_key)
        if sources is None:
            sources = variant.source_json or []
    for source in sources:
        if not isinstance(source, dict):
            continue
        source_type = str(source.get('type') or '').casefold()
        if source_type in {'mythic_plus', 'delve', 'crafted', 'profession'}:
            return True
        if source_type != 'raid':
            continue
        try:
            instance_id = int(source.get('instance_id') or 0)
            encounter_id = int(source.get('encounter_id') or 0)
        except (TypeError, ValueError):
            continue
        known = raids.get(instance_id)
        # Raidbots names non-encounter loot explicitly; trash is not a boss.
        if known and encounter_id < 0 and source.get('encounter') == 'Trash Drop':
            return True
        if known and encounter_id in known[0] and encounter_id not in known[1]:
            return True
    # Unresolved provenance cannot certify acquisition. Exclude only this
    # candidate, not valid alternatives or equipment already owned/locked.
    return False
