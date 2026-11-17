"""前台数据模拟管理的方案聚合；按页读取，历史不重复堆叠到首页。"""

import math

from django.core.paginator import Paginator
from django.db.models import Count, Exists, Max, OuterRef, Q

from botend.models import SimcApl, SimcProfile, SimcTalentString, SimcReferenceSimulation, SimcTaskArtifact
from botend.services.simc_reference_simulations import (
    ACTIVE, active_scenarios, normalized_snapshot, public_combination_keys, public_spec,
    scenario_for_snapshot, scenario_simulation_params, snapshot,
)
from botend.services.simc_task_service import _compute_content_hash


def with_report(queryset):
    return queryset.annotate(has_report=Exists(SimcTaskArtifact.objects.filter(
        task_id=OuterRef('task_id'), run__task_id=OuterRef('task_id'),
        run__status='completed', artifact_type='html_report',
    )))


def _latest_successes(base, wanted, signatures):
    if not signatures:
        return {}
    completed = base.filter(wanted, status='completed', dps__gt=0).order_by()
    # 现行输入使用签名索引。仅在本页组合中兼容含版本号的旧后端输入。
    candidates = []
    for queryset in (completed.filter(signature__in=signatures),
                     completed.exclude(signature__in=signatures).filter(snapshot__backend__1__isnull=False)):
        candidates.extend(queryset.values('signature').annotate(latest_id=Max('pk')).values_list('latest_id', flat=True))
    latest = {}
    for row in base.only('id', 'snapshot').in_bulk(candidates).values():
        if not isinstance(row.snapshot, dict):
            continue
        signature = _compute_content_hash(normalized_snapshot(row.snapshot))
        if signature in signatures:
            latest[signature] = max(row.pk, latest.get(signature, 0))
    results = with_report(completed).defer('snapshot', 'error').in_bulk(latest.values())
    return {signature: results[pk] for signature, pk in latest.items()
            if pk in results and math.isfinite(results[pk].dps)}


def scheme_results(config, page_number, *, spec=None, scenarios=None):
    from botend.services.simc_reference_details import resource_links
    from botend.services.simc_reference_rankings import current_rankings
    base = SimcReferenceSimulation.objects.filter(config=config)
    historical = base.values('profile_id', 'talent_id', 'apl_id').annotate(
        record_count=Count('pk'), latest_id=Max('pk'),
    ).order_by('-latest_id')
    key_for = lambda row: (row['profile_id'], row['talent_id'], row['apl_id'])
    recorded = {key_for(row): row for row in historical}
    spec = public_spec(config.spec_key) if spec is None else spec
    public_keys = public_combination_keys(config, spec=spec)
    grouped = []
    for profile_id, talent_id, apl_id in public_keys:
        row = recorded.pop((profile_id, talent_id, apl_id), {
            'profile_id': profile_id, 'talent_id': talent_id, 'apl_id': apl_id,
            'record_count': 0, 'latest_id': None,
        })
        grouped.append(dict(row, is_public=True))
    grouped.extend(dict(row, is_public=False) for row in recorded.values())
    page = Paginator(grouped, 20).get_page(page_number)
    groups = list(page.object_list)
    profiles = SimcProfile.objects.in_bulk(g['profile_id'] for g in groups if g['profile_id'])
    talents = SimcTalentString.objects.in_bulk(g['talent_id'] for g in groups if g['talent_id'])
    from botend.services.simc_talent_titles import apply_talent_display_names
    apply_talent_display_names(list(talents.values()))
    apls = SimcApl.objects.in_bulk(g['apl_id'] for g in groups if g['apl_id'])
    wanted, signatures = Q(pk__in=[]), set()
    scenarios = active_scenarios(config) if scenarios is None else scenarios
    scenarios_by_id = {scenario.pk: scenario for scenario in scenarios}
    published = {kind: {row['id'] for row in spec.get(kind, [])}
                 for kind in ('profiles', 'talents', 'apls')}
    for group in groups:
        profile, talent, apl = profiles.get(group['profile_id']), talents.get(group['talent_id']), apls.get(group['apl_id'])
        group.update(profile=profile, talent=talent, apl=apl, scenes=[], issue='', has_active=False)
        group['preview_links'] = resource_links(config, {'profile': profile, 'talent': talent, 'apl': apl})
        wanted |= Q(profile_id=group['profile_id'], talent_id=group['talent_id'], apl_id=group['apl_id'])
        for scenario in scenarios:
            params = scenario.simulation_params
            scene = {'scenario_id': scenario.pk, 'name': scenario.name, 'targets': params['desired_targets'],
                     'duration': params['max_time'], 'iterations': params['iterations'], 'signature': ''}
            try:
                frozen = snapshot(config, profile, talent, apl, scenario, published_resources=published)
                scene['signature'] = _compute_content_hash(frozen)
                signatures.add(scene['signature'])
            except ValueError as exc:
                group['issue'] = str(exc)
            group['scenes'].append(scene)
    # Group historical status by stable scene identity, never by target count.
    latest_by_scene = {}
    status_keys = ('profile_id', 'talent_id', 'apl_id', 'scenario_id')
    status_ids = list(base.filter(wanted, scenario__in=scenarios).values(
        *status_keys,
    ).annotate(latest_id=Max('pk')).values_list('latest_id', flat=True))
    legacy_params = []
    for scenario in scenarios:
        try:
            legacy_params.append(scenario_simulation_params(scenario))
        except ValueError:
            continue
    legacy_ids = list(base.filter(wanted, scenario__isnull=True, snapshot__simulation_params__in=legacy_params).values(
        *status_keys, 'snapshot__simulation_params',
    ).annotate(latest_id=Max('pk')).values_list('latest_id', flat=True))
    status_rows = base.defer('error').in_bulk(status_ids + legacy_ids).values()
    for row in sorted(status_rows, key=lambda row: row.pk, reverse=True):
        scenario = scenarios_by_id.get(row.scenario_id) if row.scenario_id else scenario_for_snapshot(
            config, row.snapshot, scenarios=scenarios,
        )
        if scenario is not None:
            key = (row.profile_id, row.talent_id, row.apl_id, scenario.pk)
            latest_by_scene.setdefault(key, row)
    success_by_signature = _latest_successes(base, wanted, signatures)
    rankings = current_rankings(config) if success_by_signature else {}
    for group in groups:
        for scene in group['scenes']:
            scene['latest'] = latest_by_scene.get((group['profile_id'], group['talent_id'], group['apl_id'], scene['scenario_id']))
            scene['result'] = success_by_signature.get(scene['signature'])
            scene['ranking'] = rankings.get((config.pk, scene['result'].signature)) if scene['result'] else None
            scene['stale'] = bool(scene['latest'] and
                                  _compute_content_hash(normalized_snapshot(scene['latest'].snapshot)) != scene['signature'])
            if scene['result']:
                scene['signature'] = scene['result'].signature
            current_active = bool(scene['latest'] and not scene['stale'] and scene['latest'].status in ACTIVE)
            group['has_active'] |= current_active
            scene['tone'] = ('warning' if scene['stale'] else 'running' if current_active else
                             'error' if scene['latest'] and scene['latest'].status == 'failed' else
                             'success' if scene['latest'] and scene['latest'].status == 'completed' and scene['result'] else 'neutral')
        if not group['is_public']:
            group['issue'] = '历史组合，已不在当前公开范围'
        elif not group['apl']:
            group['issue'] = '请在公开配置管理中选择 APL'
        group['can_run'] = group['is_public'] and not group['issue']
        group['has_report'] = any(scene['result'] and scene['result'].has_report for scene in group['scenes'])
    page.object_list = groups
    return page
