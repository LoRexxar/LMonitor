import os
import time
import requests
from django.core.management.base import BaseCommand
from django.conf import settings


class Command(BaseCommand):
    help = '爬取 WoW 图标到本地 static 目录'

    def add_arguments(self, parser):
        parser.add_argument(
            '--size', type=str, default='all',
            choices=['all', 'small', 'medium', 'tiny'],
            help='要下载的图标尺寸 (默认: all 下载所有尺寸)'
        )

    def handle(self, *args, **options):
        from botend.models import SpecDungeonRanking, SpecRaidRanking, PlayerSpecTopPlayer

        icons = set()

        # Collect icon names from all relevant models
        for model in [SpecDungeonRanking, SpecRaidRanking, PlayerSpecTopPlayer]:
            for talents, gear in model.objects.values_list('talents_json', 'gear_json'):
                if talents:
                    for t in talents:
                        if isinstance(t, dict) and t.get('icon'):
                            icons.add(t['icon'])
                if gear:
                    for g in gear:
                        if isinstance(g, dict) and g.get('icon'):
                            icons.add(g['icon'])

        icons.discard(None)
        icons.discard('')
        self.stdout.write(f'Found {len(icons)} unique icons')

        # Determine sizes to download
        size = options['size']
        if size == 'all':
            sizes = ['small', 'medium', 'tiny']
        else:
            sizes = [size]

        # BASE_DIR points to LMonitor/ (parent of LMonitor/settings.py)
        static_dir = os.path.join(settings.BASE_DIR, 'static', 'wow_icons')
        os.makedirs(static_dir, exist_ok=True)

        downloaded = 0
        skipped = 0
        failed = 0

        for icon_name in sorted(icons):
            for sz in sizes:
                filename = f'{icon_name}.jpg'
                filepath = os.path.join(static_dir, sz, filename)
                os.makedirs(os.path.join(static_dir, sz), exist_ok=True)

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
                        self.stdout.write(f'  ✓ {sz}/{icon_name}.jpg')
                    else:
                        failed += 1
                        self.stderr.write(f'  ✗ {sz}/{icon_name}.jpg (HTTP {resp.status_code})')
                    time.sleep(0.1)  # rate limit
                except Exception as e:
                    failed += 1
                    self.stderr.write(f'  ✗ {sz}/{icon_name}.jpg ({e})')

        self.stdout.write(self.style.SUCCESS(
            f'完成: {downloaded} 已下载, {skipped} 已跳过, {failed} 失败'
        ))
