"""当前公开组合的双维度排名；同路由、同条件，单体与多目标分别比较。"""

import math
from collections import defaultdict
from types import SimpleNamespace
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.http import Http404
from django.db.models import Case, Exists, F, IntegerField, OuterRef, Value, When

from botend.constants.wow import specialization_catalog, SPEC_ROLE
from botend.constants.hero_talents import HERO_SUBTREE_NAME_ZH, normalize_hero_subtree_names_zh
from botend.models import SimcReferencePublication, SimcReferenceRankingEntry
from botend.services.simc_player_config import canonical_simc_profile_key
from botend.services.simc_scenario_display import scenario_label
from botend.services.simc_reference_ranking_index import comparison_key
from botend.services.simc_reference_simulations import active_scenarios, scenario_simulation_params


ROLE_LABELS = {'dps': '输出', 'tank': '坦克', 'healer': '治疗', 'unknown': '未识别'}


@lru_cache(maxsize=1)
def _spec_metadata():
    """Local authoritative labels/icons/roles; never load the resource catalog."""
    metadata = {}
    for spec in specialization_catalog():
        key = canonical_simc_profile_key(
            'beast_mastery' if spec['spec_name'] == 'BeastMastery' else spec['spec_name'].lower(),
            spec['class_name'],
        )
        if key:
            role = SPEC_ROLE.get((spec['class_name'], spec['spec_name']), 'unknown')
            metadata[key] = dict(spec=spec['label'], icon=spec['icon'], role=role,
                                 role_label=ROLE_LABELS[role])
    return metadata


def ranking_spec_metadata(spec_key):
    return dict(_spec_metadata().get(spec_key, dict(
        spec=spec_key, icon='', role='unknown', role_label=ROLE_LABELS['unknown'],
    )))


def validate_ranking_role(role):
    if role is not None and role not in ROLE_LABELS:
        raise Http404('排行榜职责不存在')
    return role


def ranking_role_url(url, role):
    """Retain the original path, query identities and fragment when adding role."""
    validate_ranking_role(role)
    if not url or role is None:
        return url
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != 'role']
    return urlunsplit(parts._replace(query=urlencode(query + [('role', role)])))


def apply_ranking_role(board, role, path):
    """Pure projection: one role per page, while retaining the anchor card."""
    validate_ranking_role(role)
    entries = board.get('entries', [])
    current = board.get('current')
    present = {entry['role'] for entry in entries}
    if role is None:
        role = (current['role'] if current and not board.get('admin_ranking') else
                next((key for key in ROLE_LABELS if key in present), 'dps'))
    board.update(
        entries=[entry for entry in entries if entry['role'] == role],
        selected_role=role, role_label=ROLE_LABELS[role],
        role_options=[dict(label=label, url=ranking_role_url(path, key), selected=key == role)
                      for key, label in ROLE_LABELS.items() if key != 'unknown' or key in present or key == role],
        current_in_ranking=bool(current and current['role'] == role and
                                any(entry['is_current'] for entry in entries)),
    )
    board['empty_ranking'] = not board['entries']
    for name in ('scope_options', 'scene_options', 'settings_options'):
        board[name] = [dict(option, url=ranking_role_url(option.get('url'), role))
                       for option in board.get(name, [])]
    if board.get('module_rankings_url'):
        board['module_rankings_url'] = ranking_role_url(board['module_rankings_url'], role)
    return board


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
        'id', 'comparison_key', 'talent_display_name', 'config_id', 'scenario_id', 'profile_id', 'talent_id', 'apl_id', 'result_id',
        'config__id', 'config__spec_key', 'config__use_ptr', 'config__settings_id',
        'scenario__id', 'scenario__settings_id', 'scenario__name', 'scenario__simulation_params',
        'profile__id', 'profile__name', 'talent__id', 'talent__name', 'talent__hero_talent_names', 'apl__id', 'apl__name',
        'result__id', 'result__signature', 'result__dps',
    )


