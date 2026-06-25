from django.core.management.base import BaseCommand

from botend.services.portal_event_service import PortalEventService


class Command(BaseCommand):
    help = 'Initialize or refresh portal events'

    def add_arguments(self, parser):
        parser.add_argument('--url', default='', help='指定新闻/活动页 URL；指定后走 HTML 解析')
        parser.add_argument('--build', default='', help='指定 wago.tools DB2 build；为空时自动使用当前 build')
        parser.add_argument('--locale', default='zhCN', help='DB2 locale，默认 zhCN')
        parser.add_argument('--news', action='store_true', help='使用官方新闻页解析，而不是 DB2 日历活动')
        parser.add_argument('--fallback', action='store_true', help='仅写入本地兜底活动数据')

    def handle(self, *args, **options):
        service = PortalEventService()
        if options.get('fallback'):
            result = service.seed_fallback_events()
        elif options.get('news') or options.get('url'):
            result = service.sync_news_events(source_url=options.get('url') or '')
            if result.get('total', 0) == 0:
                fallback = service.seed_fallback_events()
                result['fallback'] = fallback
        else:
            result = service.sync_db2_events(
                build=options.get('build') or '',
                locale=options.get('locale') or 'zhCN',
            )
            if result.get('total', 0) == 0:
                fallback = service.seed_fallback_events()
                result['fallback'] = fallback
        self.stdout.write(self.style.SUCCESS(f'Portal events initialized: {result}'))
