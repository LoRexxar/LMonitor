#!/usr/bin/env python
# encoding: utf-8
"""
管理命令：从 SimC 源码的 ActionPriorityLists 导入默认 APL 到数据库
用法：
  python manage.py import_simc_apl                        # 导入全部
  python manage.py import_simc_apl --source-dir /path     # 指定源码目录
  python manage.py import_simc_apl --dry-run              # 仅预览不写入
"""
import os
import re
import logging

from dataclasses import dataclass

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


from botend.models import SimcApl
from botend.services.simc_apl.validation import validate_document


logger = logging.getLogger(__name__)

DEFAULT_SOURCE_DIR = '/home/lighthouse/simc/ActionPriorityLists/default'

# 文件名 → (class_name, spec) 映射
# 文件格式: {class}_{spec}.simc
KNOWN_SPECS = {
    'death_knight': ['blood', 'frost', 'unholy'],
    'deathknight': ['blood', 'frost', 'unholy'],
    'demon_hunter': ['havoc', 'vengeance'],
    'demonhunter': ['devourer', 'havoc', 'vengeance'],
    'druid': ['balance', 'feral', 'guardian', 'restoration'],
    'evoker': ['devastation', 'preservation', 'augmentation'],
    'hunter': ['beast_mastery', 'marksmanship', 'survival'],
    'mage': ['arcane', 'fire', 'frost'],
    'monk': ['brewmaster', 'mistweaver', 'windwalker'],
    'paladin': ['holy', 'protection', 'retribution'],
    'priest': ['discipline', 'holy', 'shadow'],
    'rogue': ['assassination', 'outlaw', 'subtlety'],
    'shaman': ['elemental', 'enhancement', 'restoration'],
    'warlock': ['affliction', 'demonology', 'destruction'],
    'warrior': ['arms', 'fury', 'protection'],
}


@dataclass(frozen=True)
class PreparedApl:
    """Fully validated, in-memory representation of one import candidate."""

    class_name: str
    spec_key: str
    content: str


