import re

from django.db import transaction
from django.utils import timezone

from botend.models import SystemAlert


_LOG_PREFIX_RE = re.compile(
    r'^\[(?P<level>[A-Z]+)\]\s+'
    r'(?:\[(?P<thread>[^\]]+)\]\s+)?'
    r'(?:\[(?P<time>\d{4}-\d{2}-\d{2}[^\]]+|\d{2}:\d{2}:\d{2}(?:,\d+)?)\]\s+)?'
    r'(?:\[(?P<source>[^\]:]+\.py:\d+)\]\s+)?'
)
_THREAD_TOKEN_RE = re.compile(r'\s*\[Thread-[^\]]+\]')
_TIMESTAMP_TOKEN_RE = re.compile(r'\[(?:\d{4}-\d{2}-\d{2}[^\]]+|\d{2}:\d{2}:\d{2}(?:,\d+)?)\]\s*')
_WHITESPACE_RE = re.compile(r'\s+')


def normalize_alert_subject(category, subject):
    subject_text = str(subject or '').strip()
    if str(category or '').strip() == 'ERROR_LOG':
        subject_text = _THREAD_TOKEN_RE.sub('', subject_text).strip()
    return subject_text[:128]


def normalize_alert_content(category, content):
    content_text = str(content or '').strip()
    if str(category or '').strip() != 'ERROR_LOG':
        return content_text

    text = _LOG_PREFIX_RE.sub('', content_text, count=1)
    text = _TIMESTAMP_TOKEN_RE.sub('', text)
    text = _THREAD_TOKEN_RE.sub('', text)
    return _WHITESPACE_RE.sub(' ', text).strip()


def build_alert_dedup_key(category, subject, content=None):
    category_text = str(category or '').strip()[:64]
    subject_text = normalize_alert_subject(category_text, subject)
    key = f"{category_text}@{subject_text}" if subject_text else category_text
    if category_text == 'ERROR_LOG':
        fingerprint = normalize_alert_content(category_text, content)
        if fingerprint:
            key = f"{key}@{fingerprint[:120]}"
    return key[:220]


def upsert_system_alert(category, subject, level, title, content):
    category_text = str(category or '').strip()[:64]
    subject_text = normalize_alert_subject(category_text, subject)
    key = build_alert_dedup_key(category_text, subject_text, content)
    now = timezone.now()

    with transaction.atomic():
        row = SystemAlert.objects.select_for_update().filter(dedup_key=key).first()
        if row is None and category_text == 'ERROR_LOG' and subject_text:
            fingerprint = normalize_alert_content(category_text, content)
            legacy_rows = SystemAlert.objects.select_for_update().filter(
                category=category_text,
                subject__startswith=subject_text,
            ).order_by('-last_seen_at', '-id')
            for legacy_row in legacy_rows:
                if normalize_alert_content(category_text, legacy_row.content) == fingerprint:
                    legacy_row.dedup_key = key
                    row = legacy_row
                    break
        if row:
            row.category = category_text
            row.subject = subject_text
            row.level = int(level or 3)
            row.title = str(title or '').strip()[:200]
            row.content = str(content or '')
            row.count = (row.count or 0) + 1
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
                    'count',
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
