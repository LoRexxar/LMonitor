"""按物品核验并转换存量装备描述，默认只读预检。"""
import json
from django.core.management.base import BaseCommand
from botend.models import WowItemSnapshot
from botend.services.variant_text import compact_item


class Command(BaseCommand):
    help = '无损拆分装备变体描述并回收无引用模板；默认预检'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--restore-inline', action='store_true', help='回退专用：无损还原内联原文，须先停止维护与采集进程')
        parser.add_argument('--after', type=int, default=0, help='上次返回的物品数据库主键')
        parser.add_argument('--limit', type=int, default=200, help='本批处理物品数，最多 1000')

    def handle(self, *args, **options):
        rows = list(WowItemSnapshot.objects.filter(pk__gt=options['after']).order_by('pk')
                    .values_list('pk', flat=True)[:max(1, min(1000, options['limit']))])
        report = dict(items=0, variants=0, before_bytes=0, after_bytes=0, templates=0)
        for pk in rows:
            result = compact_item(pk, apply=options['apply'], restore=options['restore_inline'])
            for key in report:
                report[key] += result[key]
        self.stdout.write(json.dumps({'已应用': options['apply'], '结果': report,
            '下次起点': rows[-1] if rows else options['after'], '本批物品数': len(rows)}, ensure_ascii=False))
