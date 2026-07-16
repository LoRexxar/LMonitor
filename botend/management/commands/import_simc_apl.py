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

from django.core.management.base import BaseCommand

from botend.models import SimcApl

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

    def handle(self, *args, **options):
        source_dir = options['source_dir']
        dry_run = options['dry_run']

        if not os.path.isdir(source_dir):
            self.stdout.write(self.style.ERROR(
                f'APL 目录不存在: {source_dir}'
            ))
            return

        # 扫描 .simc 文件
        files = sorted(f for f in os.listdir(source_dir) if f.endswith('.simc'))
        self.stdout.write(f'发现 {len(files)} 个 APL 文件')

        imported = 0
        skipped = 0
        errors = 0

        for fname in files:
            result = self._process_file(source_dir, fname, dry_run)
            if result == 'ok':
                imported += 1
            elif result == 'skip':
                skipped += 1
            else:
                errors += 1

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
        """处理单个 APL 文件"""
        # 解析文件名: warrior_fury.simc → class=warrior, spec=fury
        # 注意 spec 可能包含下划线，例如 hunter_beast_mastery.simc，不能简单 rsplit('_', 1)。
        base = fname[:-5]  # 去掉 .simc
        class_name, spec = self._parse_apl_filename(base)
        if not class_name or not spec:
            self.stdout.write(self.style.WARNING(
                f'无法解析文件名: {fname}，跳过'
            ))
            return 'skip'

        # 验证是否已知专精
        known_specs = KNOWN_SPECS.get(class_name, [])
        if spec not in known_specs:
            self.stdout.write(self.style.WARNING(
                f'未知专精: {class_name}_{spec}，仍会导入'
            ))

        # 读取文件
        filepath = os.path.join(source_dir, fname)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            self.stdout.write(self.style.ERROR(
                f'读取失败 {fname}: {e}'
            ))
            return 'error'

        if not content.strip():
            self.stdout.write(self.style.WARNING(
                f'空文件: {fname}，跳过'
            ))
            return 'skip'

        spec_key = f'{class_name}_{spec}'

        if dry_run:
            lines = len(content.strip().splitlines())
            self.stdout.write(f'  [DRY] {spec_key}: {lines} 行')
            return 'ok'

        # 写入 SimcApl 表；默认 APL 来源固定为 SimC 源码同步。
        _, created = SimcApl.objects.update_or_create(
            source='simc_upstream',
            spec=spec_key,
            is_system=True,
            owner_user_id=None,
            defaults={
                'name': f'默认APL {spec_key}',
                'class_name': class_name,
                'content': content,
                'is_active': True,
                'is_selectable': True,
            }
        )
        status = '新建' if created else '更新'
        self.stdout.write(f'  {status}: {spec_key}')
        return 'ok'
