"""当前公开组合的双维度排名；同条件、同版本，单体与多目标分别比较。"""

import math
from collections import defaultdict

from django.db.models import Max

from botend.models import SimcReferencePublication, SimcReferenceSimulation
from botend.services.simc_reference_simulations import snapshot, validate_config
from botend.services.simc_task_service import _compute_content_hash


def _current_cohorts(config):
    """返回按可比条件分组的当前有效结果，覆盖所有专精和分页。

    模板按实际正文匹配，不按专精标签或模板 ID 匹配。每份签名只取最新成功
    结果；重跑期间保留原有效结果。分母为组合数，并列采用 1、1、3 名次。
    """
    if not config.backend_id or not config.template_id or not config.is_enabled:
        return []
    # 先在数据库缩小到可比较的执行环境，避免载入无关版本的历史记录。
    candidates = SimcReferenceSimulation.objects.filter(
        status='completed', dps__gt=0, config__is_enabled=True,
        config__use_ptr=config.use_ptr, config__duration=config.duration,
        config__iterations=config.iterations,
        config__backend__current_version=config.backend.current_version,
        config__backend__game_build=config.backend.game_build,
        config__template__content=config.template.content,
    )
    latest_ids = candidates.order_by().values('config_id', 'signature').annotate(latest=Max('pk')).values('latest')
    rows = SimcReferenceSimulation.objects.filter(pk__in=latest_ids).select_related(
        'config__backend', 'config__template', 'profile', 'talent', 'apl',
    )
    published = {}
    publications = SimcReferencePublication.objects.filter(is_published=True)
    for kind in ('profiles', 'talents', 'apls'):
        for spec, resource_id in publications.values_list('spec_key', f'{kind}__pk'):
            published.setdefault(spec, {}).setdefault(kind, set()).add(resource_id)

    current_signatures, cohorts = {}, defaultdict(list)
    for row in rows:
        if not math.isfinite(row.dps):
            continue
        params = (row.snapshot or {}).get('simulation_params', {})
        targets = params.get('desired_targets')
        if type(targets) is not int or targets not in row.config.targets or targets not in config.targets:
            continue
        key = (row.config_id, row.profile_id, row.talent_id, row.apl_id, targets)
        if key not in current_signatures:
            try:
                frozen = snapshot(row.config, row.profile, row.talent, row.apl, targets,
                                  published_resources=published.get(row.config.spec_key, {}))
                current_signatures[key] = _compute_content_hash(frozen)
            except ValueError:
                current_signatures[key] = None
        if current_signatures[key] != row.signature:
            continue
        # 所有战斗参数均进入比较组，避免今后增加场景参数时混榜。
        cohort = _compute_content_hash({
            'params': params, 'use_ptr': row.config.use_ptr,
            'backend': [row.config.backend.current_version, row.config.backend.game_build],
            'template': row.config.template.content,
        })
        cohorts[cohort].append(row)

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
    from botend.services.simc_reference_details import resource_links
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
            })
            entries.append({
                'rank': rank, 'spec': labels.get(row.config.spec_key, row.config.spec_key),
                'profile': row.profile.name, 'talent': row.talent.name, 'apl': row.apl.name,
                'dps': row.dps, 'is_current': row.pk == anchor.pk,
                'detail_url': detail_url,
                'resource_links': {kind: detail_url + '#' + section for kind, section in (
                    ('profile', 'reference-equipment'), ('talent', 'reference-talents'), ('apl', 'reference-apl'),
                )},
                'preview_links': resource_links(row.config, {'profile': row.profile, 'talent': row.talent, 'apl': row.apl}),
            })
        scene_signatures = {
            row.snapshot['simulation_params']['desired_targets']: row.signature
            for cohort in cohorts for row in cohort
            if (row.config_id, row.profile_id, row.talent_id, row.apl_id) ==
               (anchor.config_id, anchor.profile_id, anchor.talent_id, anchor.apl_id)
        }
        return {'entries': entries, 'scope_label': '全职业' if scope == 'overall' else '本专精',
                'targets': anchor.snapshot['simulation_params']['desired_targets'],
                'duration': config.duration, 'iterations': config.iterations,
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
        {'label': '单体' if targets == 1 else f'{targets} 目标', 'selected': targets == board['targets'],
         'url': reverse(route, args=[config.pk, board['scene_signatures'][targets], scope])
                if targets in board['scene_signatures'] else None}
        for targets in config.targets
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
    """独立入口优先使用指定配置，再选择有当前公开结果的比较组。"""
    from botend.models import SimcReferenceSimulationConfig
    configs = list(SimcReferenceSimulationConfig.objects.filter(
        is_enabled=True,
        spec_key__in=SimcReferencePublication.objects.filter(is_published=True).values('spec_key'),
        pk__in=SimcReferenceSimulation.objects.filter(status='completed', dps__gt=0).values('config_id'),
    ).select_related('backend', 'template').order_by('use_ptr', 'spec_key', 'pk'))
    configs.sort(key=lambda config: str(config.pk) != str(preferred_config_id))
    checked = set()
    for config in configs:
        try:
            validate_config(config)
        except ValueError:
            continue
        condition = _compute_content_hash([config.use_ptr, config.backend.current_version, config.backend.game_build,
                                           config.template.content, config.duration, config.iterations, sorted(config.targets)])
        if condition in checked:
            continue
        checked.add(condition)
        rows = [row for cohort in _current_cohorts(config) for row in cohort]
        if rows:
            row = min(rows, key=lambda row: (row.config_id != config.pk,
                      row.snapshot['simulation_params']['desired_targets'] != 1, row.config.spec_key, row.profile.name, row.talent.name, row.pk))
            return row.config_id, row.signature, 'overall'
    return None
