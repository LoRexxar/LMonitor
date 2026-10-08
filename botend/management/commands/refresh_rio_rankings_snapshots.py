"""显式发布数据库中的 Raider.IO 榜单，共用采集后的发布服务。"""
from django.core.management.base import BaseCommand, CommandError
from botend.services.rio_rankings_snapshot import MODULES, publish_rankings


class Command(BaseCommand):
    help = '离线发布大米纪录和专精巅峰榜 JSON，不访问上游'

    def add_arguments(self, parser):
        parser.add_argument('--module', choices=MODULES, help='只发布指定榜单')
        parser.add_argument('--season', default='', help='默认使用活跃赛季的 Raider.IO 标识')
        parser.add_argument('--region', default='world', help='数据库中已有榜单的区域，默认 world')

    def handle(self, *args, **options):
        failed = []
        for module in (options['module'],) if options['module'] else MODULES:
            try:
                publish_rankings(module, season=options['season'], region=options['region'])
                self.stdout.write(f'{module} 榜单已发布')
            except Exception as exc:
                failed.append(f'{module}：{exc}')
        if failed:
            raise CommandError('；'.join(failed))
