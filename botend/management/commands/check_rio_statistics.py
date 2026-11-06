"""检查统计缓存和日更状态，不请求上游。"""
import json

from django.core.management.base import BaseCommand, CommandError
from botend.services.rio_statistics_health import check_health
from botend.services.rio_statistics_storage import StorageError


class Command(BaseCommand):
    help = '检查数据观察存储、地图和每日批次；可显式迁移旧缓存、校验正文并创建固定槽备份'

    def add_arguments(self, parser):
        parser.add_argument('--deep', action='store_true', help='校验全部正文及轻量历史的摘要')
        parser.add_argument('--migrate', action='store_true', help='将旧 JSON 缓存迁入独立缓存库，保留原文件')
        parser.add_argument('--backup', action='store_true', help='创建或更新当天固定备份槽')
        parser.add_argument('--allow-warning', action='store_true', help='有未运行批次等警告时允许返回零退出码')
        parser.add_argument('--progress-only', action='store_true', help='只读最近阶段和计数，不扫描全部正文或目录')

    def handle(self, *args, **options):
        try:
            if options['progress_only']:
                if options['deep'] or options['migrate'] or options['backup']:
                    raise CommandError('轻量进度不能与深检、迁移或备份同时执行')
                from botend.services.rio_statistics import store
                self.stdout.write(json.dumps(store().progress(), ensure_ascii=False, indent=2))
                return
            report = check_health(deep=options['deep'], initialize=options['migrate'], backup=options['backup'])
        except (StorageError, OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
        if report['status'] == 'error' or (report['status'] == 'warning' and not options['allow_warning']):
            raise CommandError('统计检查存在未通过项，请查看报告；未删除旧数据')