def _project(entry, params):
    heroes = normalize_hero_subtree_names_zh(entry.talent.hero_talent_names)
    return SimpleNamespace(
        pk=entry.result_id, signature=entry.result.signature, dps=entry.result.dps,
        config_id=entry.config_id, config=entry.config,
        profile_id=entry.profile_id, profile=entry.profile,
        talent_id=entry.talent_id, talent=entry.talent,
        talent_display_name=entry.talent_display_name or entry.talent.name,
        hero=heroes[0] if len(heroes) == 1 and heroes[0] in HERO_SUBTREE_NAME_ZH.values() else '',
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
    return [cohorts[key] for key in keys if key in cohorts]


def _hero_winners(rows):
    """每个专精、每棵英雄树保留一个最高 DPS 组合；同分按稳定的配置标识取舍。"""
    winners = {}
    for row in sorted(rows, key=lambda item: (-item.dps, item.config_id, item.profile_id,
                                             item.talent_id, item.apl_id, item.pk)):
        if row.hero:
            winners.setdefault((row.config.spec_key, row.hero), row)
    return list(winners.values())


def _positions(rows):
    """按职责分别计算竞赛排名，摘要与榜单使用完全相同的并列规则。"""
    positions, counts, previous = {}, defaultdict(int), {}
    for row in sorted(rows, key=lambda item: (-item.dps, item.pk)):
        role = ranking_spec_metadata(row.config.spec_key)['role']
        counts[role] += 1
        last_dps, last_rank = previous.get(role, (None, 0))
        rank = last_rank if row.dps == last_dps else counts[role]
        previous[role] = row.dps, rank
        positions[row.pk] = rank
    return positions, counts


def _cohort_rankings(cohorts):
    """同一批索引结果同时生成 DPS 和名次，避免重复读取。"""
    rankings = {}
    for rows in cohorts:
        by_spec = defaultdict(list)
        overall_positions, overall_counts = _positions(_hero_winners(rows))
        for row in rows:
            by_spec[row.config.spec_key].append(row)
        for group in by_spec.values():
            spec_positions, _ = _positions(group)
            for row in group:
                role = ranking_spec_metadata(row.config.spec_key)['role']
                rankings[(row.config_id, row.signature)] = dict(
                    overall_rank=overall_positions.get(row.pk), overall_total=overall_counts[role],
                    spec_rank=spec_positions[row.pk], spec_total=len(group),
                )
    return rankings


def current_rankings(config):
    """两个维度的名次摘要，与完整榜单共用候选和排序规则。"""
    return _cohort_rankings(_current_cohorts(config))


def configuration_results(config, profile_id, talent_id, apl_id):
    """配置页仅消费现行索引，不重建输入或扫描历史结果 JSON。"""
    cohorts = _current_cohorts(config)
    rankings = _cohort_rankings(cohorts)
    results = []
    for rows in cohorts:
        for row in rows:
            if (row.config_id, row.profile_id, row.talent_id, row.apl_id) != (
                    config.pk, profile_id, talent_id, apl_id):
                continue
            scene = row.current_scenario
            params = row.snapshot['simulation_params']
            results.append(dict(params, dps=row.dps, ranking=rankings[(row.config_id, row.signature)],
                config_id=row.config_id, signature=row.signature, scenario_id=scene.pk,
                scenario_name=scene.name, name=scene.name,
                targets=params['desired_targets'], duration=params['max_time']))
    return results


def _ranked_entries(rows, anchor_pk=None):
    """One formatter for public contextual rankings and admin condition groups."""
    from django.urls import reverse
    from urllib.parse import urlencode
    entries = []
    rows = list(rows)
    positions, _ = _positions(rows)
    for row in sorted(rows, key=lambda item: (-item.dps, item.pk)):
        metadata = ranking_spec_metadata(row.config.spec_key)
        detail_url = reverse('portal_simc_reference') + '?' + urlencode({
            'spec': row.config.spec_key, 'profile': row.profile_id, 'talent': row.talent_id, 'apl': row.apl_id,
            'scenario': row.current_scenario.pk,
        })
        entries.append({
            'rank': positions[row.pk], **metadata, 'hero': row.hero,
            'profile': row.profile.name, 'talent': row.talent_display_name, 'apl': row.apl.name,
            'dps': row.dps, 'is_current': row.pk == anchor_pk,
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
    return entries


def leaderboard(config, signature, scope):
    """按当前组合定位比较组，不允许借榜单读取历史或未公开资料。"""
    if scope not in ('overall', 'spec'):
        return None
    cohorts = list(_current_cohorts(config))
    for candidates in cohorts:
        anchor = next((row for row in candidates if row.config_id == config.pk and row.signature == signature), None)
        if anchor is None:
            continue
        rows = _hero_winners(candidates) if scope == 'overall' else [row for row in candidates if row.config.spec_key == config.spec_key]
        entries = _ranked_entries(rows, anchor.pk)
        current = next((entry for entry in entries if entry['is_current']), None)
        if current is None:
            current = _ranked_entries([anchor], anchor.pk)[0]
            current['rank'] = None
        scene_signatures = {
            row.current_scenario.pk: row.signature
            for cohort in cohorts for row in cohort
            if (row.config_id, row.profile_id, row.talent_id, row.apl_id) ==
               (anchor.config_id, anchor.profile_id, anchor.talent_id, anchor.apl_id)
        }
        return {'entries': entries, 'scope_label': '全职业' if scope == 'overall' else '本专精',
                'hero_winners_only': scope == 'overall',
                'targets': anchor.snapshot['simulation_params']['desired_targets'],
                'duration': anchor.snapshot['simulation_params']['max_time'], 'iterations': anchor.snapshot['simulation_params']['iterations'],
                'scenario_id': anchor.current_scenario.pk, 'scenario_name': anchor.current_scenario.name,
                'current': current,
                'scene_signatures': scene_signatures,
                'profile_id': anchor.profile_id, 'talent_id': anchor.talent_id, 'apl_id': anchor.apl_id}
    return None


def admin_ranking_page_context(settings_id=None, scenario_id=None):
    """A complete settings/scenario leaderboard, independent of any result/spec."""
    from urllib.parse import urlencode
    from django.urls import reverse
    from botend.models import SimcReferenceSimulationSettings

    available = list(SimcReferenceSimulationSettings.objects.filter(
        is_enabled=True, backend__is_active=True, template__is_active=True,
        template__is_selectable=True, template__owner_user_id__isnull=True,
        template__spec__in=('default', 'all', '*'),
    ).select_related('template').only(
        'id', 'use_ptr', 'backend_id', 'template_id', 'template__id', 'template__content',
    ).order_by('use_ptr', 'pk'))
    root = reverse('simc_reference_rankings_entry')
    base = {'entries': [], 'current': None, 'scope_options': [], 'settings_options': [], 'hero_winners_only': True,
            'scene_options': [], 'empty_ranking': True, 'admin_ranking': True,
            'module_section': 'rankings', 'module_reference_url': reverse('simc_reference_results'),
            'module_rankings_url': root, 'return_url': reverse('simc_reference_results'),
            'return_label': '返回模拟结果管理', 'scope_label': '全部方案'}
    if not available:
        return None if settings_id is not None or scenario_id is not None else base
    selected = next((item for item in available if item.pk == settings_id), None) if settings_id is not None else available[0]
    if selected is None:
        return None
    def url(setting, scene=None):
        params = {'settings': setting.pk}
        if scene is not None:
            params['scenario'] = scene.pk
        return root + '?' + urlencode(params)
    base['settings_options'] = [dict(label='PTR' if item.use_ptr else '正式服',
        selected=item.pk == selected.pk, url=url(item)) for item in available]
    scenes = list(selected.scenarios.filter(is_enabled=True).order_by('display_order', 'pk'))
    scene = next((item for item in scenes if item.pk == scenario_id), None) if scenario_id is not None else (scenes[0] if scenes else None)
    if scene is None:
        return None if scenario_id is not None else base
    try:
        params = scenario_simulation_params(scene)
    except ValueError:
        return None
    routing = SimpleNamespace(use_ptr=selected.use_ptr, backend_id=selected.backend_id, template=selected.template)
    key = comparison_key(routing, params)
    rows = [_project(entry, params) for entry in _read_queryset().filter(
        config__settings_id=selected.pk, scenario_id=scene.pk, comparison_key=key,
    ) if math.isfinite(entry.result.dps)]
    rows = _hero_winners(rows)
    base.update(entries=_ranked_entries(rows), empty_ranking=not rows,
                use_ptr=selected.use_ptr, targets=params['desired_targets'], duration=params['max_time'],
                iterations=params['iterations'], scenario_id=scene.pk, scenario_name=scene.name,
                settings_id=selected.pk, module_rankings_url=url(selected, scene))
    base['scene_options'] = [dict(label=scenario_label(item.name, item.simulation_params.get('desired_targets'), item.simulation_params.get('max_time')), scenario_id=item.pk,
        selected=item.pk == scene.pk, url=url(selected, item)) for item in scenes]
    return base


def ranking_page_context(config, signature, scope, *, admin=False):
    """独立榜单的范围、场景切换和返回配置链接。"""
    from urllib.parse import urlencode
    from django.urls import reverse

    if len(signature) != 64 or any(c not in '0123456789abcdef' for c in signature):
        return None
    if admin:
        scene_id = _read_queryset().filter(config_id=config.pk, result__signature=signature).values_list('scenario_id', flat=True).first()
        return admin_ranking_page_context(config.settings_id, scene_id)
    board = leaderboard(config, signature, scope)
    if board is None:
        return None
    route = 'simc_reference_rankings' if admin else 'portal_simc_reference_rankings'
    board['scope_options'] = [
        {'label': label, 'selected': key == scope, 'url': reverse(route, args=[config.pk, signature, key])}
        for key, label in (('overall', '全职业'), ('spec', '当前专精'))
    ]
    board['scene_options'] = [
        {'label': scenario_label(scene.name, scene.simulation_params['desired_targets'], scene.simulation_params['max_time']),
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
