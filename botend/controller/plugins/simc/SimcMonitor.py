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
from django.conf import settings
from django.utils import timezone
from utils.log import logger
from botend.models import SimcTask, SimcProfile, SimcBackendBinary
from botend.alerting import upsert_system_alert
from botend.controller.BaseScan import BaseScan


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
                self._set_update_status(row, status='自动编译失败', progress=0, is_updating=False, last_error=err)
                upsert_system_alert('SIMC_UPDATE_FAILED', self._get_runtime_platform(), 3, 'SimC 自动更新失败', err)
                logger.error(f"[SimC Monitor] Auto update local SimC backend failed: {err}")
                return ok

        status = f'本地 SimC 已是最新 {current_hash}' if current_hash else '本地 SimC 可用'
        self._set_update_status(row, status=status, progress=100, is_updating=False, latest_version=latest_version, current_version=current_hash or row.current_version, last_error='')
        return True

    def mark_task_failed(self, simc_task, reason, exc=None, overwrite_when_has_error=False):
        """
        将任务标记为失败，并写入可见错误信息。
        """
        try:
            detail = str(reason or "未知错误").strip()
            if exc is not None:
                detail = f"{detail}\n异常信息: {str(exc)}"

            current_result = str(simc_task.result_file or "").strip()
            has_existing_error = bool(current_result) and not current_result.endswith('.html')
            should_overwrite = overwrite_when_has_error or (not has_existing_error)
            if should_overwrite:
                simc_task.result_file = detail

            if exc is not None:
                self.save_simc_error_details(
                    simc_task,
                    summary=str(reason or "任务失败").strip(),
                    stderr_text=str(exc)
                )

            simc_task.current_status = 3
            simc_task.save()
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
            # ext 字段上限5000，循环裁剪原生日志以保证可落库
            if len(serialized) > 4800:
                native_value = str(payload.get('simc_error_native') or '')
                while len(serialized) > 4800 and native_value:
                    native_value = native_value[:max(0, len(native_value) - 400)]
                    payload['simc_error_native'] = native_value + '\n...(原生错误已截断)' if native_value else '(原生错误过长，已截断)'
                    serialized = json.dumps(payload, ensure_ascii=False)
            if len(serialized) > 5000:
                payload.pop('simc_error_native', None)
                serialized = json.dumps(payload, ensure_ascii=False)
            if len(serialized) > 5000:
                payload['simc_error_summary'] = str(payload.get('simc_error_summary') or '')[:200]
                serialized = json.dumps(payload, ensure_ascii=False)
            if len(serialized) > 5000:
                payload = {'simc_error_summary': str(summary or '')[:200]}
                if return_code is not None:
                    payload['simc_error_code'] = return_code
                serialized = json.dumps(payload, ensure_ascii=False)
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

            # 每次 MonitorTask 调度只取一个待执行 SimC 任务，避免单次 scan 长时间占用全局监控循环。
            # 任务继续按外层 MonitorTask 的 last_scan_time/wait_time 排序进入下一轮调度。
            simc_task = SimcTask.objects.filter(is_active=True, current_status=0).order_by('modified_time', 'id').first()
            if simc_task:
                logger.info(f"[SimC Monitor] Processing task: {simc_task.name} (ID: {simc_task.id})")
                self.process_simc_task(simc_task)
            else:
                logger.info("[SimC Monitor] No pending SimC task.")
                
        except Exception as e:
            logger.error(f"[SimC Monitor] Error during SimC simulation: {str(e)}")
            self.fail_pending_tasks("SimC调度进程异常，请稍后重试")
            return False
            
        return True

    def process_simc_task(self, simc_task):
        """
        处理单个SimC任务
        :param simc_task: SimcTask对象
        :return:
        """
        try:
            # 原子抢占待处理任务，避免多个调度进程同时执行同一个 SimC 任务。
            claimed = SimcTask.objects.filter(
                id=simc_task.id,
                is_active=True,
                current_status=0
            ).update(current_status=1, modified_time=timezone.now())
            if claimed != 1:
                logger.info(f"[SimC Monitor] Task {simc_task.id} was already claimed or no longer pending, skip")
                return False
            simc_task.refresh_from_db()
            self.clear_simc_error_details(simc_task)
            simc_task.save(update_fields=['ext', 'modified_time'])
            
            ext_payload = self.parse_task_ext(simc_task.ext)
            raw_simc_code = str(ext_payload.get('raw_simc_code') or '').strip()

            # 直接 SimC 代码只支持常规模拟，不依赖 SimcProfile。
            if raw_simc_code and int(simc_task.task_type or 1) == 1:
                return self.process_regular_simulation(simc_task, None)

            if raw_simc_code and int(simc_task.task_type or 1) == 2:
                self.mark_task_failed(simc_task, "直接 SimC 代码不支持属性模拟，请选择 SimC 配置后再运行属性模拟")
                return False

            # 新版任务配置模式（player_config_mode）不依赖 SimcProfile，直接走常规模拟。
            if ext_payload.get('player_config_mode'):
                return self.process_regular_simulation(simc_task, None)

            # 旧版 Profile + 模板模式、属性模拟都需要 SimcProfile。
            simc_profile = SimcProfile.objects.filter(
                id=simc_task.simc_profile_id,
                user_id=simc_task.user_id,
                is_active=True
            ).first()

            if not simc_profile:
                logger.error(f"[SimC Monitor] SimC profile not found for task {simc_task.id}")
                self.mark_task_failed(simc_task, "未找到对应的SimC配置，可能已被删除或禁用")
                return False

            # 根据任务类型选择处理方式
            if simc_task.task_type == 2:  # 属性模拟
                return self.process_attribute_simulation(simc_task, simc_profile)
            else:  # 常规模拟
                return self.process_regular_simulation(simc_task, simc_profile)
            
        except Exception as e:
            logger.error(f"[SimC Monitor] Error processing task {simc_task.id}: {str(e)}")
            self.mark_task_failed(simc_task, "任务处理失败", e)
            return False
        
        return True
    
    def process_regular_simulation(self, simc_task, simc_profile):
        """
        处理常规模拟任务
        :param simc_task: SimcTask对象
        :param simc_profile: SimcProfile对象（可选，用于兼容旧任务）
        :return: 执行是否成功
        """
        try:
            ext_payload = self.parse_task_ext(simc_task.ext)
            # Every regular task owns one deterministic report path. Batch comparisons
            # must never discover a directory-neighbour's report after SimC exits.
            result_file = self.ensure_regular_result_file(simc_task)

            # ── 直接 SimC 代码模式 ──
            raw_simc_code = ext_payload.get('raw_simc_code')
            if raw_simc_code and str(raw_simc_code).strip():
                logger.info(f"[SimC Monitor] Task {simc_task.id}: using raw simc code ({len(raw_simc_code)} chars)")
                simc_code = str(raw_simc_code).strip()
                override_action_list = ext_payload.get('override_action_list')
                if override_action_list:
                    simc_code = self.apply_action_list_override_to_raw(simc_code, override_action_list)
                simc_code = self.ensure_result_file_directive(simc_code, result_file)
            else:
                # ── 新版任务配置模式（从 ext JSON 读取所有配置） ──
                # 如果 ext 中有 player_config_mode，使用新逻辑
                if ext_payload.get('player_config_mode'):
                    # 从数据库获取模板
                    spec_value = ext_payload.get('spec') or (simc_profile.spec if simc_profile else 'fury')
                    template_obj = self.select_template_by_spec(spec_value)
                    if not template_obj:
                        raise Exception("未找到启用的SimC模板")
                    template = getattr(template_obj, 'content', None) or getattr(template_obj, 'template_content', '')
                    
                    # 构建 task_config 字典
                    task_config = {
                        'spec': spec_value,
                        'talent': ext_payload.get('talent') or (simc_profile.talent if simc_profile else ''),
                        'fight_style': ext_payload.get('fight_style', 'Patchwerk'),
                        'time': ext_payload.get('time', 300),
                        'target_count': ext_payload.get('target_count', 1),
                        'player_config_mode': ext_payload.get('player_config_mode'),
                        'player_import_mode': ext_payload.get('player_import_mode') or ext_payload.get('player_config_mode'),
                        'player_equipment': ext_payload.get('player_equipment', ''),
                        'battlenet_region': ext_payload.get('battlenet_region', ''),
                        'battlenet_realm': ext_payload.get('battlenet_realm', ''),
                        'battlenet_character': ext_payload.get('battlenet_character', ''),
                        'gear_crit': ext_payload.get('gear_crit', 10730),
                        'gear_haste': ext_payload.get('gear_haste', 18641),
                        'gear_mastery': ext_payload.get('gear_mastery', 21785),
                        'gear_versatility': ext_payload.get('gear_versatility', 6757),
                        'override_action_list': ext_payload.get('override_action_list', ''),
                    }
                    
                    simc_code = self.apply_template(
                        template_content=template,
                        task_config={**task_config, 'result_file': result_file}
                    )
                else:
                    # ── 兼容旧版 Profile + 模板模式 ──
                    if not simc_profile:
                        self.mark_task_failed(simc_task, "未找到对应的SimC配置，可能已被删除或禁用")
                        return False
                    
                    override_time = ext_payload.get('regular_time')
                    override_target_count = ext_payload.get('regular_target_count')
                    override_action_list = ext_payload.get('override_action_list')
                    logger.info(
                        f"[SimC Monitor] Regular overrides for task {simc_task.id}: "
                        f"time={override_time}, targets={override_target_count}"
                    )

                    # 生成SimC代码
                    simc_code = self.generate_simc_code(
                        simc_profile,
                        result_file,
                        override_time=override_time,
                        override_target_count=override_target_count,
                        override_action_list=override_action_list
                    )
                
                if not isinstance(simc_code, str) or not simc_code.strip():
                    raise Exception("生成SimC配置失败：模板渲染结果为空")
            
            # 创建临时SimC文件
            simc_file_path = os.path.join(self.result_path, f"temp_{simc_task.id}.simc")
            
            with open(simc_file_path, 'w', encoding='utf-8') as f:
                f.write(simc_code)
            
            # 执行SimC命令；明确传递本任务唯一结果名，禁止目录扫描回退。
            success = self.execute_simc_command(simc_file_path, simc_task, result_file)
            
            # 清理临时文件
            if os.path.exists(simc_file_path):
                os.remove(simc_file_path)
            
            # 更新任务状态
            if success:
                simc_task.current_status = 2  # 完成
                logger.info(f"[SimC Monitor] Regular simulation task {simc_task.id} completed successfully")
            else:
                simc_task.current_status = 3  # 失败
                logger.error(f"[SimC Monitor] Regular simulation task {simc_task.id} failed")
            
            simc_task.save()
            return success
            
        except Exception as e:
            logger.error(f"[SimC Monitor] Error in regular simulation for task {simc_task.id}: {str(e)}")
            self.mark_task_failed(simc_task, "常规模拟执行异常", e)
            return False
    
    MAX_ATTRIBUTE_TEST_POINTS = 25

    def build_attribute_test_points(self, total_value, base_value, requested_step):
        """Bound legacy two-stat scans while preserving endpoints and the real baseline."""
        try:
            total = max(0, int(total_value))
            baseline = min(total, max(0, int(base_value)))
            step = max(1, int(requested_step))
        except (TypeError, ValueError):
            total, baseline, step = 0, 0, 50
        if total == 0:
            return [0]
        requested = list(range(0, total + 1, step))
        if requested[-1] != total:
            requested.append(total)
        if len(requested) <= self.MAX_ATTRIBUTE_TEST_POINTS:
            return sorted(set(requested + [baseline]))
        intervals = self.MAX_ATTRIBUTE_TEST_POINTS - 1
        grid = [round(total * index / intervals) for index in range(intervals + 1)]
        # Keep the original allocation on the curve, replacing the nearest grid point
        # so the hard cap still applies.
        nearest = min(range(1, len(grid) - 1), key=lambda index: abs(grid[index] - baseline))
        grid[nearest] = baseline
        return sorted(set(grid))

    def process_attribute_simulation(self, simc_task, simc_profile):
        """
        :param simc_task: SimcTask对象
        :param simc_profile: SimcProfile对象
        :return: 执行是否成功
        """
        try:
            ext_payload = self.parse_task_ext(simc_task.ext)
            selected_combination = ext_payload.get('selected_attributes') or simc_task.ext
            step_size = ext_payload.get('attribute_step') or 50
            try:
                step_size = max(1, int(step_size))
            except Exception:
                step_size = 50

            # 解析属性组合
            selected_attributes = self.parse_selected_attributes(selected_combination)
            if len(selected_attributes) != 2:
                logger.error(f"[SimC Monitor] Attribute simulation requires exactly 2 attributes, got {len(selected_attributes)} for task {simc_task.id}")
                self.mark_task_failed(simc_task, f"属性模拟参数错误：需要2个属性，当前为{len(selected_attributes)}个")
                return False
            
            # 获取基础属性值
            base_attributes = self.get_base_attributes(simc_profile)
            
            # 获取两个属性及其总和
            attr1, attr2 = selected_attributes[0], selected_attributes[1]
            attr1_base = base_attributes[attr1]
            attr2_base = base_attributes[attr2]
            total_value = attr1_base + attr2_base
            
            logger.info(f"[SimC Monitor] Starting attribute simulation for {attr1} and {attr2}, total: {total_value}, task {simc_task.id}")
            
            # 执行分阶段模拟
            result_files = []
            stage = 0
            
            # Keep the endpoints and equipped allocation, but cap dense legacy scans.
            test_points = self.build_attribute_test_points(total_value, attr1_base, step_size)
            logger.info(
                f"[SimC Monitor] Attribute task {simc_task.id}: {len(test_points)} points "
                f"(requested step={step_size}, cap={self.MAX_ATTRIBUTE_TEST_POINTS})"
            )
            
            for attr1_value in test_points:
                attr2_value = total_value - attr1_value
                
                stage_result_file = f"{simc_task.id}_{attr1}_{attr1_value}_{attr2}_{attr2_value}.html"
                
                # 生成当前阶段的SimC代码
                modified_attributes = base_attributes.copy()
                modified_attributes[attr1] = attr1_value
                modified_attributes[attr2] = attr2_value
                
                simc_code = self.generate_attribute_simc_code(simc_profile, modified_attributes, stage_result_file)
                if not isinstance(simc_code, str) or not simc_code.strip():
                    raise Exception(f"生成属性模拟配置失败：stage={stage}")
                
                # 创建临时SimC文件
                simc_file_path = os.path.join(self.result_path, f"temp_{simc_task.id}_{stage}.simc")
                
                with open(simc_file_path, 'w', encoding='utf-8') as f:
                    f.write(simc_code)
                
                # 执行SimC命令
                success = self.execute_simc_command(simc_file_path, simc_task, stage_result_file)
                
                # 清理临时文件
                if os.path.exists(simc_file_path):
                    os.remove(simc_file_path)
                
                if success:
                    result_files.append(stage_result_file)
                    logger.info(f"[SimC Monitor] Stage {stage} ({attr1}:{attr1_value}, {attr2}:{attr2_value}) completed for task {simc_task.id}")
                else:
                    logger.error(f"[SimC Monitor] Stage {stage} ({attr1}:{attr1_value}, {attr2}:{attr2_value}) failed for task {simc_task.id}")
                
                stage += 1
            
            # 保存所有结果文件名（以逗号分割）
            simc_task.result_file = ','.join(result_files)
            
            # 更新任务状态
            if result_files:
                simc_task.current_status = 2  # 完成
                logger.info(f"[SimC Monitor] Attribute simulation task {simc_task.id} completed with {len(result_files)} result files")
            else:
                self.mark_task_failed(simc_task, "属性模拟未生成任何结果文件")
                logger.error(f"[SimC Monitor] Attribute simulation task {simc_task.id} failed - no results generated")
            
            if simc_task.current_status == 2:
                simc_task.save()
            return len(result_files) > 0
            
        except Exception as e:
            logger.error(f"[SimC Monitor] Error in attribute simulation for task {simc_task.id}: {str(e)}")
            self.mark_task_failed(simc_task, "属性模拟执行异常", e)
            return False

    def apply_action_list_override_to_raw(self, simc_code, override_action_list):
        """在 raw SimC 模式下用选中的 APL 覆盖原 action list。"""
        code = str(simc_code or '').strip()
        action_list = str(override_action_list or '').strip()
        if not code or not action_list:
            return code
        kept_lines = []
        removed = 0
        for line in code.splitlines():
            text = line.strip()
            if text.startswith('actions') or text.startswith('action_list'):
                removed += 1
                continue
            kept_lines.append(line)
        if removed:
            logger.info(f"[SimC Monitor] raw SimC 已移除 {removed} 行旧 APL，使用任务选择的 APL")
        else:
            logger.info("[SimC Monitor] raw SimC 未发现旧 APL，追加任务选择的 APL")
        return '\n'.join(kept_lines).rstrip() + '\n\n' + action_list + '\n'

    def _load_default_apl(self, spec):
        """
        从统一 SimC 内容模板表加载默认 APL（按专精自动匹配）。
        :param spec: 专精标识（如 fury, arms, balance）
        :return: APL 文本或 None
        """
        try:
            from botend.models import SimcContentTemplate
            spec_value = str(spec or '').strip().lower()
            if not spec_value:
                return None
            apl = SimcContentTemplate.objects.filter(
                is_active=True,
                template_type=SimcContentTemplate.TYPE_DEFAULT_APL,
                source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
                spec__endswith=f'_{spec_value}'
            ).order_by('id').first()
            if not apl:
                apl = SimcContentTemplate.objects.filter(
                    is_active=True,
                    template_type=SimcContentTemplate.TYPE_DEFAULT_APL,
                    source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
                    spec=spec_value
                ).order_by('id').first()
            if apl:
                logger.info(f"[SimC Monitor] 自动加载默认 APL: {apl.spec}")
                return apl.content
            logger.warning(f"[SimC Monitor] 未找到专精 {spec_value} 的默认 APL")
            return None
        except Exception as e:
            logger.error(f"[SimC Monitor] 加载默认 APL 失败: {e}")
            return None

    def generate_simc_code(self, profile, result_file, override_time=None, override_target_count=None, override_action_list=None):
        """
        生成SimC代码（从数据库模板 + profile 配置）
        :param profile: SimcProfile对象
        :param result_file: 结果文件名
        :return: 生成的SimC代码字符串
        """
        try:
            # ── 自动 APL：当用户未指定 override_action_list 时加载默认 APL ──
            if not override_action_list:
                override_action_list = self._load_default_apl(profile.spec)

            # 从数据库获取统一基础模板
            template_obj = self.select_template_by_spec(profile.spec)
            if not template_obj:
                raise Exception("未找到启用的SimC模板")
            template = getattr(template_obj, 'content', None) or getattr(template_obj, 'template_content', '')
            
            # 构建 task_config 字典
            task_config = {
                'spec': profile.spec or 'fury',
                'talent': profile.talent or '',
                'fight_style': 'Patchwerk',
                'time': override_time or 300,
                'target_count': override_target_count or 1,
                'player_config_mode': 'stats',
                'gear_crit': profile.gear_crit or 10730,
                'gear_haste': profile.gear_haste or 18641,
                'gear_mastery': profile.gear_mastery or 21785,
                'gear_versatility': profile.gear_versatility or 6757,
                'override_action_list': override_action_list or '',
                'result_file': result_file,
            }
            
            return self.apply_template(
                template_content=template,
                task_config=task_config
            )
            
        except Exception as e:
            logger.error(f"[SimC Monitor] Error generating SimC code: {str(e)}")
            raise e

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

    def execute_simc_command(self, simc_file_path, simc_task, result_file_name=None):
        """
        执行SimC命令
        :param simc_file_path: SimC文件路径
        :param simc_task: SimcTask对象
        :param result_file_name: 自定义结果文件名（可选）
        :return: 执行是否成功
        """
        try:
            
            # 构建命令
            cmd = [self.simc_path, simc_file_path]
            
            logger.info(f"[SimC Monitor] Executing command: {' '.join(cmd)}")
            
            # 记录执行前的 HTML 文件，用于后续检测 SimC 生成的新文件
            existing_htmls = set(
                f for f in os.listdir(self.result_path) if f.endswith('.html')
            )
            
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
                target_result_file = str(result_file_name or simc_task.result_file or '').strip()
                if not target_result_file or not target_result_file.endswith('.html'):
                    raise RuntimeError('SimC任务未配置唯一 HTML 结果文件')
                result_file_path = os.path.join(self.result_path, target_result_file)
                if not os.path.isfile(result_file_path):
                    raise RuntimeError(f'SimC未生成预期结果文件: {target_result_file}')

                if target_result_file != simc_task.result_file:
                    simc_task.result_file = target_result_file
                    simc_task.save(update_fields=['result_file', 'modified_time'])
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
                
                # 构建错误信息并直接存储到result_file字段
                error_info = f"SimC执行失败\n返回码: {result.returncode}\n"
                if result.stderr:
                    logger.error(f"[SimC Monitor] Error output: {result.stderr}")
                    error_info += f"错误输出: {result.stderr}\n"
                if result.stdout:
                    error_info += f"标准输出: {result.stdout}\n"
                
                # 直接将错误信息存储到result_file字段
                simc_task.result_file = error_info
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
            # 直接将错误信息存储到result_file字段
            simc_task.result_file = error_info
            self.save_simc_error_details(
                simc_task,
                summary="SimC执行超时（300秒）"
            )
            simc_task.save()
            return False
        except Exception as e:
            error_info = f"SimC执行异常\n任务ID: {simc_task.id}\n异常信息: {str(e)}"
            logger.error(f"[SimC Monitor] Error executing SimC command: {str(e)}")
            # 直接将错误信息存储到result_file字段
            simc_task.result_file = error_info
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
        return {
            'gear_strength': simc_profile.gear_strength or 93330,
            'gear_crit': simc_profile.gear_crit or 10730,
            'gear_haste': simc_profile.gear_haste or 18641,
            'gear_mastery': simc_profile.gear_mastery or 21785,
            'gear_versatility': simc_profile.gear_versatility or 6757
        }
    
    def generate_attribute_simc_code(self, profile, attributes, result_file):
        """
        生成属性模拟的SimC代码（从数据库模板 + profile 配置）
        :param profile: SimcProfile对象
        :param attributes: 修改后的属性字典
        :param result_file: 结果文件名
        :return: 生成的SimC代码字符串
        """
        try:
            # 从数据库获取统一基础模板
            template_obj = self.select_template_by_spec(profile.spec)
            if not template_obj:
                raise Exception("未找到启用的SimC模板")
            template = getattr(template_obj, 'content', None) or getattr(template_obj, 'template_content', '')
            
            # 构建 task_config 字典
            task_config = {
                'spec': profile.spec or 'fury',
                'talent': profile.talent or '',
                'fight_style': 'Patchwerk',
                'time': 300,
                'target_count': 1,
                'player_config_mode': 'stats',
                'gear_crit': attributes.get('gear_crit', 10730),
                'gear_haste': attributes.get('gear_haste', 18641),
                'gear_mastery': attributes.get('gear_mastery', 21785),
                'gear_versatility': attributes.get('gear_versatility', 6757),
                'override_action_list': self._load_default_apl(profile.spec) or '',
            }
            
            return self.apply_template(
                template_content=template,
                task_config=task_config
            )
            
        except Exception as e:
            logger.error(f"[SimC Monitor] Error generating attribute SimC code: {str(e)}")
            raise e

    def select_template_by_spec(self, spec):
        from botend.models import SimcContentTemplate
        spec_value = str(spec or '').strip().lower()

        queryset = SimcContentTemplate.objects.filter(
            is_active=True,
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
        ).order_by('id')
        return self._select_template_from_queryset(queryset, spec_value)

    def _select_template_from_queryset(self, queryset, spec_value):
        rows = list(queryset)
        if not rows:
            return None
        if spec_value:
            for tpl in rows:
                spec_field = str(getattr(tpl, 'spec', '') or '').strip().lower()
                if not spec_field:
                    continue
                if spec_field == spec_value:
                    return tpl
                candidates = [s.strip() for s in spec_field.split(',') if s.strip()]
                if spec_value in candidates:
                    return tpl

        for tpl in rows:
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

        if player_config_mode == 'battlenet':
            region = str(task_config.get('battlenet_region', '')).strip().lower()
            realm = str(task_config.get('battlenet_realm', '')).strip()
            character = str(task_config.get('battlenet_character', '')).strip()
            if not region or not realm or not character:
                raise ValueError('Battle.net 导入缺少 region/realm/character')
            # 生产基础模板已经包含 player header/spec/role；这里仅插入 armory 导入行，
            # 避免额外生成第二个 player 导致空模板角色参与模拟。
            simc_code = simc_code.replace('{player_config}', f'armory={region},{realm},{character}')
            simc_code = simc_code.replace('{gear_crit}', '')
            simc_code = simc_code.replace('{gear_haste}', '')
            simc_code = simc_code.replace('{gear_mastery}', '')
            simc_code = simc_code.replace('{gear_versatility}', '')
        elif player_config_mode == 'manual_equipment':
            # 手动装备模式：插入用户提供的玩家装备/天赋信息块，战斗/APL 仍由模板和选项控制
            player_equipment = str(task_config.get('player_equipment', '')).strip()
            simc_code = simc_code.replace('{player_config}', player_equipment)
            simc_code = simc_code.replace('{gear_crit}', '')
            simc_code = simc_code.replace('{gear_haste}', '')
            simc_code = simc_code.replace('{gear_mastery}', '')
            simc_code = simc_code.replace('{gear_versatility}', '')
        elif player_config_mode == 'attribute_only':
            # 历史属性型 Profile 只有天赋和副属性，不绑定角色标识或装备行。
            attribute_lines = []
            talent = str(task_config.get('talent', '')).strip()
            if talent:
                attribute_lines.append(f'talents={talent}')
            for field in ('crit', 'haste', 'mastery', 'versatility'):
                value = task_config.get(f'gear_{field}')
                if value not in (None, ''):
                    attribute_lines.append(f'{field}_rating={value}')
            simc_code = simc_code.replace('{player_config}', '\n'.join(attribute_lines))
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

