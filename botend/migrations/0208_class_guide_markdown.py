# 职业攻略单篇 Markdown 正文与原文字段。

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0207_class_guides'),
    ]

    operations = [
        migrations.AddField(
            model_name='classguiderevision',
            name='content_markdown',
            field=models.TextField(blank=True, verbose_name='Markdown 正文'),
        ),
        migrations.AddField(
            model_name='classguiderevision',
            name='source_markdown',
            field=models.TextField(blank=True, verbose_name='原文 Markdown'),
        ),
    ]
