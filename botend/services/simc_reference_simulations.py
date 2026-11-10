"""公开配置组合的专用模拟目录、队列投递和结果投影。"""

import logging
import math
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from botend.models import (
    SimcApl, SimcBackendBinary, SimcContentTemplate, SimcProfile,
    SimcReferenceSimulation, SimcReferenceSimulationConfig, SimcTalentString, SimcReferencePublication,
)
from botend.services import simc_task_service as tasks
from botend.services.simc_player_config import canonical_simc_profile_key

logger = logging.getLogger(__name__)
ACTIVE = ('queued', 'preparing', 'submitted')


class ReferenceAgentIncompatible(ValueError):
    """当前 Agent 无法执行指定版本，可由其他执行者领取。"""


def execution_backend_metadata(task, *, agent=None):
    """仅为专用任务核对执行版本；不改变普通任务的后端选择规则。"""
    request = (task.mode_params or {}).get('request_manifest') or {}
    expected = request.get('reference_backend') if isinstance(request, dict) else None
    if expected is None:
        return {}
    backend = SimcBackendBinary.objects.filter(pk=task.backend_id).first()
    if (not isinstance(expected, list) or len(expected) != 3 or not backend
            or expected != [backend.pk, backend.current_version, backend.game_build]):
        raise ValueError('公开配置模拟的后端版本已变化，请重新生成')
    if agent is not None and (agent.capabilities or {}).get('binary_revision') != expected[1]:
        raise ReferenceAgentIncompatible('当前 Agent 的实测二进制版本不匹配公开配置模拟')
    return {'backend_version': expected[1], 'reference_backend': expected}


def catalog(*, public_only=True):
    """与资料页使用同一份公开目录，保留缺失默认 APL 的组合供管理员检查。"""
    from botend.services.simc_reference import reference_catalog
    return reference_catalog(public_only=public_only)[1]


def initialize_configs():
    """仅在管理员明确同步目录时创建配置，不在前台 GET 写库。"""
    for key, spec in catalog(public_only=False).items():
        if not spec['has_data']:
            continue
        SimcReferencePublication.objects.get_or_create(spec_key=key)
        for use_ptr in sorted({row['use_ptr'] for row in spec['profiles']} or {False}):
            templates = list(SimcContentTemplate.objects.filter(
                is_active=True, is_selectable=True, owner_user_id__isnull=True,
                spec__in=(key, key.split('_', 1)[1], 'default', 'all', '*'),
            ).order_by('pk'))
            specific = [row for row in templates if row.spec not in ('default', 'all', '*')]
            choices = specific or templates
            backend = SimcBackendBinary.objects.filter(
                is_active=True, identifier='ptr' if use_ptr else 'production',
            ).first()
            SimcReferenceSimulationConfig.objects.get_or_create(
                spec_key=key, use_ptr=use_ptr,
                defaults={'backend': backend, 'template': choices[0] if len(choices) == 1 else None,
                          'targets': [1, 5]},
            )


def validate_config(config):
    if not config.is_enabled:
        raise ValueError('该专精的模拟维护已停用')
    if not config.backend_id or not config.backend.is_active:
        raise ValueError('请先选择可用的模拟后端')
    if not config.backend.current_version:
        raise ValueError('模拟后端尚未记录构建版本')
    template = config.template
    if not template or not template.is_active or not template.is_selectable or template.owner_user_id:
        raise ValueError('请先选择可用的共享模板')
    if template.spec not in (config.spec_key, config.spec_key.split('_', 1)[1], 'default', 'all', '*'):
        raise ValueError('模板与专精不匹配')
    if type(config.duration) is not int or not 10 <= config.duration <= 1800:
        raise ValueError('战斗时长须为 10–1800 秒')
    if type(config.iterations) is not int or not 100 <= config.iterations <= 100000:
        raise ValueError('迭代次数须为 100–100000')
    if (not isinstance(config.targets, list) or not 1 <= len(config.targets) <= 10
            or any(type(n) is not int or not 1 <= n <= 20 for n in config.targets)
            or len(set(config.targets)) != len(config.targets)):
        raise ValueError('目标数量须为 1–20 之间不重复的整数，最多 10 个场景')


