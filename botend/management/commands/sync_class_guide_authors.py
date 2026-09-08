"""补抓正文外的来源作者卡片，不重新翻译或修改文章正文。"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from django.core.management.base import BaseCommand, CommandError
from botend.guide_models import ClassGuide
from botend.services.class_guide_maxroll import MaxrollClient


class Command(BaseCommand):
    help = '补全所有攻略的来源作者卡片，保留后台自定义资料与正文'

    def add_arguments(self, parser):
        parser.add_argument('--workers', type=int, default=4)

    def handle(self, *args, **options):
        if not 1 <= options['workers'] <= 8:
            raise CommandError('并发数应为 1 至 8')
        rows = list(ClassGuide.objects.exclude(source_url='').values('id', 'source_url'))
        def fetch(row):
            return row['id'], MaxrollClient().article(row['source_url'])['author_profile']
        failed = []
        with ThreadPoolExecutor(max_workers=options['workers']) as pool:
            tasks = {pool.submit(fetch, row): row for row in rows}
            for task in as_completed(tasks):
                try:
                    pk, profile = task.result()
                    ClassGuide.objects.filter(pk=pk).update(source_author_profile=profile)
                    self.stdout.write(f'已补全 {pk}：{profile["name"]}')
                except Exception as exc:
                    failed.append(tasks[task]['id'])
                    self.stderr.write(f'作者资料抓取失败 {tasks[task]["id"]}：{exc}')
        if failed:
            raise CommandError(f'以下文章需重试：{failed}')
        self.stdout.write(f'作者卡片补全完成：{len(rows)} 篇；自定义资料及正文未修改')
