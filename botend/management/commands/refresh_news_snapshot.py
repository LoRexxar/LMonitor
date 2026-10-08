"""从已有新闻资料发布轻量索引，不联网。"""
from django.core.management.base import BaseCommand, CommandError
from botend.services.news_snapshot import refresh_news_snapshot


class Command(BaseCommand):
    help = '离线发布新闻首页栏目与历史列表的统一 JSON 索引'

    def handle(self, *args, **options):
        try:
            completed = refresh_news_snapshot()
        except Exception as exc:
            raise CommandError('新闻索引发布失败，保留上一版。') from exc
        if not completed:
            raise CommandError('其他发布正在执行或来源发生变化，请稍后重试。')
        self.stdout.write(self.style.SUCCESS('新闻轻量索引发布完成。'))
