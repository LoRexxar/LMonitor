"""Bounded board-metadata repair; never invokes scan/body/translation code."""
import argparse
import json

from django.core.management.base import BaseCommand, CommandError

from botend.models import MonitorTask, WowArticle
from botend.services.blizzard_forum import ForumMetadataCollector, article_channel, validate_metadata_lease


def positive_int(value):
    if not str(value).isascii() or not str(value).isdigit() or int(value) <= 0:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return int(value)


class Command(BaseCommand):
    help = 'Refresh forum-category metadata for bounded existing Blizzard tracker articles (default: dry-run, no HTTP/writes).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--id', action='append', type=positive_int, dest='ids', default=[])
        parser.add_argument('--limit', type=positive_int, default=20)
        parser.add_argument('--task-id', type=positive_int, default=None)
        parser.add_argument('--lease-owner', default=None)

    def build_collector(self, task):
        from utils.LReq import LReq
        req = LReq(is_chrome=False)
        req.set_current_task(task)
        owner = getattr(task, '_monitor_task_lease_owner', None)
        guard = (lambda: validate_metadata_lease(task.pk, owner)) if owner else None
        return ForumMetadataCollector(req, write_guard=guard)

    def handle(self, *args, **options):
        if ((options['task_id'] is None) != (options['lease_owner'] is None)
                or options['lease_owner'] == ''):
            raise CommandError('--task-id and --lease-owner must be supplied together')
        queryset = WowArticle.objects.filter(source='blizzard_tracker').select_related('blizzard_topic__category')
        if options['ids']:
            queryset = queryset.filter(pk__in=set(options['ids']))
        articles = list(queryset.order_by('-id')[:options['limit']])
        report = {'mode': 'apply' if options['apply'] else 'dry-run', 'count': len(articles),
                  'changed': 0, 'skipped': 0, 'failed': 0, 'results': []}
        if not options['apply']:
            report['skipped'] = len(articles)
            report['results'] = [{'id': article.pk, 'status': 'dry-run'} for article in articles]
            self.stdout.write(json.dumps(report, ensure_ascii=False))
            return
        tasks = MonitorTask.objects.filter(name='PortalPostMonitor')
        if options['task_id'] is not None:
            tasks = tasks.filter(pk=options['task_id'])
        candidates = list(tasks[:2])
        if len(candidates) != 1:
            raise CommandError('exactly one actual PortalPostMonitor task is required; use --task-id')
        task = candidates[0]
        if options['lease_owner'] is not None:
            task._monitor_task_lease_owner = options['lease_owner']
        collector = self.build_collector(task)
        try:
            collector.check_lease()
            for article in articles:
                before = (article.blizzard_topic_id, article_channel(article))
                ok = collector.ensure(article, force=True)
                article.refresh_from_db()
                after = (article.blizzard_topic_id, article_channel(article))
                status = 'changed' if ok and before != after else 'skipped'
                if not ok and collector.last_error and collector.last_error != 'metadata_cooldown':
                    status = 'failed'
                report[status] += 1
                result = {'id': article.pk, 'status': status}
                if collector.last_error:
                    # Never emit exception text: requests exceptions can contain proxy credentials.
                    result['error'] = collector.last_error
                report['results'].append(result)
        finally:
            req = collector.req
            if hasattr(req, 'set_current_task'):
                req.set_current_task(None)
            session = getattr(req, 's', None)
            if session is not None:
                session.close()
        self.stdout.write(json.dumps(report, ensure_ascii=False))
        if report['failed']:
            raise CommandError('Forum metadata refresh failed; see safe JSON report')
