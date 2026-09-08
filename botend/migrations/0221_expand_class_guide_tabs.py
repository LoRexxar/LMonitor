"""将存量攻略的选项卡展开为纵向 Markdown 章节。"""
import hashlib
import json

from django.db import migrations
from django.utils import timezone


def content_hash(title, markdown):
    return hashlib.sha256(json.dumps([title, markdown], ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def expand_articles(apps, schema_editor):
    from botend.services.class_guide_markdown import normalize_tab_markdown

    Guide = apps.get_model('botend', 'ClassGuide')
    records = Guide.objects.using(schema_editor.connection.alias)
    for guide in records.all().iterator():
        markdown = normalize_tab_markdown(guide.content_markdown)
        source = normalize_tab_markdown(guide.source_markdown)
        if markdown == guide.content_markdown and source == guide.source_markdown:
            continue
        changes = {'content_markdown': markdown, 'source_markdown': source, 'updated_at': timezone.now()}
        # 结构转换不应让自动稿误判成人工稿，也不能取消真正的人工保护。
        if guide.imported_content_hash == content_hash(guide.title, guide.content_markdown):
            changes['imported_content_hash'] = content_hash(guide.title, markdown)
        if records.filter(pk=guide.pk, updated_at=guide.updated_at).update(**changes) != 1:
            raise RuntimeError('攻略 {} 正在被编辑，请完成保存后重新迁移'.format(guide.pk))


class Migration(migrations.Migration):
    dependencies = [('botend', '0220_consolidate_wow_localization')]
    operations = [migrations.RunPython(expand_articles, migrations.RunPython.noop)]
