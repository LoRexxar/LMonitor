"""为已有 Maxroll 来源攻略补充统一标签，保留其它标签。"""
from urllib.parse import urlparse

from django.db import migrations


def add_maxroll_tag(apps, schema_editor):
    Guide = apps.get_model('botend', 'ClassGuide')
    Tag = apps.get_model('botend', 'ClassGuideTag')
    alias = schema_editor.connection.alias
    tag = None
    for guide in Guide.objects.using(alias).exclude(source_url='').iterator():
        if urlparse(guide.source_url).hostname not in ('maxroll.gg', 'www.maxroll.gg'):
            continue
        if tag is None:
            tag = Tag.objects.using(alias).filter(name__iexact='maxroll').first()
            if tag is None:
                tag, _ = Tag.objects.using(alias).get_or_create(name='maxroll')
        guide.tags.add(tag)


class Migration(migrations.Migration):
    dependencies = [('botend', '0212_class_guide_author_profiles')]
    operations = [migrations.RunPython(add_maxroll_tag, migrations.RunPython.noop)]