def _public_resources(config, profile, talent, apl, published_resources=None):
    if not all((profile, talent, apl)):
        raise ValueError('配置资源已删除或天赋尚未绑定默认 APL')
    published = (all(resource.pk in published_resources.get(kind, ()) for kind, resource in
                     (('profiles', profile), ('talents', talent), ('apls', apl)))
                 if published_resources is not None else SimcReferencePublication.objects.filter(
                     spec_key=config.spec_key, is_published=True, profiles=profile, talents=talent, apls=apl,
                 ).exists())
    if not published:
        raise ValueError('该方案包含未在前台公开范围中指定的资料')
    if not profile.is_active or profile.user_id is not None or profile.use_ptr != config.use_ptr:
        raise ValueError('装备不属于当前分支的共享配置')
    if profile.player_config_mode != 'manual_equipment':
        raise ValueError('共享装备须使用固定装备输入，不能使用动态角色导入')
    for resource in (talent, apl):
        if not resource.is_active or not resource.is_selectable or not resource.is_system or resource.owner_user_id is not None:
            raise ValueError('天赋或 APL 已停用或不再公开')
    if talent.default_apl_id != apl.pk:
        raise ValueError('天赋绑定的默认 APL 已变化')
    for resource in (profile, talent, apl):
        if canonical_simc_profile_key(resource.spec, getattr(resource, 'class_name', '')) != config.spec_key:
            raise ValueError('装备、天赋和 APL 必须属于同一专精')


def snapshot(config, profile, talent, apl, targets, *, published_resources=None):
    validate_config(config)
    _public_resources(config, profile, talent, apl, published_resources)
    return {
        'spec': config.spec_key, 'use_ptr': config.use_ptr,
        'resources': {
            'profile': [profile.pk, tasks._compute_content_hash(tasks._build_profile_payload(profile))],
            'talent': [talent.pk, tasks._compute_content_hash(tasks._build_talent_payload(talent))],
            'apl': [apl.pk, tasks._compute_content_hash(tasks._build_apl_payload(apl))],
            'template': [config.template_id, tasks._compute_content_hash(tasks._build_template_payload(config.template))],
        },
        'backend': [config.backend_id, config.backend.current_version, config.backend.game_build],
        'simulation_params': {
            'desired_targets': targets, 'max_time': config.duration, 'iterations': config.iterations,
            'fight_style': 'Patchwerk', 'vary_combat_length': 0,
            'raid_buffs': [], 'use_class_raid_buff': False, 'extra_options': [],
        },
    }


def enqueue_configs(config_ids, user_id, *, force=False, combination=None):
    """在短事务内登记矩阵，实际任务由 Worker 分批派发。重复点击不会重复排队。"""
    directory = catalog()
    result = {'created': 0, 'skipped': 0, 'errors': []}
    for config_id in config_ids:
        with transaction.atomic():
            config = SimcReferenceSimulationConfig.objects.select_for_update().get(pk=config_id)
            spec = directory.get(config.spec_key, {})
            try:
                validate_config(config)
            except ValueError as exc:
                result['errors'].append(f'{config.spec_key}：{exc}')
                continue
            profiles = SimcProfile.objects.in_bulk(r['id'] for r in spec.get('profiles', []) if r['use_ptr'] == config.use_ptr)
            talents = SimcTalentString.objects.in_bulk(r['id'] for r in spec.get('talents', []))
            apls = SimcApl.objects.in_bulk(r['id'] for r in spec.get('apls', []))
            for profile in profiles.values():
                for talent in talents.values():
                    apl = apls.get(talent.default_apl_id)
                    if combination and (profile.pk, talent.pk, talent.default_apl_id) != combination:
                        continue
                    try:
                        snapshots = [snapshot(config, profile, talent, apl, n) for n in config.targets]
                    except ValueError as exc:
                        result['errors'].append(f'{profile.name} / {talent.name}：{exc}')
                        continue
                    for frozen in snapshots:
                        signature = tasks._compute_content_hash(frozen)
                        states = ACTIVE if force else (*ACTIVE, 'completed')
                        if SimcReferenceSimulation.objects.filter(config=config, signature=signature, status__in=states).exists():
                            result['skipped'] += 1
                            continue
                        SimcReferenceSimulation.objects.create(
                            config=config, profile=profile, talent=talent, apl=apl,
                            requested_by_id=user_id, signature=signature, snapshot=frozen,
                        )
                        result['created'] += 1
    return result


def _fail(row_id, message, *, claim=None):
    query = SimcReferenceSimulation.objects.filter(pk=row_id, status__in=ACTIVE)
    if claim is not None:
        query = query.filter(status='preparing', updated_at=claim)
    query.update(
        status='failed', error=str(message)[:2000], updated_at=timezone.now(),
    )


