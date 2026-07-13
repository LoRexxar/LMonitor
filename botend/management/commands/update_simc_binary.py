#!/usr/bin/env python
# encoding: utf-8
"""
管理命令：编译/更新服务器端 SimC 二进制
用法：
  python manage.py update_simc_binary                 # 自动保存 tracked 改动 + git pull --rebase + 编译
  python manage.py update_simc_binary --no-pull       # 仅编译（不拉代码）
  python manage.py update_simc_binary --check         # 仅检查版本
  python manage.py update_simc_binary --threads 1     # 降低编译并行度
"""
import os
import re
import subprocess
from datetime import datetime, timezone as datetime_timezone

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from botend.models import SimcBackendBinary, SimcContentTemplate


DEFAULT_SIMC_SOURCE_DIR = '/home/lighthouse/simc'


class Command(BaseCommand):
    help = '在服务器上编译/更新 SimulationCraft 二进制'

    def add_arguments(self, parser):
        parser.add_argument('--no-pull', action='store_true', help='不执行 git pull，仅编译当前代码')
        parser.add_argument('--check', action='store_true', help='仅检查当前版本，不执行编译')
        parser.add_argument('--sync-inputs-only', action='store_true', help='仅同步默认模板和默认 APL，不执行拉取/编译')
        parser.add_argument('--threads', type=int, default=2, help='编译并行度（默认 2，内存不足时降低）')

    def handle(self, *args, **options):
        self.platform = 'linux64'
        self.simc_source_dir, self.simc_build_dir, self.simc_binary_path = self._resolve_paths()
        self.row = self._get_row()

        if options['check']:
            self._check_version()
            return
        if options['sync_inputs_only']:
            self._sync_generated_inputs()
            self._set_status(progress=100, status='默认模板和 APL 同步完成', error='', updating=False)
            return

        self._update_binary(do_pull=not options['no_pull'], threads=max(1, int(options['threads'] or 1)))

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
                'simc_path': self.simc_binary_path,
                'current_version': '',
                'latest_version': '',
                'auto_update': True,
            }
        )
        return row

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

    def _check_version(self):
        if not os.path.isfile(self.simc_binary_path):
            msg = f'二进制不存在: {self.simc_binary_path}'
            self.row.simc_path = self.simc_binary_path
            self.row.current_version = ''
            self.row.last_error = msg
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

        result = subprocess.run([self.simc_binary_path, '--help'], capture_output=True, text=True, timeout=10)
        output = result.stdout + result.stderr
        version = self._parse_version(output) or self.row.current_version or '未知'
        self.row.simc_path = self.simc_binary_path
        self.row.current_version = version
        self.row.last_error = ''
        self.row.is_updating = False
        self.row.update_progress = 100
        self.row.update_status = f'当前版本 {version}'
        now = timezone.now()
        self.row.last_checked_at = now
        self.row.save(update_fields=['simc_path', 'current_version', 'last_error', 'is_updating', 'update_progress', 'update_status', 'last_checked_at'])
        self.stdout.write(f'当前版本: {version}')
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

        _, created = SimcContentTemplate.objects.update_or_create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='default',
            name='基础模板 default',
            defaults={
                'class_name': '',
                'content': content,
                'is_active': True,
                'is_selectable': False,
            }
        )
        action = '创建' if created else '更新'
        self.stdout.write(self.style.SUCCESS(f'{action}默认 SimC 基础模板: spec=default'))

    def _sync_default_apl(self):
        source_dir = os.path.join(self.simc_source_dir, 'ActionPriorityLists', 'default')
        call_command('import_simc_apl', source_dir=source_dir)

    def _sync_generated_inputs(self):
        self._set_status(progress=95, status='同步默认模板和 APL', error='', updating=True)
        self._sync_default_template()
        self._sync_default_apl()

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
                capture_output=True, text=True, timeout=120,
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

    def _update_binary(self, do_pull=True, threads=2):
        self._set_status(progress=1, status='准备更新 SimC', error='', updating=True)
        try:
            if not os.path.isdir(self.simc_source_dir):
                self._fail('源码目录不存在', f'SimC 源码目录不存在: {self.simc_source_dir}', progress=0)

            if do_pull:
                self._preserve_tracked_changes_before_pull()
                result = self._pull_rebase()
                self.stdout.write((result.stdout or '').strip())

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
            result = subprocess.run([self.simc_binary_path, '--help'], capture_output=True, text=True, timeout=10)
            binary_output = result.stdout + result.stderr
            if result.returncode != 0 or 'SimulationCraft' not in binary_output:
                self._fail('二进制验证失败', f'二进制验证失败: {binary_output[:500]}', progress=90)

            parsed_version = self._parse_version(binary_output)
            if parsed_version:
                version = f'{parsed_version}-{self._get_git_hash()}' if self._get_git_hash() else parsed_version

            self._sync_generated_inputs()

            self.row.simc_path = self.simc_binary_path
            self.row.current_version = version
            self.row.latest_version = version
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

    def _get_git_hash(self):
        try:
            r = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=self.simc_source_dir, capture_output=True, text=True, timeout=10)
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
