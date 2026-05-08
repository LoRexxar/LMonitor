from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from botend.models import PortalEvent


class Command(BaseCommand):
    help = 'Seed demo portal events'

    def handle(self, *args, **options):
        if PortalEvent.objects.filter(is_active=True).exclude(source='demo').exists():
            self.stdout.write('Non-demo PortalEvent exists, skip.')
            return

        now = timezone.now()
        data = [
            {
                'title': '示例：暗月马戏团（即将开始）',
                'url': 'https://wow.blizzard.cn/?demo_event=darkmoon',
                'start_at': now + timedelta(days=1, hours=2),
                'end_at': now + timedelta(days=8, hours=2),
            },
            {
                'title': '示例：时空漫游周（进行中）',
                'url': 'https://wow.blizzard.cn/?demo_event=timewalking',
                'start_at': now - timedelta(days=2),
                'end_at': now + timedelta(days=5),
            },
            {
                'title': '示例：服务器维护（已结束）',
                'url': 'https://wow.blizzard.cn/?demo_event=maintenance',
                'start_at': now - timedelta(days=3, hours=6),
                'end_at': now - timedelta(days=3, hours=3),
            },
        ]

        PortalEvent.objects.filter(source='demo').delete()

        for it in data:
            PortalEvent.objects.update_or_create(
                url=it['url'],
                defaults={
                    'title': it['title'],
                    'source': 'demo',
                    'tag': '示例',
                    'start_at': it['start_at'],
                    'end_at': it['end_at'],
                    'status': '',
                    'is_active': True,
                },
            )

        self.stdout.write(f'Seeded {len(data)} demo portal events.')
