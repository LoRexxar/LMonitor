"""当前公开组合的双维度排名；同路由、同条件，单体与多目标分别比较。"""

import math
from collections import defaultdict
from types import SimpleNamespace

from django.db.models import Case, Exists, F, IntegerField, OuterRef, Value, When

from botend.models import SimcReferencePublication, SimcReferenceRankingEntry
from botend.services.simc_reference_ranking_index import comparison_key
from botend.services.simc_reference_simulations import active_scenarios, scenario_simulation_params


def _read_queryset():
    """Read only index identities, names and the result's authoritative facts.

    Currentness is maintained by writers. No frozen input, resource payload,
    binary version or player/tooltip resolution belongs on this path.
    """
    publication = SimcReferencePublication.objects.filter(
        is_published=True, spec_key=OuterRef('spec_key'),
        profiles=OuterRef('profile_id'), talents=OuterRef('talent_id'), apls=OuterRef('apl_id'),
    )
    return SimcReferenceRankingEntry.objects.filter(
        Exists(publication), is_current=True,
        config__settings__is_enabled=True,
        config__settings__use_ptr=F('config__use_ptr'),
        config__settings__backend__is_active=True,
        config__settings__template__is_active=True,
        config__settings__template__is_selectable=True,
        config__settings__template__owner_user_id__isnull=True,
        config__settings__template__spec__in=('default', 'all', '*'),
        scenario__is_enabled=True, scenario__settings_id=F('config__settings_id'),
        profile__is_active=True, profile__user_id__isnull=True,
        profile__use_ptr=F('config__use_ptr'),
        talent__is_active=True, talent__is_selectable=True,
        talent__owner_user_id__isnull=True, talent__is_system=True,
        apl__is_active=True, apl__is_selectable=True,
        apl__owner_user_id__isnull=True, apl__is_system=True,
        result__status='completed', result__dps__gt=0,
    ).select_related('config', 'scenario', 'profile', 'talent', 'apl', 'result').only(
        'id', 'comparison_key', 'config_id', 'scenario_id', 'profile_id', 'talent_id', 'apl_id', 'result_id',
        'config__id', 'config__spec_key', 'config__use_ptr', 'config__settings_id',
        'scenario__id', 'scenario__settings_id', 'scenario__name', 'scenario__simulation_params',
        'profile__id', 'profile__name', 'talent__id', 'talent__name', 'apl__id', 'apl__name',
        'result__id', 'result__signature', 'result__dps',
    )


def _project(entry, params):
    return SimpleNamespace(
        pk=entry.result_id, signature=entry.result.signature, dps=entry.result.dps,
        config_id=entry.config_id, config=entry.config,
        profile_id=entry.profile_id, profile=entry.profile,
        talent_id=entry.talent_id, talent=entry.talent,
        apl_id=entry.apl_id, apl=entry.apl,
        current_scenario=entry.scenario, snapshot={'simulation_params': params},
    )


def _current_cohorts(config):
    """Current indexed results, separated by every comparable scene parameter."""
    if not config.backend_id or not config.template_id or not config.is_enabled:
        return []
    scenes = {}
    for scene in active_scenarios(config):
        try:
            params = scenario_simulation_params(scene)
            key = comparison_key(config, params)
        except ValueError:
            continue
        scenes.setdefault(key, []).append(params)
    # Ambiguous identical scenarios were never valid snapshot matches.
    keys = [key for key, matches in scenes.items() if len(matches) == 1]
    if not keys:
        return []
    cohorts = defaultdict(list)
    entries = _read_queryset().filter(
        comparison_key__in=keys, config__use_ptr=config.use_ptr,
        config__settings__backend_id=config.backend_id,
    )
    for entry in entries:
        if math.isfinite(entry.result.dps):
            cohorts[entry.comparison_key].append(_project(entry, scenes[entry.comparison_key][0]))
    return cohorts.values()


def current_rankings(config):
    """两个维度的名次摘要，与完整榜单共用候选和排序规则。"""
    rankings = {}
    for rows in _current_cohorts(config):
        by_spec = defaultdict(list)
        for row in rows:
            by_spec[row.config.spec_key].append(row)
        for dimension, groups in (('overall', [rows]), ('spec', by_spec.values())):
            for group in groups:
                previous, rank = None, 0
                for index, row in enumerate(sorted(group, key=lambda item: (-item.dps, item.pk)), 1):
                    if row.dps != previous:
                        rank = index
                    previous = row.dps
                    rankings.setdefault((row.config_id, row.signature), {}).update({
                        f'{dimension}_rank': rank, f'{dimension}_total': len(group),
                    })
    return rankings


