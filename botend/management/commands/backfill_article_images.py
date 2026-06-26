"""
回填旧文章中的外部图片到 OSS。

用法:
    DJANGO_SETTINGS_MODULE=LMonitor.settings_dev .venv/bin/python manage.py backfill_article_images --source blizzard_tracker --limit 50
    DJANGO_SETTINGS_MODULE=LMonitor.settings_dev .venv/bin/python manage.py backfill_article_images --source wowhead --limit 50
    DJANGO_SETTINGS_MODULE=LMonitor.settings_dev .venv/bin/python manage.py backfill_article_images --dry-run
"""
import json
import re

from django.core.management.base import BaseCommand

from botend.models import WowArticle
from botend.services.article_image_service import upload_article_images_in_blocks
from utils.log import logger


class Command(BaseCommand):
    help = "回填旧文章中的外部图片到 OSS"

    def add_arguments(self, parser):
        parser.add_argument("--source", default="blizzard_tracker", help="文章来源 (default: blizzard_tracker)")
        parser.add_argument("--limit", type=int, default=50, help="每次处理的文章数 (default: 50)")
        parser.add_argument("--dry-run", action="store_true", help="只显示需要处理的文章，不实际上传")

    def handle(self, *args, **options):
        source = options["source"]
        limit = options["limit"]
        dry_run = options["dry_run"]

        articles = WowArticle.objects.filter(source=source).order_by("-id")
        processed = 0
        updated = 0
        skipped = 0
        errors = 0

        for article in articles[:limit * 3]:  # 多扫描一些，跳过不需要的
            if processed >= limit:
                break

            cb = article.content_blocks or ""
            if isinstance(cb, str):
                try:
                    blocks = json.loads(cb)
                except Exception:
                    blocks = []
            else:
                blocks = cb

            if not blocks:
                continue

            # 检查是否有外部图片
            has_external = False
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "html":
                    html = b.get("html") or ""
                    for m in re.findall(r'<img[^>]+src="([^"]+)"', html):
                        if "aliyuncs.com" not in m and "oss.wowdaily.cn" not in m:
                            has_external = True
                            break
                elif b.get("type") == "image":
                    url = (b.get("url") or "").strip()
                    if url and "aliyuncs.com" not in url and "oss.wowdaily.cn" not in url:
                        has_external = True
                if has_external:
                    break

            if not has_external:
                skipped += 1
                continue

            processed += 1
            if dry_run:
                # 统计外部图片数
                ext_count = 0
                for b in blocks:
                    if isinstance(b, dict) and b.get("type") == "html":
                        html = b.get("html") or ""
                        for m in re.findall(r'<img[^>]+src="([^"]+)"', html):
                            if "aliyuncs.com" not in m and "oss.wowdaily.cn" not in m:
                                ext_count += 1
                self.stdout.write(f"  [DRY-RUN] ID={article.id} {article.title[:60]} 外部图片={ext_count}")
                continue

            try:
                uploaded_blocks = upload_article_images_in_blocks(
                    blocks,
                    article_url=article.url or "",
                    source=source,
                )
                if uploaded_blocks and uploaded_blocks != blocks:
                    article.content_blocks = json.dumps(uploaded_blocks, ensure_ascii=False)
                    article.save(update_fields=["content_blocks"])
                    updated += 1
                    self.stdout.write(f"  ✅ ID={article.id} {article.title[:60]}")
                else:
                    skipped += 1
            except Exception as e:
                errors += 1
                logger.error(f"[backfill_article_images] error ID={article.id}: {e}")
                self.stderr.write(f"  ❌ ID={article.id} {article.title[:60]}: {e}")

        self.stdout.write(self.style.SUCCESS(
            f"\n完成: 处理={processed}, 更新={updated}, 跳过(无需处理)={skipped}, 错误={errors}"
        ))
