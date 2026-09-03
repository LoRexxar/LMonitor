"""
回填旧文章中的外部图片到 OSS，并规范化来源站点的图片组件。

用法:
    DJANGO_SETTINGS_MODULE=LMonitor.settings_dev .venv/bin/python manage.py backfill_article_images --source blizzard_tracker --limit 50
    DJANGO_SETTINGS_MODULE=LMonitor.settings_dev .venv/bin/python manage.py backfill_article_images --article-id 33490
    DJANGO_SETTINGS_MODULE=LMonitor.settings_dev .venv/bin/python manage.py backfill_article_images --dry-run
"""
import json

from bs4 import BeautifulSoup
from django.core.management.base import BaseCommand

from botend.models import WowArticle
from botend.services.article_content_service import normalize_blizzard_tracker_html_fragment
from botend.services.article_image_service import (
    _is_external_article_image_url,
    upload_article_images_in_blocks,
)
from utils.log import logger


BLOCK_FIELDS = ("content_blocks", "content_blocks_cn")


def _loads_blocks(value):
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    return value if isinstance(value, list) else []


def _is_external_image_url(value):
    return _is_external_article_image_url(value)


def _html_needs_processing(html_text, *, source):
    if not html_text:
        return False
    soup = BeautifulSoup(str(html_text), "html.parser")
    if source == "blizzard_tracker" and soup.select_one(".lightbox-wrapper"):
        return True
    for img in soup.find_all("img"):
        if any(attr in img.attrs for attr in ("srcset", "sizes")):
            return True
        if _is_external_image_url(img.get("data-source-src")):
            return True
        if _is_external_image_url(img.get("src")):
            return True
    for source_tag in soup.find_all("source"):
        if any(attr in source_tag.attrs for attr in ("srcset", "sizes", "data-srcset")):
            return True
    for link in soup.find_all("a"):
        href = (link.get("href") or "").split("?", 1)[0].lower()
        if href.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")) and _is_external_image_url(link.get("href")):
            return True
    return False


def _blocks_need_processing(blocks, *, source):
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "html":
            for key in ("html", "original_html"):
                if _html_needs_processing(block.get(key), source=source):
                    return True
        elif block.get("type") == "image" and _is_external_image_url(block.get("url")):
            return True
    return False


def _process_blocks(blocks, *, article_url, source, upload_cache=None):
    normalized = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        new_block = dict(block)
        if source == "blizzard_tracker" and new_block.get("type") == "html":
            for key in ("html", "original_html"):
                if new_block.get(key):
                    new_block[key] = normalize_blizzard_tracker_html_fragment(new_block[key])
        normalized.append(new_block)
    return upload_article_images_in_blocks(
        normalized,
        article_url=article_url,
        source=source,
        upload_cache=upload_cache,
    )


class Command(BaseCommand):
    help = "回填旧文章图片到 OSS，并清理来源站点的图片组件格式"

    def add_arguments(self, parser):
        parser.add_argument("--source", default="blizzard_tracker", help="文章来源 (default: blizzard_tracker)")
        parser.add_argument("--limit", type=int, default=50, help="每次处理的文章数 (default: 50)")
        parser.add_argument("--article-id", type=int, help="只处理指定文章 ID")
        parser.add_argument("--dry-run", action="store_true", help="只显示需要处理的文章，不实际上传或保存")

    def handle(self, *args, **options):
        source = options["source"]
        limit = options["limit"]
        article_id = options.get("article_id")
        dry_run = options["dry_run"]

        articles = WowArticle.objects.filter(source=source).order_by("-id")
        if article_id is not None:
            articles = articles.filter(id=article_id)

        processed = 0
        updated = 0
        skipped = 0
        errors = 0

        for article in articles.iterator():
            field_blocks = {
                field: _loads_blocks(getattr(article, field, ""))
                for field in BLOCK_FIELDS
            }
            candidate_fields = [
                field
                for field, blocks in field_blocks.items()
                if _blocks_need_processing(blocks, source=source)
            ]
            if not candidate_fields:
                skipped += 1
                continue
            if processed >= limit:
                break

            processed += 1
            if dry_run:
                self.stdout.write(
                    f"  [DRY-RUN] ID={article.id} {(article.title or '')[:60]} 字段={','.join(candidate_fields)}"
                )
                continue

            try:
                update_fields = []
                upload_cache = {}
                for field in candidate_fields:
                    blocks = field_blocks[field]
                    processed_blocks = _process_blocks(
                        blocks,
                        article_url=article.url or "",
                        source=source,
                        upload_cache=upload_cache,
                    )
                    if processed_blocks != blocks:
                        setattr(article, field, json.dumps(processed_blocks, ensure_ascii=False))
                        update_fields.append(field)
                if update_fields:
                    article.save(update_fields=update_fields)
                    updated += 1
                    self.stdout.write(
                        f"  ✅ ID={article.id} {(article.title or '')[:60]} 字段={','.join(update_fields)}"
                    )
                else:
                    skipped += 1
            except Exception as exc:
                errors += 1
                logger.error(f"[backfill_article_images] error ID={article.id}: {exc}")
                self.stderr.write(f"  ❌ ID={article.id} {(article.title or '')[:60]}: {exc}")

        self.stdout.write(self.style.SUCCESS(
            f"\n完成: 处理={processed}, 更新={updated}, 跳过(无需处理)={skipped}, 错误={errors}"
        ))
