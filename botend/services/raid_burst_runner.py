"""Independent manual burst publication; never owns or advances the WCL cycle."""
from contextlib import contextmanager
from functools import partial

from botend.models import MonitorTask, SeasonMeta, WclCollectionCycle
from botend.services.wcl_collection_cycle import CycleOwnershipLost, frozen_season, task_lease_guard

TASK_NAME = 'RaidBurstAggregationMonitor'
SOURCE_PHASES = ('ready', 'aggregating', 'published')


@contextmanager
def burst_source_guard(task, cycle):
    # Same lock order as collection: task -> lease -> season -> cycle.
    if (getattr(task, 'name', None) != TASK_NAME or
            not getattr(task, '_monitor_task_lease_owner', None)):
        raise CycleOwnershipLost('dedicated burst lease required')
    with task_lease_guard(task):
        if not MonitorTask.objects.filter(pk=task.pk, name=TASK_NAME).exists():
            raise CycleOwnershipLost('dedicated burst task changed')
        SeasonMeta.objects.select_for_update().get(pk=cycle.season_id)
        latest = WclCollectionCycle.objects.select_for_update().filter(
            season_id=cycle.season_id).only('id', 'season_id', 'slot', 'phase',
                                            'collected_at', 'completed_scopes').order_by('-slot', '-pk').first()
        collecting = WclCollectionCycle.objects.filter(season_id=cycle.season_id,
                                                       phase__in=('rankings', 'combatants')).exists()
        if (latest is None or latest.pk != cycle.pk or collecting or
                latest.phase not in SOURCE_PHASES or not latest.collected_at or
                latest.collected_at != cycle.collected_at or
                latest.completed_scopes != cycle.completed_scopes):
            raise CycleOwnershipLost('WCL source changed or collection in progress; retry after collection')
        # Per-scope fact timestamps are checked when materializing ranked rows.
        # Do not scan every spec's rankings on each event page/checkpoint.
        yield latest


def run_raid_burst(task, *, season_id=None, cycle_id=None, class_name=None, spec_name=None):
    from botend.services.raid_burst_aggregation import SUPPORTED, aggregate_raid_burst
    season = (SeasonMeta.objects.filter(pk=season_id).first() if season_id else
              SeasonMeta.objects.filter(is_active=True).first())
    if season is None:
        raise ValueError('season not found')
    cycles = WclCollectionCycle.objects.filter(season=season)
    cycle = (cycles.filter(pk=cycle_id).first() if cycle_id else
             cycles.order_by('-slot', '-pk').first())
    if cycle is None:
        raise ValueError('completed WCL source cycle required')
    identities = [(cls, spec) for cls, spec in SUPPORTED
                  if (not class_name or cls == class_name) and (not spec_name or spec == spec_name)]
    if not identities:
        raise ValueError('no supported burst specialization matches filters')
    guard = partial(burst_source_guard, task, cycle)
    with guard():
        frozen = frozen_season(cycle, season)
        expected = {f'raid:{difficulty}:{enc["id"]}'
                    for enc in frozen.raid_encounters for difficulty in (5, 4)}
        if not expected or not expected.issubset(set(cycle.completed_scopes)):
            raise ValueError('complete frozen heroic/mythic raid scopes required')
    completed = []
    for cls, spec in identities:
        with guard():
            pass
        result = aggregate_raid_burst(frozen, cls, spec, guard=guard,
                                      source_cutoff=cycle.collected_at)
        if result is None:
            raise RuntimeError('burst aggregation did not produce a terminal result')
        completed.append(f'{cls}/{spec}')
    with guard():
        pass
    return {'status': 'completed', 'cycle_id': cycle.pk,
            'source_cutoff': cycle.collected_at.isoformat(), 'specs': completed}
