"""预热已同步的冒险手册目录与技能，不访问上游。"""
from django.core.management.base import BaseCommand, CommandError
from botend.services.journal_snapshot import refresh_journal_snapshot


class Command(BaseCommand):
    help = '离线发布冒险手册轻量目录、首领技能及引用'

    def handle(self, *args, **options):
        try:
            completed = refresh_journal_snapshot()
        except Exception as exc:
            raise CommandError('手册文件发布失败，保留上一版。') from exc
        if not completed:
            raise CommandError('发布正在执行或资料发生变化，请稍后重试。')
        self.stdout.write(self.style.SUCCESS('手册目录与技能发布完成。'))
