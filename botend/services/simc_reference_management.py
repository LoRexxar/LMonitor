"""前台数据模拟管理的方案聚合；按页读取，历史不重复堆叠到首页。"""

from django.core.paginator import Paginator
from django.db.models import Count, Exists, Max, OuterRef, Q

from botend.models import SimcApl, SimcProfile, SimcTalentString, SimcReferenceSimulation, SimcTaskArtifact
from botend.services.simc_reference_simulations import ACTIVE, catalog, snapshot
from botend.services.simc_task_service import _compute_content_hash


def with_report(queryset):
    return queryset.annotate(has_report=Exists(SimcTaskArtifact.objects.filter(
        task_id=OuterRef('task_id'), run__task_id=OuterRef('task_id'),
        run__status='completed', artifact_type='html_report',
    )))


def public_combination_keys(config):
    """公开装备与公开天赋的矩阵；默认 APL 不可用时保留待配置行。"""
    spec = catalog().get(config.spec_key, {})
    apl_ids = {row['id'] for row in spec.get('apls', [])}
    return [
        (profile['id'], talent['id'], talent['default_apl_id'] if talent['default_apl_id'] in apl_ids else None)
        for profile in spec.get('profiles', []) if profile['use_ptr'] == config.use_ptr
        for talent in spec.get('talents', [])
    ]


def scheme_results(config, page_number):
    from botend.services.simc_reference_details import resource_links
    from botend.services.simc_reference_rankings import current_rankings
    base = SimcReferenceSimulation.objects.filter(config=config)
    historical = base.values('profile_id', 'talent_id', 'apl_id').annotate(
        record_count=Count('pk'), latest_id=Max('pk'),
    ).order_by('-latest_id')
    key_for = lambda row: (row['profile_id'], row['talent_id'], row['apl_id'])
    recorded = {key_for(row): row for row in historical}
    public_keys = public_combination_keys(config)
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
    apls = SimcApl.objects.in_bulk(g['apl_id'] for g in groups if g['apl_id'])
    wanted, signatures = Q(pk__in=[]), set()
    for group in groups:
        profile, talent, apl = profiles.get(group['profile_id']), talents.get(group['talent_id']), apls.get(group['apl_id'])
        group.update(profile=profile, talent=talent, apl=apl, scenes=[], issue='', has_active=False)
        group['preview_links'] = resource_links(config, {'profile': profile, 'talent': talent, 'apl': apl})
        wanted |= Q(profile_id=group['profile_id'], talent_id=group['talent_id'], apl_id=group['apl_id'])
        for targets in config.targets:
            scene = {'targets': targets, 'duration': config.duration, 'signature': ''}
            try:
                scene['signature'] = _compute_content_hash(snapshot(config, profile, talent, apl, targets))
                signatures.add(scene['signature'])
            except ValueError as exc:
                group['issue'] = str(exc)
            group['scenes'].append(scene)
    latest = list(base.filter(wanted).values(
        'profile_id', 'talent_id', 'apl_id', 'snapshot__simulation_params__desired_targets',
    ).annotate(latest_id=Max('pk')))
    successful = list(base.filter(signature__in=signatures, status='completed', dps__gt=0).values('signature').annotate(latest_id=Max('pk')))
    ids = {row['latest_id'] for row in latest + successful}
    rows = with_report(SimcReferenceSimulation.objects.filter(pk__in=ids)).in_bulk()
    latest_by_scene = {(r['profile_id'], r['talent_id'], r['apl_id'], r['snapshot__simulation_params__desired_targets']): rows[r['latest_id']] for r in latest}
    success_by_signature = {r['signature']: rows[r['latest_id']] for r in successful}
    rankings = current_rankings(config) if successful else {}
    for group in groups:
        for scene in group['scenes']:
            scene['latest'] = latest_by_scene.get((group['profile_id'], group['talent_id'], group['apl_id'], scene['targets']))
            scene['result'] = success_by_signature.get(scene['signature'])
            scene['ranking'] = rankings.get((config.pk, scene['signature'])) if scene['result'] else None
            scene['stale'] = bool(scene['latest'] and scene['latest'].signature != scene['signature'])
            current_active = bool(scene['latest'] and not scene['stale'] and scene['latest'].status in ACTIVE)
            group['has_active'] |= current_active
            scene['tone'] = ('warning' if scene['stale'] else 'running' if current_active else
                             'error' if scene['latest'] and scene['latest'].status == 'failed' else
                             'success' if scene['latest'] and scene['latest'].status == 'completed' and scene['result'] else 'neutral')
        if not group['is_public']:
            group['issue'] = '历史组合，已不在当前公开范围或默认 APL 已变更'
        elif not group['apl']:
            group['issue'] = '请为该天赋绑定已公开的默认 APL'
        group['can_run'] = group['is_public'] and not group['issue']
        group['has_report'] = any(scene['result'] and scene['result'].has_report for scene in group['scenes'])
    page.object_list = groups
    return page
