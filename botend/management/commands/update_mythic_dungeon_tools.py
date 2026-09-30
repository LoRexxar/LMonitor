"""手动触发与每日后台任务相同的 MDT 自动更新。"""
from django.core.management.base import BaseCommand, CommandError
from botend.mythic_planner.auto_update import sync_latest_mdt


class Command(BaseCommand):
    help = '检查并原位升级至最新 MDT 正式发布；复用后台锁，保留路线和人工编辑。'

    def add_arguments(self, parser):
        parser.add_argument('--check-only', action='store_true', help='只检查上游版本，不下载、上传或导入数据。')

    def handle(self, *args, **options):
        try:
            run = sync_latest_mdt(check_only=options['check_only'])
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        labels = {'unchanged': '已是最新版本', 'available': '发现新版本', 'updated': '更新完成'}
        self.stdout.write(self.style.SUCCESS(f'MDT {run.target_version}：{labels[run.status]}，批次 {run.pk}。'))
