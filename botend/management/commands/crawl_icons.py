import os
import gc
import time
import requests
from django.core.management.base import BaseCommand
from django.conf import settings


class Command(BaseCommand):
    help = '爬取 WoW 图标到本地 static 目录'

    def add_arguments(self, parser):
        parser.add_argument(
            '--size', type=str, default='small',
            choices=['all', 'small', 'medium', 'tiny'],
            help='要下载的图标尺寸 (默认: small)'
        )

    def handle(self, *args, **options):
        from botend.models import SpecDungeonRanking, SpecRaidRanking, PlayerSpecTopPlayer, WowItemSnapshot

        icons = set()

        # 分批收集 icon 名，避免一次性加载全部数据
        models = [SpecDungeonRanking, SpecRaidRanking, PlayerSpecTopPlayer]
        for model in models:
            self.stdout.write(f'扫描 {model.__name__}...')
            count = 0
            # 用 iterator 逐条读取，不缓存到内存
            for row in model.objects.values_list('talents_json', 'gear_json').iterator(chunk_size=200):
                for json_data in row:
                    if not json_data or not isinstance(json_data, list):
                        continue
                    for item in json_data:
                        if isinstance(item, dict):
                            self._collect_icon_from_payload(item, icons)
                count += 1
                if count % 1000 == 0:
                    gc.collect()  # 主动回收内存

            self.stdout.write(f'  {model.__name__}: {count} 条记录, {len(icons)} 个图标')

        self.stdout.write('扫描 WowItemSnapshot...')
        snapshot_count = 0
        for icon_name in WowItemSnapshot.objects.exclude(icon='').values_list('icon', flat=True).iterator(chunk_size=500):
            normalized = self._normalize_icon_name(icon_name)
            if normalized:
                icons.add(normalized)
            snapshot_count += 1
            if snapshot_count % 2000 == 0:
                gc.collect()
        self.stdout.write(f'  WowItemSnapshot: {snapshot_count} 条记录, {len(icons)} 个图标')

        icons.discard(None)
        icons.discard('')
        self.stdout.write(f'共发现 {len(icons)} 个唯一图标')

        # 确定下载尺寸
        size = options['size']
        if size == 'all':
            sizes = ['small', 'medium', 'tiny']
        else:
            sizes = [size]

        static_dir = os.path.join(settings.BASE_DIR, 'static', 'wow_icons')
        os.makedirs(static_dir, exist_ok=True)

        downloaded = 0
        skipped = 0
        failed = 0
        total = len(icons) * len(sizes)

        for i, icon_name in enumerate(sorted(icons)):
            for sz in sizes:
                filename = f'{icon_name}.jpg'
                sz_dir = os.path.join(static_dir, sz)
                filepath = os.path.join(sz_dir, filename)
                os.makedirs(sz_dir, exist_ok=True)

                if os.path.exists(filepath):
                    skipped += 1
                    continue

                url = f'https://wow.zamimg.com/images/wow/icons/{sz}/{filename}'
                try:
                    resp = requests.get(url, timeout=10, headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                    })
                    if resp.status_code == 200 and len(resp.content) > 100:
                        with open(filepath, 'wb') as f:
                            f.write(resp.content)
                        downloaded += 1
                    else:
                        failed += 1
                    time.sleep(0.05)  # rate limit
                except Exception as e:
                    failed += 1
                    self.stderr.write(f'  ✗ {sz}/{icon_name}: {e}')

            # 进度
            done = (i + 1) * len(sizes)
            if (i + 1) % 50 == 0 or i == len(icons) - 1:
                self.stdout.write(f'  进度: {done}/{total} (下载 {downloaded}, 跳过 {skipped}, 失败 {failed})')

        self.stdout.write(self.style.SUCCESS(
            f'完成: {downloaded} 已下载, {skipped} 已跳过, {failed} 失败'
        ))

    @classmethod
    def _collect_icon_from_payload(cls, payload, icons):
        normalized = cls._normalize_icon_name(payload.get('icon'))
        if normalized:
            icons.add(normalized)
        for key in ('gems_detail', 'gems', 'enchants_detail'):
            values = payload.get(key) or []
            if not isinstance(values, list):
                continue
            for child in values:
                if isinstance(child, dict):
                    child_icon = cls._normalize_icon_name(child.get('icon'))
                    if child_icon:
                        icons.add(child_icon)

    @staticmethod
    def _normalize_icon_name(icon_name):
        icon_name = str(icon_name or '').strip()
        if not icon_name:
            return ''
        icon_name = icon_name.split('?', 1)[0].strip()
        icon_name = icon_name.rsplit('/', 1)[-1]
        while '.' in icon_name:
            base, ext = icon_name.rsplit('.', 1)
            if ext.lower() in {'jpg', 'jpeg', 'png', 'gif', 'webp'}:
                icon_name = base
                continue
            break
        return icon_name.strip()
