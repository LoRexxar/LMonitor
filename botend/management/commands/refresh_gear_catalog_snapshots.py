"""预热或修复配装器目录 JSON 文件。"""
from django.core.management.base import BaseCommand, CommandError
from botend.services import gear_catalog_snapshot as snapshots
from botend.services import gear_builder as gb


class Command(BaseCommand):
    help = '构建已登记的配装目录分片，或预热指定职业专精'

    def add_arguments(self, parser):
        parser.add_argument('--class', dest='class_name')
        parser.add_argument('--spec', dest='spec_name')
        parser.add_argument('--slot', choices=tuple(gb.SLOT_LABELS))
        parser.add_argument('--limit', type=int, default=640)
        parser.add_argument('--force', action='store_true')
        parser.add_argument('--kind', choices=('all', 'equipment', 'enhancements'), default='all', help='预热装备、增强或两类目录')

    def handle(self, *args, **options):
        if bool(options['class_name']) != bool(options['spec_name']) or (options['slot'] and not options['class_name']):
            raise CommandError('请同时指定 --class 和 --spec；--slot 需要指定职业专精')
        if options['limit'] < 1:
            raise CommandError('--limit 必须为正整数')
        targets = [] if options['class_name'] else None
        if options['class_name']:
            try:
                for slot in [options['slot']] if options['slot'] else gb.SLOT_LABELS:
                    for kind in ('equipment', 'enhancements') if options['kind'] == 'all' else (options['kind'],):
                        targets.append(snapshots.request_refresh(snapshots.coordinate(options['class_name'], options['spec_name'], slot, kind)))
            except gb.GearBuilderError as exc:
                raise CommandError(str(exc)) from exc
        try:
            rows = snapshots.refresh_catalog_snapshots(batch_size=options['limit'], force=options['force'],
                targets=targets, kind=options['kind'], strict=True)
        except (RuntimeError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f'已刷新 {len(rows)} 个目录分片，本次目标回读验证通过。')
