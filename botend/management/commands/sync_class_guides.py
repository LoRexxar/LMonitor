"""手动执行一次来源同步；持续更新由 botend 统一调度。"""

from django.core.management.base import BaseCommand, CommandError
from botend.services.class_guide_monitor import get_guide_monitor_task
from botend.services.class_guide_service import sync_guides


class Command(BaseCommand):
    help = '同步已授权的全部非练级职业攻略；使用 --translate 生成中文正文'

    def add_arguments(self, parser):
        parser.add_argument('--translate', action='store_true', help='调用站内翻译引擎，成功段落断点缓存')
        parser.add_argument('--refresh-translations', action='store_true', help='原文未变时也按当前术语重建中文候选；仍复用有效段落缓存')
        parser.add_argument('--cache-dir', help='首次导入使用的原始 JSON 快照目录')
        parser.add_argument('--limit', type=int, help='本次最多处理篇数')
        parser.add_argument('--workers', type=int, default=1, choices=range(1, 9), help='文章处理并发数，最多 8')
        parser.add_argument('--watch', action='store_true', help='已停用；请在 Dashboard 开启 botend 监控任务')
        parser.add_argument('--authorization-note', help='记录已获得的翻译转载授权依据')

    def handle(self, *args, **options):
        if options['watch']:
            raise CommandError('--watch 已停用。请在 Dashboard 的“监控任务”开启 MaxrollClassGuideMonitor，由现有 botend 后端统一调度，无需额外常驻进程。')
        task = get_guide_monitor_task()
        if options['authorization_note']:
            task.notes = options['authorization_note']
            task.save(update_fields=['notes'])
        if options['limit'] is not None and options['limit'] <= 0:
            raise CommandError('篇数必须大于零')
        if options['refresh_translations'] and not options['translate']:
            raise CommandError('刷新译文必须同时指定 --translate')
        try:
            run = sync_guides(translate=options['translate'], cache_dir=options['cache_dir'], limit=options['limit'], log=self.stdout.write, workers=options['workers'], refresh_translations=options['refresh_translations'])
        except (ValueError, RuntimeError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write('同步批次 {}：{}，处理 {} 篇'.format(run.id, run.status, len(run.results)))
        if run.status not in {'completed', 'limited'}:
            raise CommandError('存在失败或未处理文章，详见同步批次')
