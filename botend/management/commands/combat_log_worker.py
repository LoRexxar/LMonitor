from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "旧版独立日志命令已停用。"

    def handle(self, *args, **options):
        raise CommandError("日志解析由上传触发，到期回收由 LMonitor 定时维护执行。")
