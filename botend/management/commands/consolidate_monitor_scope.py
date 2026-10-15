"""Consolidate explicitly selected duplicate monitor configurations, not facts."""
import json
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from botend.models import MonitorTask
from botend.plugin_sync import monitor_scope_key, _locked_monitor_tasks_and_scopes


class Command(BaseCommand):
    help = '保留指定监控配置，仅停用相同插件/目标/环境的指定重复配置；默认dry-run，不删除。'

    def add_arguments(self, parser):
        parser.add_argument('--keep-id', type=int, required=True)
        parser.add_argument('--disable-id', type=int, action='append', required=True)
        parser.add_argument('--apply', action='store_true')

    @transaction.atomic
    def handle(self, *args, **options):
        keep_id = options['keep_id']; ids = set(options['disable_id'])
        if keep_id in ids or not ids:
            raise CommandError('保留和停用范围必须独立且非空')
        rows, active = _locked_monitor_tasks_and_scopes(timezone.now())
        by_id = {row.pk: row for row in rows}
        if any(pk not in by_id for pk in ids | {keep_id}):
            raise CommandError('配置不存在')
        keep = by_id[keep_id]; key = monitor_scope_key(keep)
        if not keep.is_active or any(monitor_scope_key(by_id[pk]) != key for pk in ids):
            raise CommandError('配置不属于同一个业务范围，或保留项已停用')
        pending = sorted(pk for pk in ids if by_id[pk].is_active)
        if pending and key in active:
            raise CommandError('范围仍有有效租约，暂不改变配置')
        if pending and options['apply']:
            MonitorTask.objects.filter(pk__in=pending).update(is_active=False)
        self.stdout.write(json.dumps({'apply': options['apply'], 'keep_id': keep_id,
            'disable_ids': pending, 'status': 'disabled' if options['apply'] and pending else 'would_disable' if pending else 'unchanged'}))
