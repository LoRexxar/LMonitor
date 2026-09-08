"""将早期内部测试的块式文章转换为单篇 Markdown，不新增语义修订。"""

from django.core.management.base import BaseCommand

from botend.guide_models import ClassGuideRevision
from botend.services.class_guide_markdown import blocks_to_markdown, compile_markdown
from botend.services.class_guide_service import markdown_audit


class Command(BaseCommand):
    help = '转换旧攻略修订为 Markdown；已有 Markdown 的修订不变'

    def handle(self, *args, **options):
        count = 0
        for revision in ClassGuideRevision.objects.filter(content_markdown='').iterator():
            markdown = blocks_to_markdown(revision.blocks)
            derived = compile_markdown(markdown)
            audit = markdown_audit(revision.audit, revision.blocks)
            ClassGuideRevision.objects.filter(pk=revision.pk, content_markdown='').update(
                content_markdown=markdown, source_markdown=blocks_to_markdown(revision.source_blocks),
                blocks=derived, audit=audit)
            count += 1
        self.stdout.write(self.style.SUCCESS('已转换 {} 个历史修订'.format(count)))
