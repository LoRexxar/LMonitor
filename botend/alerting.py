from django.db import transaction
from django.utils import timezone

from botend.models import SystemAlert


def upsert_system_alert(category, subject, level, title, content):
    category_text = str(category or '').strip()[:64]
    subject_text = str(subject or '').strip()[:128]
    key = f"{category_text}@{subject_text}" if subject_text else category_text
    now = timezone.now()

    with transaction.atomic():
        row = SystemAlert.objects.select_for_update().filter(dedup_key=key).first()
        if row:
            row.category = category_text
            row.subject = subject_text
            row.level = int(level or 3)
            row.title = str(title or '').strip()[:200]
            row.content = str(content or '')
            row.last_seen_at = now
            row.is_read = False
            row.read_at = None
            row.save(
                update_fields=[
                    'category',
                    'subject',
                    'level',
                    'title',
                    'content',
                    'last_seen_at',
                    'is_read',
                    'read_at',
                ]
            )
            return row

        return SystemAlert.objects.create(
            category=category_text,
            subject=subject_text,
            dedup_key=key,
            level=int(level or 3),
            title=str(title or '').strip()[:200],
            content=str(content or ''),
            count=1,
            first_seen_at=now,
            last_seen_at=now,
            is_read=False,
            read_at=None,
        )
