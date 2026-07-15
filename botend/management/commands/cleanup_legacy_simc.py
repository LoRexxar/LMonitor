from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from botend.models import SimcTask, SimcTaskBatch


class Command(BaseCommand):
    help = '清理可证明无法执行的 legacy SimC 任务及其空批次（默认 dry-run）'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='实际删除；省略时只预览')

    def handle(self, *args, **options):
        # 已失败且没有冻结正文、legacy ext 或产物的任务无法复现当时输入，
        # 即使仍关联 Profile 也不能安全重跑；Profile 本身必须保留。
        broken = SimcTask.objects.filter(
            current_status=3, artifacts__isnull=True,
        ).filter(Q(final_simc_content__isnull=True) | Q(final_simc_content='')).filter(
            Q(ext__isnull=True) | Q(ext='')
        )
        task_ids = list(broken.values_list('id', flat=True).distinct())
        batch_ids = list(SimcTaskBatch.objects.filter(simctask__isnull=True).values_list('id', flat=True))
        mode = 'APPLY' if options['apply'] else 'DRY-RUN'
        self.stdout.write(f'{mode}: legacy/damaged tasks={len(task_ids)}, empty batches={len(batch_ids)}')
        if not options['apply']:
            self.stdout.write('未修改数据；使用 --apply 执行。')
            return
        with transaction.atomic():
            if task_ids:
                SimcTask.objects.filter(id__in=task_ids).delete()
            # 仅删除扫描时就已为空的批次；不级联清理由任务删除后才变空的批次。
            deleted_batches, _ = SimcTaskBatch.objects.filter(id__in=batch_ids, simctask__isnull=True).delete()
        self.stdout.write(self.style.SUCCESS(
            f'已删除 {len(task_ids)} 个损坏任务和 {deleted_batches} 个原本为空的批次；配置/APL/模板/规则未触碰。'))
