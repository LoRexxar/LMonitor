import json
from django.core.management.base import BaseCommand, CommandError
from botend.models import SimcTask
from botend.services.simc_task_result_storage import compact_task_results


class Command(BaseCommand):
    help = '去除可由终态Run完整恢复的Task结果副本，默认dry-run；--restore可恢复旧表示。'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--restore', action='store_true')
        parser.add_argument('--task-id', type=int)
        parser.add_argument('--after', type=int, default=0)
        parser.add_argument('--limit', type=int, default=50)

    def handle(self, *args, **options):
        if not 1 <= options['limit'] <= 100 or options['after'] < 0:
            raise CommandError('limit必须1..100，after必须非负')
        qs = SimcTask.objects.filter(pk__gt=options['after']).order_by('pk')
        if options['task_id'] is not None:
            qs = qs.filter(pk=options['task_id'])
        else:
            qs = qs.filter(current_status__in=(2, 3))
        ids = list(qs.values_list('pk', flat=True)[:options['limit']])
        results = [compact_task_results(pk, apply=options['apply'], restore=options['restore']) for pk in ids]
        last = ids[-1] if ids else options['after']
        self.stdout.write(json.dumps({'apply': options['apply'], 'restore': options['restore'],
            'last_id': last, 'has_more': qs.filter(pk__gt=last).exists(), 'results': results}, ensure_ascii=False))