def leaderboard(config, signature, scope):
    """按当前组合定位比较组，不允许借榜单读取历史或未公开资料。"""
    from botend.constants.wow import CLASS_CN, SPEC_CN, CLASS_SPEC_MAP
    from botend.services.simc_player_config import canonical_simc_profile_key
    from django.urls import reverse
    from urllib.parse import urlencode

    if scope not in ('overall', 'spec'):
        return None
    labels = {
        canonical_simc_profile_key('beast_mastery' if spec == 'BeastMastery' else spec.lower(), cls):
            f'{CLASS_CN[cls]} · {SPEC_CN[spec]}'
        for cls, specs in CLASS_SPEC_MAP.items() for spec in specs
    }
    cohorts = list(_current_cohorts(config))
    for candidates in cohorts:
        anchor = next((row for row in candidates if row.config_id == config.pk and row.signature == signature), None)
        if anchor is None:
            continue
        rows = candidates if scope == 'overall' else [row for row in candidates if row.config.spec_key == config.spec_key]
        entries, previous, rank = [], None, 0
        for index, row in enumerate(sorted(rows, key=lambda item: (-item.dps, item.pk)), 1):
            if row.dps != previous:
                rank = index
            previous = row.dps
            detail_url = reverse('portal_simc_reference') + '?' + urlencode({
                'spec': row.config.spec_key, 'profile': row.profile_id, 'talent': row.talent_id, 'apl': row.apl_id,
                'scenario': row.current_scenario.pk,
            })
            entries.append({
                'rank': rank, 'spec': labels.get(row.config.spec_key, row.config.spec_key),
                'profile': row.profile.name, 'talent': row.talent.name, 'apl': row.apl.name,
                'dps': row.dps, 'is_current': row.pk == anchor.pk,
                'detail_url': detail_url,
                'resource_links': {kind: detail_url + '#' + section for kind, section in (
                    ('profile', 'reference-equipment'), ('talent', 'reference-talents'), ('apl', 'reference-apl'),
                )},
                'preview_links': {
                    kind: reverse('simc_reference_resource_preview', kwargs={
                        'spec_key': row.config.spec_key, 'kind': kind, 'resource_id': resource.pk,
                    }) + '?' + urlencode({'config': row.config_id, 'profile': row.profile_id,
                                         'talent': row.talent_id, 'apl': row.apl_id})
                    for kind, resource in (('profile', row.profile), ('talent', row.talent), ('apl', row.apl))
                },
            })
        scene_signatures = {
            row.current_scenario.pk: row.signature
            for cohort in cohorts for row in cohort
            if (row.config_id, row.profile_id, row.talent_id, row.apl_id) ==
               (anchor.config_id, anchor.profile_id, anchor.talent_id, anchor.apl_id)
        }
        return {'entries': entries, 'scope_label': '全职业' if scope == 'overall' else '本专精',
                'targets': anchor.snapshot['simulation_params']['desired_targets'],
                'duration': anchor.snapshot['simulation_params']['max_time'], 'iterations': anchor.snapshot['simulation_params']['iterations'],
                'scenario_id': anchor.current_scenario.pk, 'scenario_name': anchor.current_scenario.name,
                'current': next(entry for entry in entries if entry['is_current']),
                'scene_signatures': scene_signatures,
                'profile_id': anchor.profile_id, 'talent_id': anchor.talent_id, 'apl_id': anchor.apl_id}
    return None


def ranking_page_context(config, signature, scope, *, admin=False):
    """独立榜单的范围、场景切换和返回配置链接。"""
    from urllib.parse import urlencode
    from django.urls import reverse

    if len(signature) != 64 or any(c not in '0123456789abcdef' for c in signature):
        return None
    board = leaderboard(config, signature, scope)
    if board is None:
        return None
    route = 'simc_reference_rankings' if admin else 'portal_simc_reference_rankings'
    board['scope_options'] = [
        {'label': label, 'selected': key == scope, 'url': reverse(route, args=[config.pk, signature, key])}
        for key, label in (('overall', '全职业'), ('spec', '当前专精'))
    ]
    board['scene_options'] = [
        {'label': f"{scene.name} · {scene.simulation_params['desired_targets']} 目标 · {scene.simulation_params['max_time']} 秒 · {scene.simulation_params['iterations']} 次迭代",
         'scenario_id': scene.pk, 'selected': scene.pk == board['scenario_id'],
         'url': reverse(route, args=[config.pk, board['scene_signatures'][scene.pk], scope])
                if scene.pk in board['scene_signatures'] else None}
        for scene in active_scenarios(config)
    ]
    board['return_url'] = (reverse('simc_reference_spec_results', args=[config.spec_key]) + '?' + urlencode({'config': config.pk})
                           if admin else reverse('portal_simc_reference') + '?' + urlencode({
                               'spec': config.spec_key, 'profile': board['profile_id'],
                               'talent': board['talent_id'], 'apl': board['apl_id'],
                           }))
    board.update(return_label='返回模拟结果管理' if admin else '返回当前配置', use_ptr=config.use_ptr,
                 admin_ranking=admin, module_section='rankings', module_reference_url=board['return_url'],
                 module_rankings_url=reverse(route, args=[config.pk, signature, scope]))
    return board


def ranking_entry(preferred_config_id=None):
    """Choose a public indexed default directly; never rebuild the leaderboard."""
    try:
        preferred = int(preferred_config_id)
    except (TypeError, ValueError, OverflowError):
        preferred = None
    entries = _read_queryset().annotate(
        preferred_order=Case(When(config_id=preferred, then=Value(0)), default=Value(1), output_field=IntegerField()),
        single_order=Case(When(scenario__simulation_params__desired_targets=1, then=Value(0)),
                          default=Value(1), output_field=IntegerField()),
    ).order_by('preferred_order', 'config__use_ptr', 'config__spec_key', 'config_id',
               'single_order', 'profile__name', 'talent__name', 'result_id')
    entry = entries.first()
    if entry is not None and math.isfinite(entry.result.dps):
        return entry.config_id, entry.result.signature, 'overall'
    return None