class Command(BaseCommand):
    help = '从 SimC 源码导入默认 APL 到数据库'

    def add_arguments(self, parser):
        parser.add_argument(
            '--source-dir', default=DEFAULT_SOURCE_DIR,
            help=f'APL 文件目录（默认: {DEFAULT_SOURCE_DIR}）'
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='仅预览，不写入数据库'
        )
        parser.add_argument(
            '--sync-version', default='',
            help='APL 对应的真实 SimC git revision'
        )
        parser.add_argument(
            '--strict', action='store_true',
            help='任一目录、文件名、读取或空内容错误均失败并回滚整个导入'
        )

    @transaction.atomic
    def handle(self, *args, **options):
        source_dir = options['source_dir']
        dry_run = options['dry_run']
        self.sync_version = str(options.get('sync_version') or '').strip()
        strict = bool(options.get('strict'))
        self.strict = strict
        if not os.path.isdir(source_dir):
            message = f'APL 目录不存在: {source_dir}'
            self.stdout.write(self.style.ERROR(message))
            if strict:
                raise CommandError(message)
            return
        if strict and not re.fullmatch(r'[0-9a-fA-F]{40}', self.sync_version):
            raise CommandError('严格导入要求 --sync-version 为 40 位 hexadecimal SimC git SHA')

        # 扫描 .simc 文件
        files = sorted(f for f in os.listdir(source_dir) if f.endswith('.simc'))
        self.stdout.write(f'发现 {len(files)} 个 APL 文件')
        if strict and not files:
            raise CommandError(f'APL 目录中没有 .simc 文件: {source_dir}')

        imported = 0
        skipped = 0
        errors = 0
        self.imported_specs = set()

        prepared_apls = []
        for fname in files:
            result, prepared = self._prepare_file(source_dir, fname)
            if result == 'ok':
                imported += 1
                prepared_apls.append(prepared)
                self.imported_specs.add(prepared.spec_key)
                # Non-strict mode intentionally retains its historical
                # file-at-a-time write behaviour.
                if not strict:
                    self._persist_prepared(prepared, dry_run)
            elif result == 'skip':
                skipped += 1
            else:
                errors += 1

        if strict and (skipped or errors or not imported):
            raise CommandError(
                f'严格 APL 导入校验失败: {imported} 成功, {skipped} 跳过, {errors} 错误'
            )

        # Strict import is genuinely two-phase: no ORM operation is reached
        # until the complete corpus has been validated and staged in memory.
        if strict:
            for prepared in prepared_apls:
                self._persist_prepared(prepared, dry_run)

        if strict and not dry_run:
            # Scope owned by this importer: active, global system rows sourced
            # from upstream SimC. User/manual APLs are deliberately excluded.
            missing = SimcApl.objects.filter(
                source=SimcApl.SOURCE_SIMC_UPSTREAM, is_system=True,
                owner_user_id=None, is_active=True,
            ).exclude(spec__in=self.imported_specs)
            for apl in missing:
                apl.is_active = False
                apl.is_selectable = False
                apl.save(update_fields=['is_active', 'is_selectable'])

        action = '预览' if dry_run else '导入'
        self.stdout.write(self.style.SUCCESS(
            f'{action}完成: {imported} 成功, {skipped} 跳过, {errors} 错误'
        ))

    def _parse_apl_filename(self, base):
        """按已知专精后缀解析 APL 文件名。

        SimC 默认 APL 文件名形如 class_spec.simc，但部分 spec 自身包含下划线
        （例如 beast_mastery），所以必须优先匹配 KNOWN_SPECS 中的完整 spec 后缀。
        """
        for class_name, specs in KNOWN_SPECS.items():
            prefix = f'{class_name}_'
            if not base.startswith(prefix):
                continue
            suffix = base[len(prefix):]
            for spec in sorted(specs, key=len, reverse=True):
                if suffix == spec:
                    return class_name, spec
            return class_name, suffix

        parts = base.rsplit('_', 1)
        if len(parts) == 2:
            return parts[0], parts[1]
        return None, None

    def _process_file(self, source_dir, fname, dry_run):
        """Backward-compatible single-file path used by non-corpus callers."""
        result, prepared = self._prepare_file(source_dir, fname)
        if result != 'ok':
            return result
        if hasattr(self, 'imported_specs'):
            self.imported_specs.add(prepared.spec_key)
        self._persist_prepared(prepared, dry_run)
        return 'ok'

    def _prepare_file(self, source_dir, fname):
        """Read and validate one APL file without touching the ORM."""
        strict = getattr(self, 'strict', False)
        base = fname[:-5]
        class_name, spec = self._parse_apl_filename(base)
        if not class_name or not spec:
            self.stdout.write(self.style.WARNING(
                f'无法解析文件名: {fname}，跳过'
            ))
            return ('error' if strict else 'skip'), None

        known_specs = KNOWN_SPECS.get(class_name, [])
        if spec not in known_specs:
            self.stdout.write(self.style.WARNING(
                f'未知专精: {class_name}_{spec}，仍会导入'
            ))
            if strict:
                return 'error', None

        filepath = os.path.join(source_dir, fname)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            self.stdout.write(self.style.ERROR(
                f'读取失败 {fname}: {e}'
            ))
            return 'error', None

        if not content.strip():
            self.stdout.write(self.style.WARNING(
                f'空文件: {fname}，跳过'
            ))
            return ('error' if strict else 'skip'), None

        spec_key = f'{class_name}_{spec}'
        if spec_key == 'warrior_fury':
            content = content.replace('talent.slayers_dominance', 'hero_tree.slayer')
            content = content.replace('talent.lightning_strikes', 'hero_tree.mountain_thane')
        elif spec_key == 'demonhunter_havoc':
            # Upstream revision 62ababb contains one accidental C-style
            # conjunction. SimC APL uses a single '&'; normalize that exact
            # source typo before structural validation and persistence.
            content = content.replace(
                'cooldown.eye_beam.remains>5&&equipped.algethar_puzzle_box',
                'cooldown.eye_beam.remains>5&equipped.algethar_puzzle_box',
            )

        if strict:
            _, _, diagnostics = validate_document(content)
            if diagnostics:
                self.stdout.write(self.style.ERROR(
                    f'APL 校验失败 {fname}: {len(diagnostics)} 个错误'))
                return 'error', None

        return 'ok', PreparedApl(
            class_name=class_name,
            spec_key=spec_key,
            content=content,
        )

    def _persist_prepared(self, prepared, dry_run):
        """Write (or preview) a previously prepared APL DTO."""
        if dry_run:
            lines = len(prepared.content.strip().splitlines())
            self.stdout.write(f'  [DRY] {prepared.spec_key}: {lines} 行')
            return

        _, created = SimcApl.objects.update_or_create(
            source='simc_upstream',
            spec=prepared.spec_key,
            is_system=True,
            owner_user_id=None,
            defaults={
                'name': f'默认APL {prepared.spec_key}',
                'class_name': prepared.class_name,
                'content': prepared.content,
                'is_active': True,
                'is_selectable': False,
                'sync_version': getattr(self, 'sync_version', ''),
                'validation_status': SimcApl.VALIDATION_DRAFT,
                'validated_content_hash': '',
                'validation_revision': '',
                'validation_game_build': '',
                'validation_stale_reason': 'authoritative_validation_required',
                'validation_diagnostics': [],
                'validated_at': None,
            }
        )
        status = '新建' if created else '更新'
        self.stdout.write(f'  {status}: {prepared.spec_key}')
