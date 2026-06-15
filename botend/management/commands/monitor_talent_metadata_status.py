# -*- coding: utf-8 -*-

import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from botend.constants.wow import CLASS_SPEC_MAP
from botend.models import WowTalentNodeMetadata


class Command(BaseCommand):
    help = '持续输出天赋元数据覆盖率到 talent_metadata_status.md'

    def add_arguments(self, parser):
        parser.add_argument('--interval', type=int, default=60, help='刷新间隔（秒）')
        parser.add_argument('--once', action='store_true', help='只输出一次就退出')
        parser.add_argument('--output', default='talent_metadata_status.md', help='输出文件路径（相对项目根目录）')

    def handle(self, *args, **options):
        interval = max(10, int(options.get('interval') or 60))
        once = bool(options.get('once'))
        output = (options.get('output') or 'talent_metadata_status.md').strip()

        while True:
            self._write_status(output)
            if once:
                return
            time.sleep(interval)

    def _write_status(self, output):
        now = timezone.localtime()
        lines = []
        lines.append(f'# Talent metadata status')
        lines.append('')
        lines.append(f'- Updated: `{now:%Y-%m-%d %H:%M:%S}`')
        lines.append('')
        lines.append('| Class | Spec | Total | Coords | Parents | Icons | Names | Display |')
        lines.append('|---|---:|---:|---:|---:|---:|---:|---:|')

        missing = []
        for class_name, specs in CLASS_SPEC_MAP.items():
            for spec_name in specs:
                qs = WowTalentNodeMetadata.objects.filter(
                    class_name=class_name,
                    spec_name=spec_name,
                ).exclude(spell_id__isnull=True)

                total = qs.count()
                coords = qs.exclude(row__isnull=True).exclude(column__isnull=True).count()
                parents = qs.exclude(parents_json=[]).count()
                icons = qs.exclude(icon='').count()
                names = qs.exclude(name='').count()
                display = qs.exclude(display_spell_id__isnull=True).count()

                lines.append(
                    f'| {class_name} | {spec_name} | {total} | {coords} | {parents} | {icons} | {names} | {display} |'
                )

                if total == 0 or coords < total or icons < total or names < total:
                    missing.append((class_name, spec_name, total, coords, icons, names))

        lines.append('')
        lines.append('## Incomplete')
        lines.append('')
        if not missing:
            lines.append('All specs look complete.')
        else:
            lines.append('| Class | Spec | Total | Coords | Icons | Names |')
            lines.append('|---|---:|---:|---:|---:|---:|')
            missing.sort(key=lambda x: (x[2] == 0, x[3] / x[2] if x[2] else 0), reverse=False)
            for class_name, spec_name, total, coords, icons, names in missing:
                lines.append(f'| {class_name} | {spec_name} | {total} | {coords} | {icons} | {names} |')

        with open(output, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
