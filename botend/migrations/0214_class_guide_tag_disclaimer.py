"""通过标签维护统一免责声明，不修改文章正文。"""
from django.db import migrations, models


def seed_disclaimer(apps, schema_editor):
    Tag = apps.get_model('botend', 'ClassGuideTag')
    tags = Tag.objects.using(schema_editor.connection.alias)
    tag = tags.filter(name__iexact='maxroll').first()
    if tag is None:
        tag = tags.create(name='maxroll')
    tag.disclaimer = '本站尊重攻略内容的原创以及版权，以下内容全量采集与maxroll.gg的职业攻略板块。由于原文无中文版本，故转载翻译。maxroll攻略主体由Echo攻略专业玩家完成，请关注更新时间，如涉及到转载授权可以联系我进行修改。'
    tag.save(update_fields=['disclaimer'])


class Migration(migrations.Migration):
    dependencies = [('botend', '0213_class_guide_maxroll_tag')]
    operations = [
        migrations.AddField(model_name='classguidetag', name='disclaimer',
                            field=models.TextField('统一免责声明', blank=True, default='', max_length=3000)),
        migrations.RunPython(seed_disclaimer, migrations.RunPython.noop),
    ]
