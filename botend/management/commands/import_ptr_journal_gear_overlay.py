import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from botend.interface.ossupload import ossUploadObject
from botend.services.ptr_journal_gear_overlay import import_ptr_journal_gear_overlay


class Command(BaseCommand):
    help = '校验并增量导入 PTR 冒险手册与 Gear Builder 装备 overlay（默认 dry-run）'

    def add_arguments(self, parser):
        parser.add_argument(
            '--artifact',
            default=str(Path(settings.BASE_DIR) / 'botend' / 'data' / 'ptr_kithix_unbound_12_1_5.json'),
            help='版本化 PTR overlay JSON 路径',
        )
        parser.add_argument('--apply', action='store_true', help='实际写入；未指定时只输出计划')

    def handle(self, *args, **options):
        artifact = Path(options['artifact'])
        report = import_ptr_journal_gear_overlay(artifact, apply=False)
        if options['apply'] and not report['already_applied']:
            icons = self._upload_icons(artifact)
            report = import_ptr_journal_gear_overlay(artifact, apply=True) | {'icons': icons}
        elif options['apply']:
            report['icons'] = {'uploaded': 0, 'skipped': 'overlay already applied'}
        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        if not options['apply']:
            self.stdout.write(self.style.WARNING('dry-run：未写入数据库；实际导入需显式指定 --apply'))
        elif report['already_applied']:
            self.stdout.write(self.style.WARNING('相同 PTR overlay 已完整生效，本次未重复写入'))
        else:
            self.stdout.write(self.style.SUCCESS('PTR 冒险手册与装备 overlay 已原子导入'))

    @staticmethod
    def _upload_icons(artifact):
        payload = json.loads(artifact.read_text(encoding='utf-8'))
        asset_root = artifact.with_name(f'{artifact.stem}_icons')
        names = sorted({str(item.get('icon') or '').strip()
                        for item in (payload.get('gear') or {}).get('items') or []})
        planned = [
            (asset_root / size / f'{name}.jpg', f'wow_icons_oss/{size}/{name}.jpg')
            for size in ('small', 'medium') for name in names
        ]
        missing = [str(path) for path, _key in planned if not path.is_file()]
        if missing:
            raise ValueError(f'PTR overlay 缺少 {len(missing)} 个版本化图标资产')
        for path, object_key in planned:
            if not ossUploadObject(str(path), object_key=object_key):
                raise ValueError(f'PTR overlay 图标上传失败：{path.name}')
        return {'uploaded': len(planned), 'sizes': ['small', 'medium']}
