from django.core.management.base import BaseCommand

from botend.services.portal_event_service import PortalEventService


class Command(BaseCommand):
    help = 'Initialize or refresh portal events'

    def add_arguments(self, parser):
        parser.add_argument('--url', default='', help='指定活动来源 URL；为空时使用默认官方来源')
        parser.add_argument('--fallback', action='store_true', help='仅写入本地兜底活动数据')

    def handle(self, *args, **options):
        service = PortalEventService()
        if options.get('fallback'):
            result = service.seed_fallback_events()
        else:
            result = service.sync_events(source_url=options.get('url') or '')
            if result.get('total', 0) == 0:
                fallback = service.seed_fallback_events()
                result['fallback'] = fallback
        self.stdout.write(self.style.SUCCESS(f'Portal events initialized: {result}'))
