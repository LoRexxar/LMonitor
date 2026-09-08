"""旧块式正文已由文章结构迁移统一转换，此兼容命令不再写入内容。"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = '兼容旧部署命令；当前攻略直接保存整篇 Markdown'

    def handle(self, *args, **options):
        self.stdout.write('攻略已直接保存 Markdown，无需额外转换。请执行 migrate 完成结构升级。')
