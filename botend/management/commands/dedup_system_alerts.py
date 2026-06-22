# -*- coding: utf-8 -*-

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from botend.alerting import build_alert_dedup_key, normalize_alert_subject
from botend.models import SystemAlert


class Command(BaseCommand):
    help = '合并 system_alert 中历史重复的 ERROR_LOG 记录，默认 dry-run'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='实际写入数据库；默认只预览')
        parser.add_argument('--category', default='ERROR_LOG', help='报警分类，默认 ERROR_LOG')

    def handle(self, *args, **options):
        category = (options.get('category') or 'ERROR_LOG').strip()
        apply_changes = bool(options.get('apply'))

        groups = defaultdict(list)
        qs = SystemAlert.objects.filter(category=category).order_by('id')
        for alert in qs.iterator(chunk_size=500):
            subject = normalize_alert_subject(alert.category, alert.subject)
            key = build_alert_dedup_key(alert.category, subject, alert.content)
            groups[key].append(alert)

        duplicate_groups = [rows for rows in groups.values() if len(rows) > 1]
        rows_to_delete = sum(len(rows) - 1 for rows in duplicate_groups)

        self.stdout.write(
            'category={} duplicate_groups={} duplicate_rows={}'.format(
                category, len(duplicate_groups), rows_to_delete
            )
        )

        if not apply_changes:
            for rows in duplicate_groups[:20]:
                keep = max(rows, key=lambda item: (item.last_seen_at, item.id))
                self.stdout.write(
                    'DRY-RUN keep_id={} merge_ids={} subject={} count_sum={}'.format(
                        keep.id,
                        ','.join(str(item.id) for item in rows if item.id != keep.id),
                        keep.subject,
                        sum(item.count or 1 for item in rows),
                    )
                )
            self.stdout.write('dry-run only; rerun with --apply to merge')
            return

        merged_groups = 0
        deleted_rows = 0
        with transaction.atomic():
            for rows in duplicate_groups:
                keep = max(rows, key=lambda item: (item.last_seen_at, item.id))
                duplicates = [item for item in rows if item.id != keep.id]
                keep.category = category
                keep.subject = normalize_alert_subject(keep.category, keep.subject)
                target_key = build_alert_dedup_key(keep.category, keep.subject, keep.content)
                existing = SystemAlert.objects.select_for_update().filter(dedup_key=target_key).exclude(id=keep.id).first()
                if existing and all(item.id != existing.id for item in rows):
                    rows.append(existing)
                    keep = max(rows, key=lambda item: (item.last_seen_at, item.id))
                    duplicates = [item for item in rows if item.id != keep.id]
                    keep.category = category
                    keep.subject = normalize_alert_subject(keep.category, keep.subject)
                    target_key = build_alert_dedup_key(keep.category, keep.subject, keep.content)
                keep.dedup_key = target_key
                keep.count = sum(item.count or 1 for item in rows)
                keep.first_seen_at = min(item.first_seen_at for item in rows)
                keep.last_seen_at = max(item.last_seen_at for item in rows)
                keep.is_read = all(item.is_read for item in rows)
                if not keep.is_read:
                    keep.read_at = None
                else:
                    read_times = [item.read_at for item in rows if item.read_at]
                    keep.read_at = max(read_times) if read_times else keep.read_at
                delete_ids = [item.id for item in duplicates]
                SystemAlert.objects.filter(id__in=delete_ids).delete()
                keep.save(update_fields=[
                    'category',
                    'subject',
                    'dedup_key',
                    'count',
                    'first_seen_at',
                    'last_seen_at',
                    'is_read',
                    'read_at',
                ])
                merged_groups += 1
                deleted_rows += len(delete_ids)

        self.stdout.write(
            self.style.SUCCESS(
                'merged_groups={} deleted_rows={}'.format(merged_groups, deleted_rows)
            )
        )