def maintain_simulations(batch_size=5):
    """沿用现有 Worker 生命周期，失败留档、失去进程的准备项可恢复。"""
    now = timezone.now()
    SimcReferenceSimulation.objects.filter(
        status='preparing', task__isnull=True, updated_at__lt=now - timedelta(minutes=15),
    ).update(status='queued', updated_at=now)
    for row in SimcReferenceSimulation.objects.filter(status='submitted').filter(
        Q(task__current_status__gte=2) | Q(task__is_active=False) | Q(task__isnull=True),
    ).select_related('task').order_by('pk')[:100]:
        task = row.task
        if not task or not task.is_active or task.current_status == 3:
            _fail(row.pk, '模拟任务失败或已停用，请查看任务记录后重新生成')
        elif task.current_status == 2:
            runs = list(task.simulation_runs.values(
                'status', 'result_summary__dps', 'result_summary__valid', 'resource_manifest__backend_version',
            )[:2])
            dps = runs[0]['result_summary__dps'] if len(runs) == 1 else None
            version = runs[0]['resource_manifest__backend_version'] if runs else None
            valid = bool(runs and runs[0]['status'] == 'completed' and runs[0]['result_summary__valid'] is not False)
            if not valid or not isinstance(dps, (int, float)) or isinstance(dps, bool) or not math.isfinite(dps) or dps <= 0:
                _fail(row.pk, '任务未产生唯一、有效的 DPS 结果')
            elif version != row.snapshot['backend'][1]:
                _fail(row.pk, '实际执行后端版本与登记版本不一致，请重新生成')
            else:
                SimcReferenceSimulation.objects.filter(pk=row.pk, status='submitted').update(
                    status='completed', dps=dps, updated_at=now, error='',
                )
    ids = list(SimcReferenceSimulation.objects.filter(status='queued').order_by('pk').values_list('pk', flat=True)[:batch_size])
    for row_id in ids:
        claim = timezone.now()
        if not SimcReferenceSimulation.objects.filter(pk=row_id, status='queued').update(status='preparing', updated_at=claim):
            continue
        try:
            row = SimcReferenceSimulation.objects.select_related('config__backend', 'config__template', 'profile', 'talent', 'apl').get(pk=row_id)
            config = row.config
            current = snapshot(config, row.profile, row.talent, row.apl, row.snapshot['simulation_params']['desired_targets'])
            if current != row.snapshot or current['simulation_params']['desired_targets'] not in config.targets:
                raise ValueError('配置已更新，请重新生成当前组合')
            kwargs = dict(user_id=row.requested_by_id, profile_id=row.profile_id, talent_string_id=row.talent_id,
                          apl_id=row.apl_id, template_id=config.template_id, backend_id=config.backend_id, is_admin=True)
            prepared = tasks.prepare_task_creation(**kwargs)
            with transaction.atomic():
                locked = SimcReferenceSimulation.objects.select_for_update().get(pk=row_id)
                if locked.status != 'preparing' or locked.updated_at != claim:
                    continue
                task = tasks.create_task_from_prepared(
                    prepared=prepared, **kwargs, name=f'公开配置 · {config.spec_key} · {row.pk}',
                    mode='normal', simulation_params=row.snapshot['simulation_params'],
                    mode_params={'request_manifest': {'reference_backend': row.snapshot['backend']}},
                    is_benchmark_task=True, benchmark_queue_priority=20,
                )
                actual = {kind: [getattr(task, f'{kind}_version').resource_id,
                                 getattr(task, f'{kind}_version').content_hash]
                          for kind in ('profile', 'talent', 'apl', 'template')}
                if actual != row.snapshot['resources'] or prepared.validation_identity[0] != row.snapshot['backend'][1]:
                    raise ValueError('任务创建期间资源版本发生变化，请重新生成')
                locked.task, locked.status = task, 'submitted'
                locked.save(update_fields=['task', 'status', 'updated_at'])
        except Exception as exc:
            logger.exception('公开配置模拟派发失败：%s', row_id)
            _fail(row_id, exc, claim=claim)


def public_results(spec_key, profile, talent, apl):
    """仅返回本模块创建且严格匹配当前公开输入的结果。"""
    if not all((profile, talent, apl)):
        return []
    config = SimcReferenceSimulationConfig.objects.select_related('backend', 'template').filter(
        spec_key=spec_key, use_ptr=profile.use_ptr, is_enabled=True,
    ).first()
    if not config:
        return []
    try:
        snapshots = [snapshot(config, profile, talent, apl, n) for n in config.targets]
    except ValueError:
        return []
    signatures = {tasks._compute_content_hash(value): value for value in snapshots}
    found = {}
    for signature in signatures:
        row = SimcReferenceSimulation.objects.filter(
            config=config, signature=signature, status='completed', dps__gt=0,
        ).order_by('-pk').values('dps').first()
        if row and math.isfinite(row['dps']):
            found[signature] = row['dps']
    from botend.services.simc_reference_rankings import current_rankings
    rankings = current_rankings(config) if found else {}
    return [dict(value['simulation_params'], dps=found[key], ranking=rankings.get((config.pk, key)),
                 config_id=config.pk, signature=key)
            for key, value in signatures.items() if key in found]
