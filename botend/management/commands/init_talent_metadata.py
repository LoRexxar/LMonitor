# -*- coding: utf-8 -*-

from django.core.management import call_command
from django.core.management.base import BaseCommand

from botend.constants.wow import CLASS_SPEC_MAP
from botend.models import WowTalentNodeMetadata


class Command(BaseCommand):
    help = '批量初始化 WoW 天赋元数据，按职业/专精依次执行样本同步与静态回填'

    def add_arguments(self, parser):
        parser.add_argument('--class-name', default='', help='仅处理指定职业')
        parser.add_argument('--spec-name', default='', help='仅处理指定专精')
        parser.add_argument('--sample-limit', type=int, default=0, help='样本同步阶段每个数据源的限制，0 表示不限制')
        parser.add_argument('--backfill-limit', type=int, default=0, help='静态回填阶段每个专精的限制，0 表示不限制')
        parser.add_argument('--skip-sync', action='store_true', help='跳过 sync_talent_metadata，仅执行静态回填')
        parser.add_argument('--stop-on-error', action='store_true', help='遇到单个专精失败时立即中止')
        parser.add_argument('--db2-dump-dir', default='', help='使用 dump_wago_db2_tables 输出目录（提升回填速度）')
        parser.add_argument('--bulk-size', type=int, default=800, help='回填 bulk_update 批大小')

    def handle(self, *args, **options):
        class_name = (options.get('class_name') or '').strip()
        spec_name = (options.get('spec_name') or '').strip()
        sample_limit = max(0, int(options.get('sample_limit') or 0))
        backfill_limit = max(0, int(options.get('backfill_limit') or 0))
        skip_sync = bool(options.get('skip_sync'))
        stop_on_error = bool(options.get('stop_on_error'))
        db2_dump_dir = (options.get('db2_dump_dir') or '').strip()
        bulk_size = max(50, int(options.get('bulk_size') or 800))

        targets = self._build_targets(class_name, spec_name)
        self.stdout.write(f'准备初始化 {len(targets)} 个职业/专精目标')

        if not skip_sync:
            sync_kwargs = {'limit': sample_limit}
            if class_name:
                sync_kwargs['class_name'] = class_name
            if spec_name:
                sync_kwargs['spec_name'] = spec_name
            self.stdout.write('开始执行样本元数据同步')
            call_command('sync_talent_metadata', **sync_kwargs)

        failures = []
        for index, (target_class, target_spec) in enumerate(targets, start=1):
            call_command(
                'normalize_talent_metadata',
                class_name=target_class,
                spec_name=target_spec,
            )
            before = self._collect_coverage(target_class, target_spec)
            self.stdout.write(
                f'[{index}/{len(targets)}] {target_class}/{target_spec} '
                f'初始化前 total={before["total"]} coords={before["coords"]} '
                f'parents={before["parents"]} icon={before["icon"]} '
                f'name={before["name"]} display={before["display_spell"]}'
            )
            try:
                call_command(
                    'backfill_talent_spell_names',
                    class_name=target_class,
                    spec_name=target_spec,
                    limit=backfill_limit,
                    refresh_tree_type=True,
                    db2_dump_dir=db2_dump_dir,
                    bulk_size=bulk_size,
                )
            except Exception as exc:
                failures.append((target_class, target_spec, str(exc)))
                self.stdout.write(self.style.ERROR(
                    f'[{index}/{len(targets)}] {target_class}/{target_spec} 初始化失败: {exc}'
                ))
                if stop_on_error:
                    raise
                continue

            after = self._collect_coverage(target_class, target_spec)
            self.stdout.write(self.style.SUCCESS(
                f'[{index}/{len(targets)}] {target_class}/{target_spec} 初始化完成 '
                f'coords {before["coords"]}->{after["coords"]}, '
                f'parents {before["parents"]}->{after["parents"]}, '
                f'icon {before["icon"]}->{after["icon"]}, '
                f'name {before["name"]}->{after["name"]}, '
                f'display {before["display_spell"]}->{after["display_spell"]}'
            ))

        if failures:
            self.stdout.write(self.style.WARNING(f'初始化完成，但有 {len(failures)} 个目标失败'))
            for target_class, target_spec, message in failures:
                self.stdout.write(f'- {target_class}/{target_spec}: {message}')
            return

        self.stdout.write(self.style.SUCCESS('所有目标初始化完成'))

    def _build_targets(self, class_name='', spec_name=''):
        if class_name:
            specs = CLASS_SPEC_MAP.get(class_name, [])
            if spec_name:
                return [(class_name, spec_name)]
            return [(class_name, current_spec) for current_spec in specs]
        targets = []
        for current_class, specs in CLASS_SPEC_MAP.items():
            for current_spec in specs:
                targets.append((current_class, current_spec))
        return targets

    @staticmethod
    def _collect_coverage(class_name, spec_name):
        queryset = WowTalentNodeMetadata.objects.filter(
            class_name=class_name,
            spec_name=spec_name,
        ).exclude(spell_id__isnull=True)
        return {
            'total': queryset.count(),
            'coords': queryset.exclude(row__isnull=True).exclude(column__isnull=True).count(),
            'parents': queryset.exclude(parents_json=[]).count(),
            'icon': queryset.exclude(icon='').count(),
            'name': queryset.exclude(name='').count(),
            'display_spell': queryset.exclude(display_spell_id__isnull=True).count(),
        }
