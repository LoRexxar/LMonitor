#!/usr/bin/env python
# encoding: utf-8
"""
管理命令：编译/更新服务器端 SimC 二进制
用法：
  python manage.py update_simc_binary                 # 自动保存 tracked 改动 + git pull --rebase + 编译
  python manage.py update_simc_binary --no-pull       # 仅编译（不拉代码）
  python manage.py update_simc_binary --check         # 仅检查版本
  python manage.py update_simc_binary --apply-patches # 有新本地补丁时才编译
  python manage.py update_simc_binary --threads 1     # 降低编译并行度
"""
import os
import re
import subprocess
import fcntl
import tempfile
import json
from datetime import datetime, timezone as datetime_timezone

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from botend.models import (SimcApl, SimcBackendBinary, SimcContentTemplate,
                           WowSpellSnapshotState, WowTalentVersion)
from botend.services.simc_apl.authoritative_validator import RestrictedSimcValidator
from botend.services.simc_apl.publish import content_hash
from botend.services.simc_apl.validation import validate_payload
from botend.services.simc_composer import SimcComposer
from botend.services.simc_player_config import canonical_simc_spec_identity


DEFAULT_SIMC_SOURCE_DIR = '/home/lighthouse/simc'


class Command(BaseCommand):
    help = '在服务器上编译/更新 SimulationCraft 二进制'

    def add_arguments(self, parser):
        parser.add_argument('--no-pull', action='store_true', help='不执行 git pull，仅编译当前代码')
        parser.add_argument('--check', action='store_true', help='仅检查当前版本，不执行编译')
        parser.add_argument('--sync-inputs-only', action='store_true', help='仅同步默认模板和默认 APL，不执行拉取/编译')
        parser.add_argument('--apply-patches', action='store_true', help='应用仓库补丁，仅在源码变化时编译')
        parser.add_argument('--threads', type=int, default=2, help='编译并行度（默认 2，内存不足时降低）')
        parser.add_argument('--wow-build', default='', help='本次 APL/symbol 发布对应的明确 WoW build')

    def handle(self, *args, **options):
        with open('/tmp/lmonitor-simc-update.lock', 'w') as command_lock:
            try:
                fcntl.flock(command_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CommandError('另一个 SimC 更新正在运行') from exc
            self.platform = 'linux64'
            self.simc_source_dir, self.simc_build_dir, self.simc_binary_path = self._resolve_paths()
            self.wow_build_override = str(options.get('wow_build') or '').strip()
            self.row = self._get_row()

            if options['check']:
                self._check_version()
                return
            if options['sync_inputs_only']:
                git_hash = self._get_git_hash()
                current_version = self.row.current_version
                if not self._revision_matches_git_hash(current_version, git_hash):
                    raise CommandError('当前 SimC 二进制 revision 与源码 HEAD 不一致，拒绝仅同步输入')
                self._sync_generated_inputs(git_hash=git_hash,
                                            binary_path=self.simc_binary_path,
                                            binary_revision=git_hash)
                if current_version != git_hash:
                    # Promote legacy metadata only after the entire transactional
                    # corpus/symbol publication has succeeded.
                    self.row.current_version = git_hash
                    self.row.save(update_fields=['current_version'])
                self._set_status(progress=100, status='默认模板和 APL 同步完成', error='', updating=False)
                return
            if options['apply_patches']:
                self._apply_patches_only(threads=max(1, int(options['threads'] or 1)))
                return

            self._update_binary(
                do_pull=not options['no_pull'],
                threads=max(1, int(options['threads'] or 1)),
            )

    def _resolve_paths(self):
        cfg = getattr(settings, 'SIMC_CONFIG', {}) or {}
        source_dir = str(cfg.get('simc_source_dir') or DEFAULT_SIMC_SOURCE_DIR).rstrip('/')
        build_dir = str(cfg.get('simc_build_dir') or os.path.join(source_dir, 'build-cli')).rstrip('/')
        binary_path = str(cfg.get('simc_path') or os.path.join(build_dir, 'simc'))
        return source_dir, build_dir, binary_path

    def _get_row(self):
        row, _ = SimcBackendBinary.objects.get_or_create(
            platform=self.platform,
            defaults={
                'simc_path': self._stored_simc_path(),
                'current_version': '',
                'latest_version': '',
                'auto_update': True,
            }
        )
        return row

    def _stored_simc_path(self):
        """Return the operational path bounded for the database field."""
        return str(self.simc_binary_path)[:500]

    def _set_status(self, progress=None, status=None, error='', updating=None, updated=False):
        now = timezone.now()
        fields = ['last_checked_at']
        self.row.last_checked_at = now
        if progress is not None:
            self.row.update_progress = int(progress)
            fields.append('update_progress')
        if status is not None:
            self.row.update_status = str(status)[:255]
            fields.append('update_status')
        if error is not None:
            self.row.last_error = str(error)[:500]
            fields.append('last_error')
        if updating is not None:
            self.row.is_updating = bool(updating)
            fields.append('is_updating')
        if updated:
            self.row.last_updated_at = now
            fields.append('last_updated_at')
        self.row.save(update_fields=fields)

    def _fail(self, status, message, progress=0):
        self._set_status(progress=progress, status=status, error=message, updating=False)
        raise CommandError(message)

    def _run(self, cmd, cwd, timeout, status, progress):
        self._set_status(progress=progress, status=status, error='', updating=True)
        self.stdout.write(status)
        try:
            result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            self._fail(f'{status}超时', f'{status}超时: {exc}', progress=progress)
        except Exception as exc:
            self._fail(f'{status}失败', f'{status}失败: {exc}', progress=progress)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or '').strip()[-1000:]
            self._fail(f'{status}失败', detail or f'{status}失败，退出码 {result.returncode}', progress=progress)
        return result

    def _probe_binary(self, binary_path=None):
        """运行无参数探针；由调用方按场景处理失败状态。"""
        path = binary_path or self.simc_binary_path
        result = subprocess.run([path], capture_output=True, text=True, timeout=10)
        output = result.stdout + result.stderr
        return result, output

    def _check_version(self):
        if not os.path.isfile(self.simc_binary_path):
            msg = f'二进制不存在: {self.simc_binary_path}'
            self.row.simc_path = self._stored_simc_path()
            self.row.current_version = ''
            self.row.last_error = msg[:500]
            self.row.is_updating = False
            self.row.update_progress = 0
            self.row.update_status = '二进制不存在'
            self.row.last_checked_at = timezone.now()
            self.row.save(update_fields=[
                'simc_path', 'current_version', 'last_error', 'is_updating',
                'update_progress', 'update_status', 'last_checked_at'
            ])
            self.stdout.write(self.style.WARNING(msg))
            return

        try:
            result, output = self._probe_binary()
        except subprocess.TimeoutExpired as exc:
            msg = f'二进制验证超时: {exc}'
            self._set_status(progress=0, status='二进制验证超时', error=msg, updating=False)
            self.stdout.write(self.style.WARNING(msg))
            return
        except Exception as exc:
            msg = f'二进制验证失败: {exc}'
            self._set_status(progress=0, status='二进制验证失败', error=msg, updating=False)
            self.stdout.write(self.style.WARNING(msg))
            return
        if result.returncode != 0 or 'SimulationCraft' not in output:
            msg = f'二进制验证失败: {output[:500]}'
            self._set_status(progress=0, status='二进制验证失败', error=msg, updating=False)
            self.stdout.write(self.style.WARNING(msg))
            return
        display_version = self._parse_version(output) or '未知'
        self.row.simc_path = self._stored_simc_path()
        self.row.last_error = ''
        self.row.is_updating = False
        self.row.update_progress = 100
        self.row.update_status = f'二进制可用 {display_version}'
        now = timezone.now()
        self.row.last_checked_at = now
        self.row.save(update_fields=['simc_path', 'last_error', 'is_updating', 'update_progress', 'update_status', 'last_checked_at'])
        self.stdout.write(f'二进制版本: {display_version}')
        self.stdout.write(f'路径: {self.simc_binary_path}')

    def _sync_default_template(self):
        cfg = getattr(settings, 'SIMC_CONFIG', {}) or {}
        template_path = str(cfg.get('simc_template') or 'LMonitor/simc_template.txt')
        if not os.path.isabs(template_path):
            template_path = os.path.join(settings.BASE_DIR, template_path)
        if not os.path.isfile(template_path):
            self.stdout.write(self.style.WARNING(f'默认模板文件不存在，跳过同步: {template_path}'))
            return

        with open(template_path, encoding='utf-8') as f:
            content = f.read()
        if not content.strip():
            self.stdout.write(self.style.WARNING(f'默认模板文件为空，跳过同步: {template_path}'))
            return

        template, created = SimcContentTemplate.objects.update_or_create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='default',
            name='基础模板 default',
            defaults={
                'class_name': '',
                'content': content,
                'is_active': True,
                'is_selectable': True,
            }
        )
        SimcContentTemplate.objects.filter(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
        ).exclude(id=template.id).update(is_active=False, is_selectable=False)
        action = '创建' if created else '更新'
        self.stdout.write(self.style.SUCCESS(f'{action}默认 SimC 基础模板: spec=default'))

    def _sync_default_apl(self, git_hash):
        source_dir = os.path.join(self.simc_source_dir, 'ActionPriorityLists', 'default')
        call_command('import_simc_apl', source_dir=source_dir, sync_version=git_hash, strict=True)

    def _resolve_wow_build(self, override=''):
        """Resolve one authoritative build: CLI, config, then agreeing current DB state."""
        explicit = str(override or '').strip()
        if explicit:
            return explicit
        cfg = getattr(settings, 'SIMC_CONFIG', {}) or {}
        configured = str(cfg.get('wow_build') or '').strip()
        if configured:
            return configured
        candidates = set(WowSpellSnapshotState.objects.filter(
            branch='wow').exclude(snapshot_build='').values_list('snapshot_build', flat=True))
        candidates.update(WowTalentVersion.objects.filter(
            is_active=True, is_default_simulator=True).exclude(current_build='')
                          .values_list('current_build', flat=True))
        candidates = {str(value).strip() for value in candidates if str(value).strip()}
        if len(candidates) == 1:
            return candidates.pop()
        detail = '未找到' if not candidates else f'存在多个候选: {sorted(candidates)}'
        raise CommandError(f'无法唯一解析 wow_build（{detail}）；请传 --wow-build')

    def _validate_system_apl(self, apl, baseline, git_hash, binary_path, binary_revision):
        """Run one staged upstream APL against its same-revision MID1 baseline."""
        class_name, short_spec = canonical_simc_spec_identity(apl.spec)
        profile = type('SystemAplProfile', (), {
            'id': 0, 'user_id': 0, 'spec': short_spec, 'class_name': class_name,
            'player_config_mode': 'manual_equipment',
            'player_equipment': baseline.content, 'talent': '',
            'battlenet_region': '', 'battlenet_realm': '', 'battlenet_character': '',
            'gear_crit': 0, 'gear_haste': 0, 'gear_mastery': 0,
            'gear_versatility': 0,
        })()
        validation_input = SimcComposer(0).compose_validation_input(profile, apl.content)
        validator = RestrictedSimcValidator(
            binary_path, catalog_revision=git_hash,
            binary_revision=binary_revision,
            temp_root=getattr(settings, 'SIMC_APL_VALIDATION_TEMP_ROOT', None),
        )
        context = SimcComposer.validation_context(
            profile, catalog_revision=git_hash, binary_revision=binary_revision,
            validation_input=validation_input,
        )
        return validate_payload(
            apl.content, mode='both', authoritative_validator=validator,
            validation_context=context,
        )

    @staticmethod
    def _validation_baseline_for_spec(spec, baselines):
        exact = baselines.get(spec)
        if exact is not None:
            return exact
        class_name, short_spec = canonical_simc_spec_identity(spec)
        source = next((row for row in baselines.values()
                       if row.class_name == class_name), None)
        if source is None:
            return None
        content = re.sub(r'(?m)^spec\s*=.*$', f'spec={short_spec}', source.content, count=1)
        content = re.sub(r'(?m)^talents\s*=.*\n?', '', content)
        return type('ValidationBaseline', (), {
            'content': content, 'spec': spec, 'class_name': class_name,
        })()

    def _publish_system_apl_corpus(self, git_hash, wow_build, binary_path, binary_revision):
        apls = list(SimcApl.objects.filter(
            source=SimcApl.SOURCE_SIMC_UPSTREAM, is_system=True,
            owner_user_id=None, is_active=True, sync_version=git_hash,
        ).order_by('spec'))
        if not apls:
            raise CommandError('本次 revision 没有可发布的系统 APL')
        baselines = {
            row.spec: row for row in SimcContentTemplate.objects.filter(
                template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER,
                source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
                is_active=True, sync_version=git_hash,
            )
        }
        failures = []
        results = []
        for apl in apls:
            baseline = self._validation_baseline_for_spec(apl.spec, baselines)
            if baseline is None:
                failures.append(f'{apl.spec}: 缺少同 revision 默认玩家基线')
                continue
            try:
                result = self._validate_system_apl(
                    apl, baseline, git_hash, binary_path, binary_revision)
            except (ValueError, TypeError, AttributeError, OSError) as exc:
                failures.append(f'{apl.spec}: {exc}')
                continue
            if not (result.get('structural_valid') and result.get('authoritative_valid')):
                detail = result.get('authoritative_error', {}).get('code') or 'SimC rejected APL'
                failures.append(f'{apl.spec}: {detail}')
            results.append((apl, result))
        if failures:
            raise CommandError('系统 APL corpus 权威校验失败: ' + '; '.join(failures[:10]))

        validated_at = timezone.now()
        for apl, result in results:
            apl.validation_status = SimcApl.VALIDATION_VALID
            apl.validated_content_hash = content_hash(apl.content)
            apl.validation_revision = git_hash
            apl.validation_game_build = wow_build
            apl.validation_stale_reason = ''
            apl.validation_diagnostics = result.get('diagnostics') or []
            apl.validated_at = validated_at
            apl.is_selectable = True
            apl.save(update_fields=[
                'validation_status', 'validated_content_hash', 'validation_revision',
                'validation_game_build', 'validation_stale_reason',
                'validation_diagnostics', 'validated_at', 'is_selectable',
            ])

    def _sync_generated_inputs(self, wow_build_override=None, git_hash=None,
                               binary_path=None, binary_revision=None):
        git_hash = git_hash or self._get_git_hash()
        if not re.fullmatch(r'[0-9a-fA-F]{40}', str(git_hash or '')):
            raise CommandError('无法取得有效的 40 位 hexadecimal SimC git SHA')
        override = (self.wow_build_override if wow_build_override is None and
                    hasattr(self, 'wow_build_override') else wow_build_override)
        wow_build = self._resolve_wow_build(override)
        binary_path = binary_path or getattr(self, 'simc_binary_path', '/tmp/simc')
        binary_revision = binary_revision or (
            getattr(self.row, 'current_version', '') if hasattr(self, 'row') else git_hash)
        if binary_revision != git_hash:
            raise CommandError('SimC binary revision 与待发布 corpus revision 不一致')
        if hasattr(self, 'row'):
            self._set_status(progress=95, status='同步默认模板和 APL', error='', updating=True)
        with transaction.atomic():
            self._sync_default_template()
            player_source_dir = os.path.join(self.simc_source_dir, 'profiles', 'MID1')
            call_command('import_simc_player_templates', source_dir=player_source_dir,
                         sync_version=git_hash)
            self._sync_default_apl(git_hash)
            manifest_path = self._export_runtime_manifest(
                git_hash, wow_build, binary_path=binary_path)
            try:
                call_command(
                    'sync_simc_apl_symbols', simc_revision=git_hash, wow_build=wow_build,
                    runtime_manifest=manifest_path,
                )
                self._publish_system_apl_corpus(
                    git_hash, wow_build, binary_path, binary_revision)
            finally:
                try:
                    os.unlink(manifest_path)
                except FileNotFoundError:
                    pass


    def _export_runtime_manifest(self, git_hash, wow_build, binary_path=None):
        handle = tempfile.NamedTemporaryFile(prefix='lmonitor-simc-apl-', suffix='.json', delete=False)
        path = handle.name
        handle.close()
        generated_profiles = []
        try:
            profile_dir = os.path.join(self.simc_source_dir, 'profiles', 'MID1')
            profiles = sorted(
                os.path.join(profile_dir, name)
                for name in os.listdir(profile_dir)
                if name.endswith('.simc') and os.path.isfile(os.path.join(profile_dir, name))
            ) if os.path.isdir(profile_dir) else []
            if not profiles:
                self._fail(
                    'SimC runtime manifest 缺少 actor profile',
                    f'未找到可初始化的 MID1 profile: {profile_dir}',
                    progress=92,
                )
            baseline_rows = list(SimcContentTemplate.objects.filter(
                template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER,
                source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
                is_active=True, sync_version=git_hash,
            ))
            baselines = {row.spec: row for row in baseline_rows}
            available_specs = set(baselines)
            required_specs = set(SimcApl.objects.filter(
                source=SimcApl.SOURCE_SIMC_UPSTREAM, is_system=True,
                is_active=True, sync_version=git_hash,
            ).values_list('spec', flat=True))
            for spec in sorted(required_specs - available_specs):
                baseline = self._validation_baseline_for_spec(spec, baselines)
                if baseline is None:
                    continue
                generated = tempfile.NamedTemporaryFile(
                    mode='w', prefix='lmonitor-simc-manifest-profile-',
                    suffix='.simc', delete=False, encoding='utf-8')
                with generated:
                    generated.write(baseline.content)
                generated_profiles.append(generated.name)
            profiles.extend(generated_profiles)
            self._run(
                [binary_path or self.simc_binary_path, *profiles,
                 f'apl_metadata_export={path}',
                 f'apl_metadata_revision={git_hash}',
                 f'apl_metadata_game_build={wow_build}'],
                cwd=self.simc_source_dir, timeout=120,
                status='导出 SimC runtime APL manifest', progress=92,
            )
            if not os.path.isfile(path):
                self._fail('SimC runtime manifest 缺失', 'SimC 导出未生成 manifest', progress=92)
            with open(path, encoding='utf-8') as manifest:
                payload = json.load(manifest)
            if payload.get('simc_revision') != git_hash or payload.get('game_build') != wow_build:
                self._fail('SimC runtime manifest 不匹配', 'manifest revision/build 与本次发布不一致', progress=92)
            return path
        except Exception:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            raise
        finally:
            for generated_profile in generated_profiles:
                try:
                    os.unlink(generated_profile)
                except FileNotFoundError:
                    pass

    def _preserve_tracked_changes_before_pull(self):
        """Commit tracked source edits only; leave generated and credential files untracked."""
        try:
            result = subprocess.run(
                ['git', 'status', '--porcelain', '--untracked-files=no'],
                cwd=self.simc_source_dir,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except Exception as exc:
            self._fail('检查本地 SimC 源码改动失败', str(exc), progress=7)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or '').strip()
            self._fail('检查本地 SimC 源码改动失败', detail or 'git status 失败', progress=7)
        if not (result.stdout or '').strip():
            return False

        timestamp = datetime.now(datetime_timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        self._run(
            ['git', 'add', '-u'], cwd=self.simc_source_dir, timeout=30,
            status='保存本地 SimC 源码改动', progress=8,
        )
        self._run(
            ['git', 'commit', '-m', f'auto-save local changes before upstream sync ({timestamp})'],
            cwd=self.simc_source_dir, timeout=60,
            status='提交本地 SimC 源码改动', progress=9,
        )
        return True

    def _pull_rebase(self):
        self._set_status(progress=10, status='拉取 SimC 源码', error='', updating=True)
        self.stdout.write('拉取 SimC 源码')
        try:
            result = subprocess.run(
                ['git', 'pull', '--rebase'], cwd=self.simc_source_dir,
                capture_output=True, text=True, timeout=300,
            )
        except subprocess.TimeoutExpired as exc:
            self._fail('拉取 SimC 源码超时', f'拉取 SimC 源码超时: {exc}', progress=10)
        except Exception as exc:
            self._fail('拉取 SimC 源码失败', f'拉取 SimC 源码失败: {exc}', progress=10)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or '').strip()[-1000:]
            if 'CONFLICT' in detail or 'could not apply' in detail:
                detail = f'{detail}\n本地自动保存提交未丢失；请在 {self.simc_source_dir} 解决冲突后执行 git rebase --continue，或执行 git rebase --abort。'
            self._fail('拉取 SimC 源码失败', detail or 'git pull --rebase 失败', progress=10)
        return result

    def _apply_local_patches(self):
        """Apply repository-owned SimC patches, skipping patches already present."""
        cfg = getattr(settings, 'SIMC_CONFIG', {}) or {}
        patch_dir = str(
            cfg.get('simc_patch_dir') or os.path.join(settings.BASE_DIR, 'simc_patches')
        )
        if not os.path.isdir(patch_dir):
            return False

        changed = False
        for patch_name in sorted(name for name in os.listdir(patch_dir) if name.endswith('.patch')):
            patch_path = os.path.join(patch_dir, patch_name)
            check = subprocess.run(
                ['git', 'apply', '--check', patch_path],
                cwd=self.simc_source_dir, capture_output=True, text=True, timeout=30,
            )
            if check.returncode == 0:
                applied = subprocess.run(
                    ['git', 'apply', patch_path],
                    cwd=self.simc_source_dir, capture_output=True, text=True, timeout=30,
                )
                if applied.returncode != 0:
                    detail = (applied.stderr or applied.stdout or '').strip()[-1000:]
                    self._fail(
                        f'应用 SimC 补丁失败 {patch_name}',
                        detail or f'git apply 失败: {patch_name}',
                        progress=20,
                    )
                self.stdout.write(f'应用 SimC 补丁 {patch_name}')
                changed = True
                continue

            reverse = subprocess.run(
                ['git', 'apply', '--reverse', '--check', patch_path],
                cwd=self.simc_source_dir, capture_output=True, text=True, timeout=30,
            )
            if reverse.returncode == 0:
                continue
            detail = (check.stderr or check.stdout or '').strip()[-1000:]
            self._fail(
                f'SimC 补丁冲突 {patch_name}',
                detail or f'无法应用或识别 SimC 补丁: {patch_name}',
                progress=20,
            )
        return changed

    def _apply_patches_only(self, threads=2):
        changed = self._apply_local_patches()
        if not changed and not self._binary_needs_patch_rebuild():
            self.stdout.write('SimC 本地补丁已存在，无需重新编译')
            return False
        self._update_binary(do_pull=False, threads=threads, apply_patches=False)
        return True

    def _binary_needs_patch_rebuild(self):
        """Recover from a missing/stale/broken binary after a prior patch application."""
        if not os.path.isfile(self.simc_binary_path):
            return True
        cfg = getattr(settings, 'SIMC_CONFIG', {}) or {}
        patch_dir = str(
            cfg.get('simc_patch_dir') or os.path.join(settings.BASE_DIR, 'simc_patches')
        )
        binary_mtime = os.path.getmtime(self.simc_binary_path)
        if os.path.isdir(patch_dir):
            for patch_name in os.listdir(patch_dir):
                if patch_name.endswith('.patch') and os.path.getmtime(
                    os.path.join(patch_dir, patch_name)
                ) > binary_mtime:
                    return True
        try:
            result, output = self._probe_binary(self.simc_binary_path)
        except (OSError, subprocess.SubprocessError):
            return True
        return result.returncode != 0 or 'simulationcraft' not in str(output or '').lower()

    def _update_binary(self, do_pull=True, threads=2, apply_patches=True):
        self._set_status(progress=1, status='准备更新 SimC', error='', updating=True)
        try:
            if not os.path.isdir(self.simc_source_dir):
                self._fail('源码目录不存在', f'SimC 源码目录不存在: {self.simc_source_dir}', progress=0)

            if do_pull:
                self._preserve_tracked_changes_before_pull()
                result = self._pull_rebase()
                self.stdout.write((result.stdout or '').strip())

            if apply_patches:
                self._apply_local_patches()

            git_hash = self._get_git_hash()
            if not re.fullmatch(r'[0-9a-fA-F]{40}', str(git_hash or '')):
                self._fail('源码版本无效', '无法取得有效的 40 位 hexadecimal SimC git SHA', progress=1)
            version = self._get_git_version()
            self.stdout.write(f'编译版本: {version}')
            os.makedirs(self.simc_build_dir, exist_ok=True)

            self._run(
                ['cmake', '..', '-DBUILD_GUI=OFF', '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_CXX_FLAGS_RELEASE=-O1 -DNDEBUG', '-G', 'Ninja'],
                cwd=self.simc_build_dir,
                timeout=120,
                status='CMake 配置 SimC',
                progress=30,
            )
            self._run(
                ['ninja', f'-j{threads}'],
                cwd=self.simc_build_dir,
                timeout=1800,
                status=f'编译 SimC (-j{threads})',
                progress=60,
            )

            self._set_status(progress=90, status='验证 SimC 二进制', error='', updating=True)
            if not os.path.isfile(self.simc_binary_path):
                self._fail('编译产物不存在', f'编译产物不存在: {self.simc_binary_path}', progress=90)
            result, binary_output = self._probe_binary()
            if result.returncode != 0 or 'SimulationCraft' not in binary_output:
                self._fail('二进制验证失败', f'二进制验证失败: {binary_output[:500]}', progress=90)

            parsed_version = self._parse_version(binary_output)
            if parsed_version:
                version = f'{parsed_version}-{git_hash}'

            self._sync_generated_inputs(
                git_hash=git_hash, binary_path=self.simc_binary_path,
                binary_revision=git_hash)

            self.row.simc_path = self._stored_simc_path()
            self.row.current_version = git_hash
            self.row.latest_version = git_hash
            self.row.last_error = ''
            self.row.is_updating = False
            self.row.update_progress = 100
            self.row.update_status = f'编译完成 {version}'
            now = timezone.now()
            self.row.last_checked_at = now
            self.row.last_updated_at = now
            self.row.save(update_fields=[
                'simc_path', 'current_version', 'latest_version', 'last_error', 'is_updating',
                'update_progress', 'update_status', 'last_checked_at', 'last_updated_at'
            ])
            self.stdout.write(self.style.SUCCESS(f'编译完成！版本: {version}, 路径: {self.simc_binary_path}'))
        except CommandError:
            raise
        except Exception as exc:
            self._fail('SimC 更新失败', str(exc), progress=0)

    @staticmethod
    def _revision_matches_git_hash(current_version, git_hash):
        """Accept canonical SHA or one legacy version string ending in its git prefix."""
        current = str(current_version or '').strip().lower()
        revision = str(git_hash or '').strip().lower()
        if not re.fullmatch(r'[0-9a-f]{40}', revision):
            return False
        if current == revision:
            return True
        match = re.search(r'(?:^|[-.])([0-9a-f]{7,39})$', current)
        return bool(match and revision.startswith(match.group(1)))

    def _get_git_hash(self):
        try:
            # Revision-keyed APL and symbol facts require the collision-resistant
            # canonical commit id, not a presentation abbreviation.
            r = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=self.simc_source_dir, capture_output=True, text=True, timeout=10)
            return r.stdout.strip() if r.returncode == 0 else ''
        except Exception:
            return ''

    def _get_git_version(self):
        git_hash = self._get_git_hash()
        try:
            cmake_path = os.path.join(self.simc_source_dir, 'CMakeLists.txt')
            version = git_hash or 'unknown'
            if os.path.isfile(cmake_path):
                with open(cmake_path, encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                major = re.search(r'SC_MAJOR_VERSION\s*=\s*"?(\d+)"?', content)
                minor = re.search(r'SC_MINOR_VERSION\s*=\s*"?(\d+)"?', content)
                if major and minor:
                    version = f'{major.group(1)}.{minor.group(1)}-{git_hash}' if git_hash else f'{major.group(1)}.{minor.group(1)}'
            return version
        except Exception:
            return git_hash or 'unknown'

    def _parse_version(self, text):
        m = re.search(r'SimulationCraft\s+([^\s]+)', text or '')
        return m.group(1) if m else None
