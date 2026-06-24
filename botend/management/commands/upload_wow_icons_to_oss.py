import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.conf import settings
from django.core.management.base import BaseCommand

from botend.interface.ossupload import ossUploadObject


class Command(BaseCommand):
    help = '批量上传本地 WoW 图标到阿里云 OSS'

    def add_arguments(self, parser):
        parser.add_argument(
            '--prefix',
            type=str,
            default='wow_icons_oss',
            help='OSS 目标目录前缀，默认 wow_icons_oss',
        )
        parser.add_argument(
            '--size',
            type=str,
            default='all',
            choices=['all', 'tiny', 'small', 'medium'],
            help='要上传的尺寸目录，默认 all',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='只输出将上传的文件，不实际上传',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='保留参数用于显式表示允许覆盖 OSS 同名对象',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=0,
            help='限制处理文件数，默认不限制',
        )
        parser.add_argument(
            '--workers',
            type=int,
            default=12,
            help='并发上传线程数，默认 12',
        )

    def handle(self, *args, **options):
        static_dir = os.path.join(settings.BASE_DIR, 'static', 'wow_icons')
        prefix = str(options['prefix'] or '').strip().strip('/')
        selected_size = options['size']
        dry_run = bool(options['dry_run'])
        limit = max(int(options['limit'] or 0), 0)
        workers = max(int(options['workers'] or 1), 1)

        if not os.path.isdir(static_dir):
            self.stderr.write(self.style.ERROR(f'本地图标目录不存在: {static_dir}'))
            return
        if not prefix:
            self.stderr.write(self.style.ERROR('--prefix 不能为空'))
            return

        files = list(self._iter_icon_files(static_dir, selected_size, prefix))
        if limit:
            files = files[:limit]

        self.stdout.write(f'扫描目录: {static_dir}')
        self.stdout.write(f'目标前缀: {prefix}')
        self.stdout.write(f'待处理文件: {len(files)}')
        if dry_run:
            for file_path, object_key in files[:20]:
                self.stdout.write(f'DRY-RUN {file_path} -> {object_key}')
            if len(files) > 20:
                self.stdout.write(f'... 还有 {len(files) - 20} 个文件')
            return

        uploaded = 0
        failed = 0
        skipped = 0
        previous_level = logging.getLogger('LSpider').level
        logging.getLogger('LSpider').setLevel(logging.WARNING)
        try:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = []
                for file_path, object_key in files:
                    if not os.access(file_path, os.R_OK):
                        skipped += 1
                        self.stderr.write(f'跳过不可读文件: {file_path}')
                        continue
                    futures.append(executor.submit(self._upload_one, file_path, object_key))

                total = len(futures)
                for index, future in enumerate(as_completed(futures), start=1):
                    ok, file_path, object_key, error = future.result()
                    if ok:
                        uploaded += 1
                    else:
                        failed += 1
                        suffix = f': {error}' if error else ''
                        self.stderr.write(f'上传失败: {file_path} -> {object_key}{suffix}')

                    if index % 100 == 0 or index == total:
                        self.stdout.write(f'进度: {index}/{total} 上传 {uploaded}, 跳过 {skipped}, 失败 {failed}')
        finally:
            logging.getLogger('LSpider').setLevel(previous_level)

        self.stdout.write(self.style.SUCCESS(
            f'完成: 上传 {uploaded}, 跳过 {skipped}, 失败 {failed}'
        ))

    @staticmethod
    def _upload_one(file_path, object_key):
        try:
            return bool(ossUploadObject(file_path, object_key=object_key)), file_path, object_key, ''
        except Exception as exc:
            return False, file_path, object_key, str(exc)

    def _iter_icon_files(self, static_dir, selected_size, prefix):
        allowed_exts = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
        sizes = ['tiny', 'small', 'medium'] if selected_size == 'all' else [selected_size]
        for size in sizes:
            size_dir = os.path.join(static_dir, size)
            if not os.path.isdir(size_dir):
                continue
            for root, _, filenames in os.walk(size_dir):
                for filename in sorted(filenames):
                    ext = os.path.splitext(filename)[1].lower()
                    if ext not in allowed_exts:
                        continue
                    file_path = os.path.join(root, filename)
                    rel_path = os.path.relpath(file_path, static_dir).replace(os.sep, '/')
                    object_key = f'{prefix}/{rel_path}'
                    yield file_path, object_key
