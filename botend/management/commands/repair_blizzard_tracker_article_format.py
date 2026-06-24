from django.core.management.base import BaseCommand
from django.db import transaction

from utils.LReq import LReq
from botend.controller.plugins.portal.PortalPostMonitor import PortalPostMonitor
from botend.models import WowArticle
from botend.services.article_content_service import blocks_to_plain_text, dumps_blocks
from botend.services.article_translation_service import build_translation_service


class Command(BaseCommand):
    help = "Refetch Blizzard Tracker article bodies as structured HTML blocks."

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Write changes to database. Default is dry-run.')
        parser.add_argument('--limit', type=int, default=0)
        parser.add_argument('--id', type=int, action='append', dest='ids')
        parser.add_argument('--reset-translation', action='store_true', help='Clear Chinese content fields after source content changes.')
        parser.add_argument('--translate', action='store_true', help='Translate repaired articles after saving source content.')

    def handle(self, *args, **options):
        apply_changes = bool(options.get('apply'))
        limit = int(options.get('limit') or 0)
        ids = options.get('ids') or []
        reset_translation = bool(options.get('reset_translation'))
        translate_after = bool(options.get('translate'))
        translation_service = build_translation_service() if translate_after else None
        monitor = PortalPostMonitor(LReq(is_chrome=False, is_cloak=False), None)

        qs = WowArticle.objects.filter(source='blizzard_tracker').exclude(url__isnull=True).exclude(url='').order_by('-publish_time', '-id')
        if ids:
            qs = qs.filter(id__in=ids)
        if limit > 0:
            qs = qs[:limit]

        scanned = 0
        repaired = 0
        skipped = 0
        failed = 0
        unsafe = 0

        for article in qs:
            scanned += 1
            try:
                blocks = monitor._fetch_blizzard_tracker_blocks(article.url)
                body = blocks_to_plain_text(blocks)
                if not body or len(body.strip()) < 20:
                    unsafe += 1
                    self.stdout.write(self.style.WARNING(f"unsafe-skip id={article.id} title={article.title[:80]}"))
                    continue

                update_fields = []
                blocks_raw = dumps_blocks(blocks)
                if article.content != body:
                    article.content = body
                    update_fields.append('content')
                if article.description != body[:1200]:
                    article.description = body[:1200]
                    update_fields.append('description')
                if article.content_blocks != blocks_raw:
                    article.content_blocks = blocks_raw
                    update_fields.append('content_blocks')

                if update_fields and (reset_translation or translate_after):
                    if article.content_cn:
                        article.content_cn = ''
                        update_fields.append('content_cn')
                    if article.content_blocks_cn:
                        article.content_blocks_cn = ''
                        update_fields.append('content_blocks_cn')

                if not update_fields:
                    skipped += 1
                    continue

                repaired += 1
                self.stdout.write(
                    f"repair id={article.id} blocks={len(blocks)} chars={len(body)} title={article.title[:80]}"
                )
                if apply_changes:
                    with transaction.atomic():
                        article.save(update_fields=sorted(set(update_fields)))
                    if translate_after and translation_service:
                        translation_service.translate_article_fields(article, logger_prefix='repair_blizzard_tracker_article_format')
            except Exception as exc:
                failed += 1
                self.stdout.write(self.style.WARNING(f"failed id={article.id} error={exc}"))

        mode = 'APPLY' if apply_changes else 'DRY-RUN'
        self.stdout.write(
            self.style.SUCCESS(
                f"{mode} done scanned={scanned} repaired={repaired} skipped={skipped} failed={failed} unsafe={unsafe}"
            )
        )
