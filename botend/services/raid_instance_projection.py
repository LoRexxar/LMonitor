"""Display physical raids without changing WCL zones, samples or saved snapshots."""
from collections import defaultdict

from botend.journal_models import JournalEncounter


def project_raid_instances(payload):
    """Join only published bosses to the current central journal in one narrow query.

    WCL may combine several raids under one zone. Journal DungeonEncounterID is
    the encounter join key; JournalInstance.ID is NOT a WCL zone ID. Missing or
    ambiguous mappings retain their original WCL grouping, never lose a boss.
    """
    all_groups = list(payload.get('zone_groups') or [])
    for option in payload.get('difficulties') or []:
        all_groups.extend(option.get('zone_groups') or [])
    boss_ids = {boss['boss_id'] for group in all_groups for boss in group.get('bosses', [])
                if isinstance(boss, dict) and type(boss.get('boss_id')) is int and boss['boss_id'] > 0}
    if not boss_ids:
        return payload
    matches = defaultdict(list)
    rows = JournalEncounter.objects.filter(
        instance__release__journalstate__key='wow-zhCN',
        instance__kind='raid', payload__dungeon_encounter_id__in=boss_ids,
    ).order_by().values_list('payload__dungeon_encounter_id', 'instance__journal_id', 'instance__name')
    for boss_id, instance_id, name in rows:
        matches[boss_id].append((instance_id, name))
    known = {bid: values[0] for bid, values in matches.items() if len(values) == 1}
    if not known:
        return payload

    def regroup(groups):
        result = []
        for group in groups:
            partitions = {}
            for boss in group.get('bosses', []):
                identity = known.get(boss.get('boss_id')) if isinstance(boss, dict) else None
                if identity not in partitions:
                    row = {key: value for key, value in group.items()
                           if key not in {'bosses', 'instance_id', 'instance_name'}}
                    row['bosses'] = []
                    if identity:
                        row.update(instance_id=identity[0], instance_name=identity[1])
                    partitions[identity] = row
                if identity:
                    boss = dict(boss, raid_instance_id=identity[0], raid_instance_name=identity[1])
                partitions[identity]['bosses'].append(boss)
            result.extend(partitions.values() if partitions else [group])
        return result

    return dict(payload,
                zone_groups=regroup(payload.get('zone_groups') or []),
                difficulties=[dict(option, zone_groups=regroup(option.get('zone_groups') or []))
                              for option in payload.get('difficulties') or []])
