"""预热并发布职业攻略目录、正文及引用快照。"""
from django.core.management.base import BaseCommand, CommandError

from botend.services.class_guide_snapshot import refresh_guide_snapshot


class Command(BaseCommand):
    help = '生成攻略私有中间文件；成功后整批切换，失败保留上一版'

    def handle(self, *args, **options):
        try:
            if not refresh_guide_snapshot():
                raise CommandError('攻略快照未发布：其他进程正在构建，或构建期间内容发生变化，请重试')
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError(f'攻略快照构建失败：{exc}') from exc
        self.stdout.write(self.style.SUCCESS('攻略目录和文章快照已发布'))
