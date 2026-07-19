#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: SimcMonitor.py
@time: 2025/1/15 10:00
@desc: SimC模拟监控

'''

import os
import subprocess
import hashlib
import time
import json
import re
import platform as py_platform
from dataclasses import asdict, is_dataclass
from django.conf import settings
from django.db import models, transaction
from django.utils import timezone
from utils.log import logger
from botend.models import SimcTask, SimcTaskBatch, SimcProfile, SimcBackendBinary
from botend.alerting import upsert_system_alert
from botend.controller.BaseScan import BaseScan
from botend.services.simc_player_config import (
    EQUIPMENT_SLOT_ALIASES,
    authoritative_player_baseline,
    validate_player_baseline,
)
from botend.services.simc_composer import SimcComposer
from botend.services.task_resolver import resolve_task, is_reference_task, TaskResolutionError
from botend.models import SimulationRun


class SimcMonitor(BaseScan):
    """
    SimC模拟监控
    """

    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task
        self.hint = ""
        
        # SimC 只支持服务器本地源码编译产物；旧 nightly/上传二进制链路已移除。
        self.simc_config = getattr(settings, 'SIMC_CONFIG', {}) or {}
        self.simc_source_dir, self.simc_build_dir, self.simc_path = self._resolve_local_build_paths()
        self.result_path = os.path.join(os.getcwd(), self.simc_config.get('result_path', 'static/simc_results/'))
        self.simc_template_path = self.simc_config.get('simc_template', 'LMonitor/simc_template.txt')
        
        # 确保结果目录存在
        if not os.path.exists(self.result_path):
            os.makedirs(self.result_path, exist_ok=True)

    def _get_runtime_platform(self):
        sys_name = str(py_platform.system() or '').lower()
        if 'linux' in sys_name:
            machine = str(py_platform.machine() or '').lower()
            if machine in ('aarch64', 'arm64'):
                return 'linuxarm64'
            return 'linux64'
        return 'unsupported'

    def _resolve_local_build_paths(self):
        """Resolve the only supported SimC backend path: local source checkout + Linux build output."""
        cfg = getattr(settings, 'SIMC_CONFIG', {}) or {}
        source_dir = str(cfg.get('simc_source_dir') or '/home/lighthouse/simc').rstrip('/')
        build_dir = str(cfg.get('simc_build_dir') or os.path.join(source_dir, 'build-cli')).rstrip('/')
        binary_path = str(cfg.get('simc_path') or os.path.join(build_dir, 'simc'))
        return source_dir, build_dir, binary_path

    def _get_backend_row(self):
        platform = self._get_runtime_platform()
        row, _ = SimcBackendBinary.objects.get_or_create(
            platform=platform,
            defaults={
                'simc_path': self.simc_path,
                'current_version': '',
                'latest_version': '',
                'auto_update': True,
                'update_status': '未检查',
            }
        )
        return row

    def _set_update_status(self, row, status=None, progress=None, is_updating=None, latest_version=None, last_error=None, current_version=None, updated=False):
        try:
            fields = []
            if status is not None:
                row.update_status = str(status)[:255]
                fields.append('update_status')
            if progress is not None:
                row.update_progress = max(0, min(100, int(progress)))
                fields.append('update_progress')
            if is_updating is not None:
                row.is_updating = bool(is_updating)
                fields.append('is_updating')
            if latest_version is not None:
                row.latest_version = str(latest_version)[:128]
                fields.append('latest_version')
            if current_version is not None:
                row.current_version = str(current_version)[:128]
                fields.append('current_version')
            if last_error is not None:
                row.last_error = str(last_error)[:500]
                fields.append('last_error')
            row.last_checked_at = timezone.now()
            fields.append('last_checked_at')
            if updated:
                row.last_updated_at = timezone.now()
                fields.append('last_updated_at')
            if fields:
                row.save(update_fields=list(dict.fromkeys(fields)))
        except Exception as e:
            logger.warning(f"[SimC Monitor] Failed to update SimC backend state: {e}")

    def _git_output(self, args, cwd, timeout=30):
        result = subprocess.run(['git'] + list(args), cwd=cwd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            raise Exception((result.stderr or result.stdout or '').strip() or f"git {' '.join(args)} failed")
        return (result.stdout or '').strip()

    def _get_git_hash(self, ref='HEAD'):
        source_dir = self.simc_source_dir
        try:
            return self._git_output(['rev-parse', '--short', ref], source_dir, timeout=10)
        except Exception:
            return ''

    def _get_git_upstream_hash(self):
        source_dir = self.simc_source_dir
        try:
            self._git_output(['fetch', '--quiet'], source_dir, timeout=120)
            return self._git_output(['rev-parse', '--short', '@{u}'], source_dir, timeout=10)
        except Exception as e:
            logger.warning(f"[SimC Monitor] Failed to check SimC upstream git hash: {e}")
            return ''

    def _validate_local_simc_binary(self):
        if not self.simc_path:
            return False, 'SimC路径未配置'
        if not os.path.isfile(self.simc_path):
            return False, f'SimC可执行文件不存在: {self.simc_path}'
        if not os.access(self.simc_path, os.X_OK):
            try:
                st = os.stat(self.simc_path)
                os.chmod(self.simc_path, st.st_mode | 0o111)
            except Exception:
                return False, f'SimC文件不可执行: {self.simc_path}'
        try:
            result = subprocess.run([self.simc_path], capture_output=True, text=True, timeout=10)
            output = (result.stdout or '') + (result.stderr or '')
            if 'SimulationCraft' not in output:
                return False, f'SimC二进制验证失败: {output[:300]}'
        except Exception as e:
            return False, f'SimC二进制验证异常: {e}'
        return True, ''

    def ensure_local_simc_backend_current(self):
        """
        Only supported update path: check local SimulationCraft git checkout and compile with update_simc_binary.
        The old nightly-package download/install chain has been removed.
        """
        row = self._get_backend_row()
        if row.simc_path != self.simc_path:
            row.simc_path = self.simc_path
            row.save(update_fields=['simc_path'])

        ok, binary_error = self._validate_local_simc_binary()
        interval = int(self.simc_config.get('update_check_interval_seconds', 1800) or 1800)
        now = timezone.now()
        if ok and row.last_checked_at and (now - row.last_checked_at).total_seconds() < interval:
            self._set_update_status(row, status=row.update_status or '本地 SimC 可用', progress=100, is_updating=False, last_error='')
            return True

        if row.is_updating:
            logger.info('[SimC Monitor] SimC backend update is already running, skip auto check')
            return ok

        current_hash = self._get_git_hash('HEAD')
        upstream_hash = self._get_git_upstream_hash()
        latest_version = upstream_hash or current_hash
        need_update = (not ok) or (bool(upstream_hash and current_hash and upstream_hash != current_hash))

        if need_update:
            if not row.auto_update:
                msg = binary_error if not ok else f'检测到 SimC 源码更新 {current_hash} -> {upstream_hash}，但自动更新关闭'
                self._set_update_status(row, status='需要更新', progress=0, is_updating=False, latest_version=latest_version, last_error=msg)
                if not ok:
                    return False
                return True
            try:
                logger.info(f"[SimC Monitor] Auto updating local SimC backend: current={current_hash}, upstream={upstream_hash}, binary_ok={ok}")
                from django.core.management import call_command
                self._set_update_status(row, status='自动编译更新 SimC', progress=1, is_updating=True, latest_version=latest_version, last_error='')
                call_command('update_simc_binary', threads=int(self.simc_config.get('compile_threads', 2) or 2), no_pull=False, check=False)
                ok, binary_error = self._validate_local_simc_binary()
                if not ok:
                    self._set_update_status(row, status='自动编译后验证失败', progress=0, is_updating=False, last_error=binary_error)
                    return False
                row = self._get_backend_row()
                row.simc_path = self.simc_path
                row.save(update_fields=['simc_path'])
                return True
            except Exception as e:
                err = str(e)
                if ok:
                    self._set_update_status(
                        row,
                        status='自动更新失败，继续使用现有 SimC 二进制',
                        progress=100,
                        is_updating=False,
                        last_error=err,
                    )
                    upsert_system_alert(
                        'SIMC_UPDATE_FAILED', self._get_runtime_platform(), 3,
                        'SimC 自动更新失败，已回退到现有二进制', err,
                    )
                    logger.error(f"[SimC Monitor] Auto update failed; keeping usable local SimC binary: {err}")
                    return True
                self._set_update_status(row, status='自动编译失败', progress=0, is_updating=False, last_error=err)
                upsert_system_alert('SIMC_UPDATE_FAILED', self._get_runtime_platform(), 3, 'SimC 自动更新失败', err)
                logger.error(f"[SimC Monitor] Auto update local SimC backend failed: {err}")
                return False

        status = f'本地 SimC 已是最新 {current_hash}' if current_hash else '本地 SimC 可用'
        self._set_update_status(row, status=status, progress=100, is_updating=False, latest_version=latest_version, current_version=current_hash or row.current_version, last_error='')
        return True

    def sync_batch_lifecycle(self, batch_id):
        """Recompute one real Batch strictly from its FK-owned task statuses."""
        if not batch_id:
            return
        try:
            with transaction.atomic():
                batch = SimcTaskBatch.objects.select_for_update().filter(id=batch_id).first()
                if batch is None:
                    return
                statuses = list(
                    SimcTask.objects.filter(batch_id=batch.id, is_active=True)
                    .values_list('current_status', flat=True)
                )
                if not statuses:
                    resolved_status = 0
                elif all(status in (2, 3) for status in statuses):
                    resolved_status = 3 if any(status == 3 for status in statuses) else 2
                else:
                    resolved_status = 1

                update_fields = []
                if batch.status != resolved_status:
                    batch.status = resolved_status
                    update_fields.append('status')
                if resolved_status in (2, 3):
                    if batch.completed_at is None:
                        batch.completed_at = timezone.now()
                        update_fields.append('completed_at')
                elif batch.completed_at is not None:
                    batch.completed_at = None
                    update_fields.append('completed_at')
                if update_fields:
                    update_fields.append('updated_at')
                    batch.save(update_fields=update_fields)
        except Exception as exc:
            logger.error(f"[SimC Monitor] Failed to sync batch {batch_id} lifecycle: {exc}")

    def reconcile_open_batches(self):
        """Retry lifecycle reconciliation for non-terminal real batches."""
        for batch_id in SimcTaskBatch.objects.filter(
            is_active=True,
            status__in=(0, 1),
        ).values_list('id', flat=True):
            self.sync_batch_lifecycle(batch_id)

    def mark_task_failed(self, simc_task, reason, exc=None, overwrite_when_has_error=False):
        """
        将任务标记为失败，并写入可见错误信息。
        """
        try:
            detail = str(reason or "未知错误").strip()
            if exc is not None:
                detail = f"{detail}\n异常信息: {str(exc)}"

            if exc is not None:
                self.save_simc_error_details(
                    simc_task,
                    summary=str(reason or "任务失败").strip(),
                    stderr_text=str(exc)
                )

            simc_task.current_status = 3
            simc_task.error_detail = detail
            simc_task.save()
            self.sync_batch_lifecycle(simc_task.batch_id)
        except Exception as save_err:
            logger.error(f"[SimC Monitor] Failed to persist task error for task {getattr(simc_task, 'id', '-')}: {save_err}")

    def clear_simc_error_details(self, simc_task):
        """
        清理任务中的历史SimC错误详情，避免重跑后显示旧错误。
        """
        try:
            payload = self.parse_task_ext(simc_task.ext)
            if not isinstance(payload, dict) or not payload:
                return
            changed = False
            for key in ('simc_error_summary', 'simc_error_native', 'simc_error_code'):
                if key in payload:
                    payload.pop(key, None)
                    changed = True
            if changed:
                simc_task.ext = json.dumps(payload, ensure_ascii=False) if payload else ''
        except Exception as e:
            logger.warning(f"[SimC Monitor] Failed to clear simc error details for task {getattr(simc_task, 'id', '-')}: {e}")

    def save_simc_error_details(self, simc_task, summary, return_code=None, stderr_text=None, stdout_text=None):
        """
        保存缩略错误 + 原生错误到任务ext，便于前端日志查看。
        """
        try:
            payload = self.parse_task_ext(simc_task.ext)
            if not isinstance(payload, dict):
                payload = {}

            payload['simc_error_summary'] = str(summary or '').strip()[:800]
            if return_code is not None:
                try:
                    payload['simc_error_code'] = int(return_code)
                except Exception:
                    payload['simc_error_code'] = str(return_code)

            native_parts = []
            if return_code is not None:
                native_parts.append(f"returncode: {return_code}")
            if stderr_text:
                native_parts.append("stderr:")
                native_parts.append(str(stderr_text))
            if stdout_text:
                native_parts.append("stdout:")
                native_parts.append(str(stdout_text))
            native_text = '\n'.join(native_parts).strip()
            if native_text:
                payload['simc_error_native'] = native_text

            serialized = json.dumps(payload, ensure_ascii=False)
            # Preserve the runnable task manifest. Error logging must never replace
            # player_config_mode/batch_compare with a summary, otherwise a failed
            # batch task cannot be inspected or retried.
            if len(serialized) > 4800:
                native_value = str(payload.get('simc_error_native') or '')
                available = max(0, 4600 - len(json.dumps({
                    key: value for key, value in payload.items()
                    if key != 'simc_error_native'
                }, ensure_ascii=False)))
                if available > 0 and native_value:
                    suffix = '\n...(原生错误已截断)'
                    payload['simc_error_native'] = native_value[:max(0, available - len(suffix))] + suffix
                else:
                    payload.pop('simc_error_native', None)
                serialized = json.dumps(payload, ensure_ascii=False)
            # ext is a TextField, so retain a compact native tail rather than
            # discarding the original execution context at an arbitrary 5 KB limit.
            simc_task.ext = serialized
        except Exception as e:
            logger.warning(f"[SimC Monitor] Failed to save native error details for task {getattr(simc_task, 'id', '-')}: {e}")

    def fail_pending_tasks(self, reason):
        """
        当运行前置条件不满足时，为待执行任务写入失败原因，避免前端无日志可看。
        """
        try:
            pending_tasks = SimcTask.objects.filter(is_active=True, current_status=0)
            for task in pending_tasks:
                self.mark_task_failed(task, reason)
        except Exception as e:
            logger.error(f"[SimC Monitor] Failed to mark pending tasks as failed: {e}")

    def scan(self, url=None):
        """
        执行SimC模拟扫描
        :param url: 可选参数，这里不使用
        :return:
        """
        logger.info("[SimC Monitor] Start SimC simulation check.")
        
        try:
            self.reconcile_open_batches()
            if not self.ensure_local_simc_backend_current():
                logger.error("[SimC Monitor] Local SimC backend is not ready")
                self.fail_pending_tasks("SimC本地编译产物不可用，请先完成后端编译更新")
                return False

            # 检查SimC路径是否正确
            if not self.simc_path:
                logger.error("[SimC Monitor] SimC path not configured")
                self.fail_pending_tasks("SimC路径未配置，请检查系统配置")
                return False
            if not os.path.exists(self.simc_path):
                logger.error(f"[SimC Monitor] SimC executable not found at path: {self.simc_path}")
                self.fail_pending_tasks(f"SimC可执行文件不存在: {self.simc_path}")
                return False
            if not os.path.isfile(self.simc_path):
                logger.error(f"[SimC Monitor] SimC path is not a file: {self.simc_path}")
                self.fail_pending_tasks(f"SimC路径不是文件: {self.simc_path}")
                return False

            # A SimC batch is one user action and must be drained during this dispatch.
            # Processing only one candidate makes global MonitorTask scheduling latency
            # multiply by the number of candidates and leaves a "completed" report partial.
            first_task = SimcTask.objects.filter(
                is_active=True,
                current_status=0,
            ).order_by('modified_time', 'id').first()
            if not first_task:
                logger.info("[SimC Monitor] No pending SimC task.")
                return True

            # Batch membership is authoritative only through the database FK. Legacy
            # ext.batch_compare.batch_id strings are intentionally not used for dispatch.
            pending_tasks = [first_task]
            if first_task.batch_id:
                pending_tasks = list(SimcTask.objects.filter(
                    is_active=True,
                    current_status=0,
                    batch_id=first_task.batch_id,
                ).order_by('modified_time', 'id'))

            for simc_task in pending_tasks:
                logger.info(f"[SimC Monitor] Processing task: {simc_task.name} (ID: {simc_task.id})")
                self.process_simc_task(simc_task)
                
        except Exception as e:
            logger.error(f"[SimC Monitor] Error during SimC simulation: {str(e)}")
            self.fail_pending_tasks("SimC调度进程异常，请稍后重试")
            return False
            
        return True

    @staticmethod
    def is_reference_task(simc_task):
        """
        检查任务是否是完整引用型任务（新架构）。
        必须同时具有所有六个引用字段。
        """
        return is_reference_task(simc_task)

    @staticmethod
    def apply_candidate_overrides(composer_request, mode_params):
        """Apply one Batch candidate to an in-memory Composer request.

        Immutable resource payloads remain untouched; only this execution request
        is changed before composition.
        """
        request_data = dict(composer_request or {})
        params = mode_params or {}
        candidate_type = params.get('candidate_type') or 'base'

        if candidate_type == 'gear_swap':
            swap = params.get('gear_swap') or {}
            slot = str(swap.get('slot') or '').strip().lower()
            raw_value = str(swap.get('raw_value') or '').strip()
            if not slot or not raw_value:
                raise ValueError('装备候选缺少 slot 或 raw_value')
            lines = []
            replaced = False
            in_candidate_section = False
            for line in str(request_data.get('player_equipment') or '').splitlines():
                stripped = line.strip()
                if stripped.startswith('###'):
                    in_candidate_section = True
                current = line.partition('=')[0].strip().lower()
                canonical = EQUIPMENT_SLOT_ALIASES.get(current, current)
                if canonical == slot and not replaced and not in_candidate_section:
                    lines.append(f'{current}={raw_value}')
                    replaced = True
                else:
                    lines.append(line)
            if not replaced:
                raise ValueError(f'基准玩家块未包含可替换的装备槽位: {slot}')
            request_data['player_equipment'] = '\n'.join(lines)

        elif candidate_type == 'talent_override':
            talent = str(params.get('talent_override') or '').strip()
            if not talent:
                raise ValueError('天赋候选缺少构筑码')
            lines = []
            replaced = False
            for line in str(request_data.get('player_equipment') or '').splitlines():
                key = line.partition('=')[0].strip().lower()
                if key in ('talent', 'talents') and not replaced:
                    lines.append(f'talents={talent}')
                    replaced = True
                else:
                    lines.append(line)
            if not replaced:
                raise ValueError('基准玩家块未包含 talents 行，无法应用天赋候选')
            request_data['player_equipment'] = '\n'.join(lines)
            request_data['talent'] = talent

        elif candidate_type == 'apl_override':
            request_data['override_action_list'] = str(params.get('apl_override') or '')

        elif candidate_type == 'attribute_ratings':
            ratings = params.get('attribute_ratings') or {}
            for stat in ('crit', 'haste', 'mastery', 'versatility'):
                if stat not in ratings:
                    raise ValueError(f'属性候选缺少 {stat}')
                request_data[f'gear_{stat}'] = int(ratings[stat])

        elif candidate_type != 'base':
            raise ValueError(f'不支持的候选类型: {candidate_type}')

        return request_data

    def process_reference_task(self, simc_task):
        """
        Process reference-based task with immutable version snapshots.

        Contract:
        1. Only accepts complete 6-reference tasks
        2. Calls task_resolver.resolve_task for version payloads
        3. Uses SimcComposer.compose() to generate SimC input
        4. Creates SimulationRun with input_hash and metadata
        5. Uses existing execute_simc_command
        """
        run = None
        try:
            # Validate complete 6-reference task
            if not is_reference_task(simc_task):
                self.mark_task_failed(
                    simc_task,
                    "引用型任务必须包含完整的六个引用字段",
                    Exception("任务缺少 profile/template/apl 的 FK 或 version FK")
                )
                return False

            # Allocate under a lock on the parent row. Locking an empty child
            # queryset does not serialize first-run allocation.
            with transaction.atomic():
                SimcTask.objects.select_for_update().get(pk=simc_task.pk)
                max_sequence = SimulationRun.objects.filter(task=simc_task).aggregate(
                    value=models.Max('sequence')
                )['value'] or 0
                run = SimulationRun.objects.create(
                    task=simc_task,
                    sequence=max_sequence + 1,
                    candidate_label=simc_task.candidate_label or '',
                    status='running',
                    input_hash='',
                    resource_manifest={},
                    started_at=timezone.now(),
                )

            # Resolve task to version payloads
            try:
                resolved = resolve_task(simc_task)
            except TaskResolutionError as e:
                run.status = 'failed'
                run.error_detail = str(e)
                run.completed_at = timezone.now()
                run.save(update_fields=['status', 'error_detail', 'completed_at'])
                self.mark_task_failed(simc_task, "任务引用解析失败", e)
                return False

            # Extract profile spec from resource metadata
            profile_metadata = resolved.resource_metadata.get('profile', {})
            profile_payload = resolved.profile_payload
            profile_spec = profile_metadata.get('spec', 'fury')

            # Build Composer input exclusively from immutable version payloads.
            composer_request = {
                'spec': resolved.simulation_params.get('spec') or profile_spec,
                'fight_style': resolved.simulation_params.get('fight_style', 'Patchwerk'),
                'time': resolved.simulation_params.get('max_time', 300),
                'target_count': resolved.simulation_params.get('desired_targets', 1),
                'iterations': resolved.simulation_params.get('iterations', 10000),
                'target_error': resolved.simulation_params.get('target_error'),
                'vary_combat_length': resolved.simulation_params.get('vary_combat_length'),
                'enemy_type': resolved.simulation_params.get('enemy_type'),
                'player_import_mode': profile_payload.get('player_config_mode', ''),
                'player_equipment': profile_payload.get('player_equipment', ''),
                'battlenet_region': profile_payload.get('battlenet_region', ''),
                'battlenet_realm': profile_payload.get('battlenet_realm', ''),
                'battlenet_character': profile_payload.get('battlenet_character', ''),
                'talent': profile_payload.get('talent', ''),
                'gear_strength': profile_payload.get('gear_strength'),
                'gear_crit': profile_payload.get('gear_crit'),
                'gear_haste': profile_payload.get('gear_haste'),
                'gear_mastery': profile_payload.get('gear_mastery'),
                'gear_versatility': profile_payload.get('gear_versatility'),
                'base_template_content': resolved.template_content or '',
                'override_action_list': resolved.apl_content or '',
                '_result_file_path': simc_task.result_file or f'{simc_task.id}.html',
            }
            composer_request = self.apply_candidate_overrides(
                composer_request,
                resolved.mode_params,
            )
            # Compose final SimC input using SimcComposer
            composer = SimcComposer(simc_task.user_id)
            simc_code, composition_manifest, compose_error = composer.compose(composer_request)
            serializable_manifest = (
                asdict(composition_manifest)
                if is_dataclass(composition_manifest)
                else (composition_manifest or {})
            )
            run.resource_manifest = {
                **(resolved.resource_metadata or {}),
                'composition_manifest': serializable_manifest,
            }
            run.save(update_fields=['resource_manifest'])

            if compose_error or simc_code is None:
                # Compose failed - mark run as failed
                run.status = 'failed'
                run.error_detail = compose_error or 'SimC composition failed'
                run.completed_at = timezone.now()
                run.save(update_fields=['status', 'error_detail', 'completed_at'])

                self.mark_task_failed(simc_task, "SimC 组合失败", Exception(compose_error or 'Composition returned None'))
                return False

            # Compute input hash
            input_hash = hashlib.sha256(simc_code.encode('utf-8')).hexdigest()
            run.input_hash = input_hash
            run.save(update_fields=['input_hash'])

            # Write temporary SimC file
            result_file = simc_task.result_file or f'{simc_task.id}.html'
            simc_file_path = os.path.join(self.result_path, f'temp_{simc_task.id}_run_{run.id}.simc')

            try:
                with open(simc_file_path, 'w', encoding='utf-8') as f:
                    f.write(simc_code)

                # Execute SimC using existing command
                self._active_run = run
                success = self.execute_simc_command(simc_file_path, simc_task, result_file)

                # Update THIS run based on execution result
                if success:
                    simc_task.refresh_from_db()
                    ext_payload = self.parse_task_ext(simc_task.ext)
                    result_summary = ext_payload.get('semantic_validation') or {}
                    run.status = 'completed'
                    run.result_summary = result_summary
                    run.completed_at = timezone.now()
                    run.save(update_fields=['status', 'result_summary', 'completed_at'])
                    # Mark task as completed and expose the same semantic summary.
                    simc_task.current_status = 2
                    simc_task.result_summary = json.dumps(result_summary, ensure_ascii=False)
                    simc_task.error_detail = None
                    simc_task.completed_at = timezone.now()
                    simc_task.save(update_fields=['current_status', 'result_summary', 'error_detail', 'completed_at'])
                    return True
                else:
                    simc_task.refresh_from_db()
                    detail = str(simc_task.error_detail or '').strip()
                    if not detail:
                        ext_payload = self.parse_task_ext(simc_task.ext)
                        detail = str(ext_payload.get('simc_error_summary') or 'SimC execution failed')
                        simc_task.error_detail = detail
                        simc_task.save(update_fields=['error_detail'])
                    run.status = 'failed'
                    run.error_detail = detail
                    run.completed_at = timezone.now()
                    run.save(update_fields=['status', 'error_detail', 'completed_at'])

                    # Ensure task is marked as failed
                    simc_task.refresh_from_db()
                    if simc_task.current_status != 3:
                        simc_task.current_status = 3
                        simc_task.save(update_fields=['current_status'])
                    return False

            finally:
                self._active_run = None
                # Clean up temporary file
                if os.path.exists(simc_file_path):
                    os.remove(simc_file_path)

        except Exception as e:
            logger.error(f"[SimC Monitor] Error processing reference task {simc_task.id}: {str(e)}")

            # Only update THIS run if it was created in this execution
            if run is not None:
                try:
                    run.refresh_from_db()
                    if run.status != 'completed':
                        run.status = 'failed'
                        run.error_detail = str(e)
                        run.completed_at = timezone.now()
                        run.save(update_fields=['status', 'error_detail', 'completed_at'])
                except Exception:
                    pass

            self.mark_task_failed(simc_task, "引用型任务处理异常", e)
            return False

    def process_simc_task(self, simc_task, already_claimed=False):
        """
        处理单个SimC任务。
        ``already_claimed`` 供独立 Worker 使用：Worker 负责原子领取，Monitor 只负责执行。
        """
        try:
            if not already_claimed:
                claimed_at = timezone.now()
                claimed = SimcTask.objects.filter(
                    id=simc_task.id,
                    is_active=True,
                    current_status=0
                ).update(
                    current_status=1,
                    started_at=claimed_at,
                    completed_at=None,
                    modified_time=claimed_at,
                )
                if claimed != 1:
                    logger.info(f"[SimC Monitor] Task {simc_task.id} was already claimed or no longer pending, skip")
                    return False
            simc_task.refresh_from_db()
            self.sync_batch_lifecycle(simc_task.batch_id)
            self.clear_simc_error_details(simc_task)
            simc_task.save(update_fields=['ext', 'modified_time'])

            # 引用型任务：新架构，动态 Composer + SimulationRun
            if self.is_reference_task(simc_task):
                return self.process_reference_task(simc_task)

            # ── All non-reference tasks rejected ──
            self.mark_task_failed(
                simc_task,
                '只支持完整引用型任务',
                Exception('任务必须包含完整的六个引用字段（profile/template/apl + versions）')
            )
            return False

        except Exception as e:
            logger.error(f"[SimC Monitor] Error processing task {simc_task.id}: {str(e)}")
            self.mark_task_failed(simc_task, "任务处理失败", e)
            return False
        finally:
            self.sync_batch_lifecycle(getattr(simc_task, 'batch_id', None))

        return True

    def build_attribute_test_points(self, total_value, base_value, requested_step):
        """Build an exact requested-step two-stat curve, retaining endpoints and baseline.

        Attribute optimization promises a fixed rating step.  Do not silently resample
        a dense curve into arbitrary values: that makes both the displayed candidate
        points and the optimization conclusion untruthful.
        """
        try:
            total = max(0, int(total_value))
            baseline = min(total, max(0, int(base_value)))
            step = max(1, int(requested_step))
        except (TypeError, ValueError):
            return [0]
        points = list(range(0, total + 1, step))
        if points[-1] != total:
            points.append(total)
        return sorted(set(points + [baseline]) )

    def ensure_regular_result_file(self, simc_task):
        """Allocate the one report filename a regular SimC task is allowed to use."""
        current = str(simc_task.result_file or '').strip()
        if current.endswith('.html') and '\n' not in current and '/' not in current and '\\' not in current:
            return current
        result_file = f'simc_task_{simc_task.id}.html'
        simc_task.result_file = result_file
        simc_task.save(update_fields=['result_file', 'modified_time'])
        return result_file

    @staticmethod
    def ensure_result_file_directive(simc_code, result_file):
        """Render exactly one task-owned HTML output directive into SimC input."""
        result_file = str(result_file or '').strip()
        if not result_file.endswith('.html') or '/' in result_file or '\\' in result_file:
            raise ValueError('SimC任务结果文件名无效')
        code = str(simc_code or '')
        code = code.replace('{result_file}', result_file)
        lines = [line for line in code.splitlines() if not re.match(r'^\s*html\s*=', line, re.IGNORECASE)]
        lines.append(f'html={result_file}')
        return '\n'.join(lines).strip()

    @staticmethod
    def validate_simulation_semantics(stdout_text):
        """Reject reports that technically finish but never execute a real rotation."""
        text = str(stdout_text or '')
        dps_match = re.search(r'\bDPS=([0-9]+(?:\.[0-9]+)?)', text)
        action_rows = re.findall(
            r'^\s{2,}([a-z][a-z0-9_]*)\s+Count=.*?\bpDPS=\s*([0-9]+)',
            text,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        ignored = ('auto_attack', 'charge_impact', 'potion', 'blood_fury')
        non_auto_dps = sum(
            int(value) for name, value in action_rows
            if not name.lower().startswith(ignored) and int(value) > 0
        )
        valid = bool(dps_match and non_auto_dps > 0)
        declared_action_lists = set(re.findall(
            r'^\s*Priorities \(actions\.([a-z][a-z0-9_]*)\):\s*$',
            text,
            flags=re.IGNORECASE | re.MULTILINE,
        ))
        default_priority_match = re.search(
            r'^\s*Priorities \(actions\.default\):\s*$([\s\S]*?)(?=^\s*Priorities \(actions\.|^\s*Actions:|\Z)',
            text,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        default_priority_text = default_priority_match.group(1) if default_priority_match else ''
        talent_dispatch_lists = []
        for action_list, condition in re.findall(
            r'run_action_list,name=([a-z][a-z0-9_]*),if=([^\n]+)',
            default_priority_text,
            flags=re.IGNORECASE,
        ):
            if 'talent.' in condition.lower() and action_list.lower() not in talent_dispatch_lists:
                talent_dispatch_lists.append(action_list.lower())
        active_talent_dispatch_lists = [
            name for name in talent_dispatch_lists
            if name in {value.lower() for value in declared_action_lists}
        ]
        unresolved_action_lists = [
            name for name in talent_dispatch_lists
            if name not in {value.lower() for value in declared_action_lists}
        ]
        failure_type = ''
        reason = ''
        if not valid:
            if talent_dispatch_lists and not active_talent_dispatch_lists:
                failure_type = 'talent_apl_dispatch'
                reason = (
                    'SimC结果语义无效：英雄天赋未进入任何有效 APL 分流；'
                    '当前天赋码可能与 SimC 天赋树版本不兼容。未激活列表: '
                    + ', '.join(unresolved_action_lists)
                )
            else:
                failure_type = 'auto_attack_only'
                reason = 'SimC结果语义无效：只有自动攻击，未执行有效技能循环'
        return {
            'valid': valid,
            'dps': float(dps_match.group(1)) if dps_match else 0.0,
            'non_auto_dps': non_auto_dps,
            'action_row_count': len(action_rows),
            'failure_type': failure_type,
            'unresolved_action_lists': unresolved_action_lists,
            'reason': reason,
        }

    @staticmethod
    def persist_semantic_validation(simc_task, validation):
        try:
            manifest = json.loads(simc_task.ext) if simc_task.ext else {}
        except (TypeError, ValueError):
            manifest = {}
        if not isinstance(manifest, dict):
            manifest = {}
        manifest['semantic_validation'] = validation
        simc_task.ext = json.dumps(manifest, ensure_ascii=False)
        simc_task.result_summary = json.dumps(validation, ensure_ascii=False)
        simc_task.save(update_fields=['ext', 'result_summary'])

    def execute_simc_command(self, simc_file_path, simc_task, result_file_name=None, run=None):
        """
        执行SimC命令
        :param simc_file_path: SimC文件路径
        :param simc_task: SimcTask对象
        :param result_file_name: 自定义结果文件名（可选）
        :return: 执行是否成功
        """
        try:
            
            # SimC resolves an `html=` inside an absolute input profile relative to that
            # profile, not `cwd`. Force the task-owned absolute output path on the
            # command line; this overrides the profile directive and is the exact file
            # verified below.
            active_run = run or getattr(self, '_active_run', None)
            if active_run is not None:
                from botend.services.simc_artifacts import result_filename_for_run
                target_result_file = result_filename_for_run(simc_task, active_run) or ''
            else:
                target_result_file = str(result_file_name or simc_task.result_file or '').strip()
            if (
                not target_result_file
                or not target_result_file.endswith('.html')
                or os.path.basename(target_result_file) != target_result_file
                or '/' in target_result_file
                or '\\' in target_result_file
                or '\n' in target_result_file
            ):
                raise RuntimeError('SimC任务未配置唯一 HTML 结果文件')
            result_file_path = os.path.join(self.result_path, target_result_file)
            if os.path.isfile(result_file_path):
                os.remove(result_file_path)
            cmd = [self.simc_path, simc_file_path, f'html={result_file_path}']
            
            logger.info(f"[SimC Monitor] Executing command: {' '.join(cmd)}")
            
            # 执行命令
            result = subprocess.run(
                cmd,
                cwd=self.result_path,
                capture_output=True,
                text=True,
                timeout=300  # 5分钟超时
            )
            
            if result.returncode == 0:
                logger.info(f"[SimC Monitor] SimC execution successful for task {simc_task.id}")
                if result.stdout:
                    logger.debug(f"[SimC Monitor] SimC output: {result.stdout[:500]}...")  # 只记录前500字符
                
                # A successful SimC process is not a successful task without this
                # task's explicitly requested report. Never borrow a stale/latest HTML.
                if not os.path.isfile(result_file_path):
                    raise RuntimeError(f'SimC未生成预期结果文件: {target_result_file}')

                semantic_validation = self.validate_simulation_semantics(result.stdout)
                self.persist_semantic_validation(simc_task, semantic_validation)
                if not semantic_validation['valid']:
                    summary = semantic_validation.get('reason') or 'SimC结果语义校验失败'
                    self.save_simc_error_details(simc_task, summary, stdout_text=result.stdout)
                    logger.error('[SimC Monitor] Task %s semantic validation failed: %s', simc_task.id, summary)
                    return False

                if target_result_file != simc_task.result_file:
                    simc_task.result_file = target_result_file
                    simc_task.save(update_fields=['result_file', 'modified_time'])
                if isinstance(simc_task, SimcTask):
                    from botend.services.simc_artifacts import upsert_task_html_artifact
                    artifact = upsert_task_html_artifact(
                        simc_task,
                        target_result_file,
                        run=active_run,
                    )
                    if artifact is None:
                        raise RuntimeError('SimC结果文件未通过任务产物安全校验')
                try:
                    from botend.interface.ossupload import ossUpload
                    upload_success = ossUpload(result_file_path)
                    if upload_success:
                        logger.info(f"[SimC Monitor] Result file {target_result_file} uploaded to OSS successfully for task {simc_task.id}")
                    else:
                        logger.error(f"[SimC Monitor] Failed to upload result file {target_result_file} to OSS for task {simc_task.id}")
                except Exception as e:
                    logger.error(f"[SimC Monitor] Error uploading result file to OSS: {str(e)}")

                return True
            else:
                logger.error(f"[SimC Monitor] SimC execution failed for task {simc_task.id}")
                logger.error(f"[SimC Monitor] Return code: {result.returncode}")
                
                # Error details belong to error_detail/ext. result_file remains a
                # task-owned HTML filename so retries and diagnostics stay valid.
                error_info = f"SimC执行失败\n返回码: {result.returncode}\n"
                if result.stderr:
                    logger.error(f"[SimC Monitor] Error output: {result.stderr}")
                    error_info += f"错误输出: {result.stderr}\n"
                if result.stdout:
                    error_info += f"标准输出: {result.stdout}\n"
                
                simc_task.error_detail = error_info
                self.save_simc_error_details(
                    simc_task,
                    summary=f"SimC执行失败（返回码: {result.returncode}）",
                    return_code=result.returncode,
                    stderr_text=result.stderr,
                    stdout_text=result.stdout
                )
                simc_task.save()
                return False
                
        except subprocess.TimeoutExpired:
            error_info = f"SimC执行超时\n任务ID: {simc_task.id}\n超时时间: 300秒"
            logger.error(f"[SimC Monitor] SimC execution timeout for task {simc_task.id}")
            simc_task.error_detail = error_info
            self.save_simc_error_details(
                simc_task,
                summary="SimC执行超时（300秒）"
            )
            simc_task.save()
            return False
        except Exception as e:
            error_info = f"SimC执行异常\n任务ID: {simc_task.id}\n异常信息: {str(e)}"
            logger.error(f"[SimC Monitor] Error executing SimC command: {str(e)}")
            simc_task.error_detail = error_info
            self.save_simc_error_details(
                simc_task,
                summary="SimC执行异常",
                stderr_text=str(e)
            )
            simc_task.save()
            return False


    def check_status(self, result):
        """
        检查请求状态
        :param result: 请求结果
        :return: 状态检查结果
        """
        return True

    def resolve_data(self, result):
        """
        处理返回的内容
        :param result: 返回内容
        :return: 处理结果
        """
        return True

    def trigger_webhook(self):
        """
        触发webhook
        :return: 触发结果
        """
        return True
    
    def parse_selected_attributes(self, ext_data):
        """
        解析选中的属性组合
        :param ext_data: 扩展信息字符串，单个属性组合名称（如'crit_versatility'）
        :return: 选中的属性列表
        """
        try:
            if not ext_data:
                return []
            
            # ext_data是单个属性组合字符串，如"crit_versatility"
            combination = ext_data.strip()
            
            # 属性组合映射
            combination_map = {
                'crit_mastery': ['gear_crit', 'gear_mastery'],
                'crit_haste': ['gear_crit', 'gear_haste'],
                'crit_versatility': ['gear_crit', 'gear_versatility'],
                'mastery_haste': ['gear_mastery', 'gear_haste'],
                'mastery_versatility': ['gear_mastery', 'gear_versatility'],
                'haste_versatility': ['gear_haste', 'gear_versatility'],
                'haste_mastery': ['gear_haste', 'gear_mastery']
            }
            
            # 获取选中的属性
            if combination in combination_map:
                return combination_map[combination]
            else:
                logger.warning(f"[SimC Monitor] Unknown attribute combination: {combination}")
                return []
            
        except Exception as e:
            logger.error(f"[SimC Monitor] Error parsing selected attributes: {str(e)}")
            return []

    def parse_task_ext(self, ext_data):
        if not ext_data:
            return {}
        if isinstance(ext_data, dict):
            return ext_data
        text = str(ext_data).strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
            return {}
        except Exception:
            return {'selected_attributes': text}
    
    def get_base_attributes(self, simc_profile):
        """
        获取基础属性值
        :param simc_profile: SimcProfile对象
        :return: 属性字典
        """
        def _value(field):
            try:
                return int(getattr(simc_profile, field, 0) or 0)
            except (TypeError, ValueError):
                return 0

        return {
            'gear_strength': _value('gear_strength'),
            'gear_crit': _value('gear_crit'),
            'gear_haste': _value('gear_haste'),
            'gear_mastery': _value('gear_mastery'),
            'gear_versatility': _value('gear_versatility'),
        }

    def select_template_by_spec(self, spec, player_config_mode=None):
        from botend.models import SimcContentTemplate
        spec_value = str(spec or '').strip().lower()
        import_mode = str(player_config_mode or '').strip().lower()

        queryset = SimcContentTemplate.objects.filter(
            is_active=True,
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
        ).order_by('id')
        return self._select_template_from_queryset(queryset, spec_value, player_config_mode=import_mode)

    @staticmethod
    def _is_executable_base_template(template):
        """Return whether a base template declares a real SimC player.

        # Attribute-only profiles need an explicit player header in the base
        # template, but do not require a persisted gear block.  Battle.net and
        # manual-equipment modes supply it through {player_config}.
        """
        content = str(getattr(template, 'content', '') or getattr(template, 'template_content', '') or '')
        return bool(re.search(
            r'^\s*(?:warrior|paladin|hunter|rogue|priest|deathknight|shaman|mage|warlock|monk|druid|demonhunter|evoker)\s*=',
            content, re.IGNORECASE | re.MULTILINE,
        ))

    def _select_template_from_queryset(self, queryset, spec_value, player_config_mode=None):
        rows = list(queryset)
        if not rows:
            return None
        # An armory directive imports a complete player, therefore it does not
        # need a template-owned player header.  It still needs the active base
        # template for shared encounter options and the selected default APL.
        if str(player_config_mode or '').strip().lower() == 'battlenet':
            usable_rows = rows
        else:
            usable_rows = [tpl for tpl in rows if self._is_executable_base_template(tpl)]
        if not usable_rows:
            logger.error('[SimC Monitor] 没有包含玩家块和装备基线的可执行基础模板')
            return None
        if len(usable_rows) != len(rows):
            skipped = [str(getattr(tpl, 'id', '')) for tpl in rows if tpl not in usable_rows]
            logger.warning('[SimC Monitor] 跳过非可执行基础模板: %s', ', '.join(skipped))
        if spec_value:
            for tpl in usable_rows:
                spec_field = str(getattr(tpl, 'spec', '') or '').strip().lower()
                if not spec_field:
                    continue
                if spec_field == spec_value:
                    return tpl
                candidates = [s.strip() for s in spec_field.split(',') if s.strip()]
                if spec_value in candidates:
                    return tpl

        for tpl in usable_rows:
            spec_field = str(getattr(tpl, 'spec', '') or '').strip().lower()
            if not spec_field:
                continue
            candidates = [s.strip() for s in spec_field.split(',') if s.strip()]
            if 'default' in candidates or 'all' in candidates or '*' in candidates:
                return tpl
        return None

    def _get_class_by_spec(self, spec):
        """Return SimC class slug by spec slug used in the dashboard selector."""
        spec = str(spec or '').strip().lower()
        spec_to_class = {
            'arms': 'warrior', 'fury': 'warrior', 'protection': 'warrior', 'protection_warrior': 'warrior',
            'havoc': 'demonhunter', 'vengeance': 'demonhunter',
            'balance': 'druid', 'feral': 'druid', 'guardian': 'druid', 'restoration': 'druid',
            'devastation': 'evoker', 'preservation': 'evoker', 'augmentation': 'evoker',
            'beast_mastery': 'hunter', 'marksmanship': 'hunter', 'survival': 'hunter',
            'arcane': 'mage', 'fire': 'mage', 'frost': 'mage',
            'brewmaster': 'monk', 'mistweaver': 'monk', 'windwalker': 'monk',
            'holy': 'priest', 'discipline': 'priest', 'shadow': 'priest',
            'retribution': 'paladin',
            'assassination': 'rogue', 'outlaw': 'rogue', 'subtlety': 'rogue',
            'elemental': 'shaman', 'enhancement': 'shaman', 'restoration_shaman': 'shaman',
            'affliction': 'warlock', 'demonology': 'warlock', 'destruction': 'warlock',
            'blood': 'deathknight', 'frost_dk': 'deathknight', 'unholy': 'deathknight',
        }
        return spec_to_class.get(spec, '')

    def apply_template(self, template_content, task_config):
        """
        新版模板渲染：接收模板内容和任务配置字典，生成SimC代码。
        
        :param template_content: 模板原始内容（包含 {spec}, {fight_style} 等占位符）
        :param task_config: SimcTask.ext 解析后的字典，包含所有运行时配置
        :return: 渲染后的SimC代码字符串
        """
        if not template_content:
            raise Exception("模板内容为空")
        if not task_config:
            task_config = {}
        
        simc_code = str(template_content)
        
        # 替换基础占位符
        simc_code = simc_code.replace('{fight_style}', str(task_config.get('fight_style', 'Patchwerk')))
        simc_code = simc_code.replace('{time}', str(task_config.get('time', 300)))
        simc_code = simc_code.replace('{target_count}', str(task_config.get('target_count', 1)))
        simc_code = simc_code.replace('{spec}', str(task_config.get('spec', 'fury')))
        simc_code = simc_code.replace('{talent}', str(task_config.get('talent', '')))
        
        # 处理 {player_config} 占位符：这里只拼“玩家信息块”，不接受完整 simc
        player_config_mode = task_config.get('player_import_mode') or task_config.get('player_config_mode', 'manual_equipment')
        if player_config_mode == 'equipment':
            player_config_mode = 'manual_equipment'

        # Manual and attribute exports are complete player blocks. Keep the template's
        # runtime/APL options, but drop fields owned by its placeholder actor. Attribute
        # tasks then append their candidate rating overrides to the frozen player block.
        if player_config_mode in ('manual_equipment', 'attribute_only'):
            player_equipment = str(task_config.get('player_equipment', '')).strip()
            if player_config_mode == 'attribute_only' and not player_equipment:
                raise ValueError('属性模拟缺少冻结的玩家装备基线')
            placeholder_count = simc_code.count('{player_config}')
            if placeholder_count != 1:
                raise ValueError('玩家模板中的 {player_config} 必须恰好一个')
            placeholder_pos = simc_code.find('{player_config}')
            prefix = simc_code[:placeholder_pos]
            suffix = simc_code[placeholder_pos + len('{player_config}'):]
            template_player_field = re.compile(
                r'^\s*(?:'
                r'warrior|paladin|hunter|rogue|priest|deathknight|shaman|mage|warlock|monk|druid|demonhunter|evoker|'
                r'source|spec|level|race|role|position|talents|'
                r'head|neck|shoulder|shoulders|back|chest|wrist|wrists|hands|waist|legs|feet|finger1|finger2|trinket1|trinket2|main_hand|off_hand|'
                r'gear_[a-z0-9_]+'
                r')\s*=',
                flags=re.IGNORECASE,
            )
            retained_prefix = '\n'.join(
                line for line in prefix.splitlines()
                if not template_player_field.match(line)
            ).strip()
            retained_suffix = '\n'.join(
                line for line in suffix.splitlines()
                if not template_player_field.match(line)
            ).strip()
            parts = ['{player_config}']
            if retained_prefix:
                parts.append(retained_prefix)
            if retained_suffix:
                parts.append(retained_suffix)
            simc_code = '\n'.join(parts)

        # ``armory`` is itself a complete SimC player import. Combining it with any
        # template-owned player option silently overrides the live character (for
        # example level=80 or an empty talents= after a level-90 Armory import).
        # Keep only global runtime options and the selected APL from the template.
        if player_config_mode == 'battlenet':
            battlenet_player_field = re.compile(
                r'^\s*(?:'
                r'warrior|paladin|hunter|rogue|priest|deathknight|shaman|mage|warlock|monk|druid|demonhunter|evoker|'
                r'source|spec|level|race|role|position|talents|'
                r'potion|flask|food|augmentation|temporary_enchant|'
                r'head|neck|shoulder|shoulders|back|chest|wrist|wrists|hands|waist|legs|feet|finger1|finger2|trinket1|trinket2|main_hand|off_hand|'
                r'gear_[a-z0-9_]+'
                r')\s*=',
                flags=re.IGNORECASE,
            )
            simc_code = '\n'.join(
                line for line in simc_code.splitlines()
                if not battlenet_player_field.match(line)
            )

        if player_config_mode == 'battlenet':
            region = str(task_config.get('battlenet_region', '')).strip().lower()
            realm = str(task_config.get('battlenet_realm', '')).strip()
            character = str(task_config.get('battlenet_character', '')).strip()
            if not region or not realm or not character:
                raise ValueError('Battle.net 导入缺少 region/realm/character')
            # ``armory`` creates the player actor, so it must precede all options
            # scoped to that imported player (source/level/role/consumables, etc.).
            # Base templates commonly put {player_config} after those options because
            # attribute/manual modes already have a template actor; leaving that order
            # in armory mode makes SimC parse them as unknown global options.
            simc_code = simc_code.replace('{player_config}', '')
            simc_code = f'armory={region},{realm},{character}\n{simc_code.lstrip()}'
            simc_code = simc_code.replace('{gear_crit}', '')
            simc_code = simc_code.replace('{gear_haste}', '')
            simc_code = simc_code.replace('{gear_mastery}', '')
            simc_code = simc_code.replace('{gear_versatility}', '')
        elif player_config_mode == 'manual_equipment':
            # 手动装备模式：只插入权威已装备区；背包/周常候选不得参与执行。
            player_equipment = authoritative_player_baseline(task_config.get('player_equipment', ''))
            simc_code = simc_code.replace('{player_config}', player_equipment)
            simc_code = simc_code.replace('{gear_crit}', '')
            simc_code = simc_code.replace('{gear_haste}', '')
            simc_code = simc_code.replace('{gear_mastery}', '')
            simc_code = simc_code.replace('{gear_versatility}', '')
        elif player_config_mode == 'attribute_only':
            # SimC treats gear_*_rating as final rating overrides. Keep one frozen
            # real player/equipment block for every candidate, remove prior overrides,
            # then append this task's talent and rating values.
            player_equipment = validate_player_baseline(task_config.get('player_equipment', ''))
            overridden_field = re.compile(
                r'^\s*(?:talents|talent|gear_strength|gear_(?:crit|haste|mastery|versatility)(?:_rating)?)\s*=',
                flags=re.IGNORECASE,
            )
            frozen_player = '\n'.join(
                line for line in player_equipment.splitlines()
                if not overridden_field.match(line)
            ).strip()
            attribute_lines = []
            talent = str(task_config.get('talent', '')).strip()
            if talent:
                attribute_lines.append(f'talents={talent}')
            # Attribute optimization searches the four secondary ratings.  It
            # must not replace the primary stat with the legacy profile value
            # (especially zero); the equipment block remains authoritative.
            for field in ('crit', 'haste', 'mastery', 'versatility'):
                value = task_config.get(f'gear_{field}')
                if value not in (None, ''):
                    # These are player-scoped SimC stats. Bare ``crit_rating`` is
                    # parsed as an unknown global option and silently ignored.
                    attribute_lines.append(f'gear_{field}_rating={value}')
            simc_code = simc_code.replace(
                '{player_config}',
                '\n'.join(part for part in (frozen_player, '\n'.join(attribute_lines)) if part),
            )
            simc_code = simc_code.replace('{gear_crit}', '')
            simc_code = simc_code.replace('{gear_haste}', '')
            simc_code = simc_code.replace('{gear_mastery}', '')
            simc_code = simc_code.replace('{gear_versatility}', '')
        else:
            raise ValueError(f'不支持的玩家信息导入方式: {player_config_mode}')
        
        # 处理 {action_list} 占位符
        override_action_list = str(task_config.get('override_action_list', '')).strip()
        simc_code = simc_code.replace('{action_list}', override_action_list)
        
        # 清理空行：连续多个空行合并为一个
        simc_code = re.sub(r'\n{3,}', '\n\n', simc_code)
        simc_code = simc_code.strip()
        result_file = task_config.get('result_file')
        if result_file not in (None, ''):
            simc_code = self.ensure_result_file_directive(simc_code, result_file)
        return simc_code

    @staticmethod
    def build_final_config_validation(simc_code):
        """Build a non-sensitive structural audit of the exact rendered input."""
        import hashlib

        text = str(simc_code or '')
        actor_pattern = r'^\s*(?:warrior|paladin|hunter|rogue|priest|deathknight|shaman|mage|warlock|monk|druid|demonhunter|evoker)\s*='
        equipment_pattern = r'^\s*(?:head|neck|shoulder|shoulders|back|chest|wrist|wrists|hands|waist|legs|feet|finger1|finger2|trinket1|trinket2|main_hand|off_hand)\s*='
        return {
            'char_count': len(text),
            'line_count': len(text.splitlines()),
            'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
            'actor_count': len(re.findall(actor_pattern, text, flags=re.IGNORECASE | re.MULTILINE)),
            'spec_count': len(re.findall(r'^\s*spec\s*=', text, flags=re.IGNORECASE | re.MULTILINE)),
            'talents_count': len(re.findall(r'^\s*talents\s*=', text, flags=re.IGNORECASE | re.MULTILINE)),
            'equipment_count': len(re.findall(equipment_pattern, text, flags=re.IGNORECASE | re.MULTILINE)),
            'action_count': len(re.findall(r'^\s*actions(?:\.[^=\s]+)?(?:\+)?\s*=', text, flags=re.IGNORECASE | re.MULTILINE)),
            'html_output_count': len(re.findall(r'^\s*html\s*=', text, flags=re.IGNORECASE | re.MULTILINE)),
            'placeholder_count': len(re.findall(r'\{[a-zA-Z_][a-zA-Z0-9_]*\}', text)),
        }

    @staticmethod
    def persist_final_config_validation(simc_task, simc_code):
        try:
            manifest = json.loads(simc_task.ext) if simc_task.ext else {}
        except (TypeError, ValueError):
            manifest = {}
        if not isinstance(manifest, dict):
            manifest = {}
        manifest['final_config_validation'] = SimcMonitor.build_final_config_validation(simc_code)
        simc_task.ext = json.dumps(manifest, ensure_ascii=False)
        simc_task.save(update_fields=['ext'])

