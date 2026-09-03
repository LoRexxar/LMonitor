import json
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from botend.models import WowArticle


class BackfillArticleImagesCommandTests(TestCase):
    def test_repairs_discourse_markup_and_srcset_in_both_block_fields(self):
        def blocks(text):
            return json.dumps(
                [
                    {
                        "type": "html",
                        "html": (
                            f"<p>{text}</p>"
                            '<div class="lightbox-wrapper">'
                            '<a class="lightbox" href="https://oss.wowdaily.cn/portal/articles/blizzard_tracker/a.jpg">'
                            '<img alt="Screenshot" sizes="100vw" '
                            'src="https://oss.wowdaily.cn/portal/articles/blizzard_tracker/a.jpg" '
                            'srcset="https://cdn.example.com/a-2x.jpg 2x">'
                            '<div class="meta"><span class="filename">a.jpg</span>'
                            '<span class="informations">1920×1080 234 KB</span></div>'
                            "</a></div>"
                        ),
                        "original_html": (
                            '<div class="lightbox-wrapper"><a class="lightbox" '
                            'href="https://oss.wowdaily.cn/portal/articles/blizzard_tracker/a.jpg">'
                            '<img src="https://oss.wowdaily.cn/portal/articles/blizzard_tracker/a.jpg" '
                            'srcset="https://cdn.example.com/a.jpg 1x">'
                            '<span class="meta">original metadata</span></a></div>'
                        ),
                    }
                ],
                ensure_ascii=False,
            )

        article = WowArticle.objects.create(
            title="Bluepost",
            url="https://us.forums.blizzard.com/en/wow/t/test/1",
            source="blizzard_tracker",
            content_blocks=blocks("Source text"),
            content_blocks_cn=blocks("中文文本"),
        )

        stdout = StringIO()
        call_command("backfill_article_images", article_id=article.id, limit=1, stdout=stdout)

        article.refresh_from_db()
        for field, expected_text in (
            ("content_blocks", "Source text"),
            ("content_blocks_cn", "中文文本"),
        ):
            html = json.loads(getattr(article, field))[0]["html"]
            self.assertIn(expected_text, html)
            self.assertIn('class="article-image-link"', html)
            self.assertNotIn("lightbox-wrapper", html)
            self.assertNotIn('class="meta"', html)
            self.assertNotIn("srcset", html)
            self.assertNotIn("sizes", html)
            original_html = json.loads(getattr(article, field))[0]["original_html"]
            self.assertIn('class="article-image-link"', original_html)
            self.assertNotIn("lightbox-wrapper", original_html)
            self.assertNotIn("srcset", original_html)

        stdout = StringIO()
        call_command("backfill_article_images", article_id=article.id, limit=1, stdout=stdout)
        article.refresh_from_db()
        self.assertIn("更新=0", stdout.getvalue())
        self.assertNotIn("lightbox-wrapper", article.content_blocks)
        self.assertNotIn("lightbox-wrapper", article.content_blocks_cn)
