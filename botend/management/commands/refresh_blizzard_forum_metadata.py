"""Bounded board-metadata repair; never invokes scan/body/translation code."""
import argparse
import json
from contextlib import contextmanager

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from botend.models import MonitorTask, MonitorTaskLeaseLost, WowArticle
from botend.monitor_env import env_limit_hint, is_task_runnable
from botend.services.blizzard_forum import ForumMetadataCollector, article_channel, validate_metadata_lease
from botend.services.monitor_task_execution import MonitorTaskAdmissionBusy, monitor_task_execution


def positive_int(value):
    if not str(value).isascii() or not str(value).isdigit() or int(value) <= 0:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return int(value)


def validate_execution_lease(task_id, owner):
    """Reuse the caller's lease only for the real, environment-eligible plugin."""
    from LMonitor.config import Monitor_Type_BaseObject_List
    with transaction.atomic():
        # Retain the collector's all-parent -> exact-lease locking order.
        validate_metadata_lease(task_id, owner)
        task = MonitorTask.objects.get(pk=task_id)
        task_type = next((i for i, plugin in enumerate(Monitor_Type_BaseObject_List)
                          if plugin.__name__ == 'PortalPostMonitor'), None)
        if task_type is None or task.type != task_type:
            raise MonitorTaskLeaseLost('metadata task has incorrect plugin type')
        if not is_task_runnable(task):
            raise MonitorTaskAdmissionBusy(env_limit_hint(task.env_limit), code='env_limit_blocked',
                                           env_limit=task.env_limit)
        task._monitor_task_lease_owner = owner
        return task


@contextmanager
def metadata_execution(task_id, owner):
    if owner is None:
        with monitor_task_execution('PortalPostMonitor') as task:
            yield task
    else:
        # The caller retains ownership/heartbeat/completion; never claim twice.
        task = validate_execution_lease(task_id, owner)
        yield task
        validate_execution_lease(task_id, owner)


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
        guard = (lambda: validate_execution_lease(task.pk, owner)) if owner else None
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
        try:
            with metadata_execution(options['task_id'], options['lease_owner']) as task:
                self.refresh_metadata(task, articles, report)
        except MonitorTaskAdmissionBusy as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(report, ensure_ascii=False))
        if report['failed']:
            raise CommandError('Forum metadata refresh failed; see safe JSON report')

    def refresh_metadata(self, task, articles, report):
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
