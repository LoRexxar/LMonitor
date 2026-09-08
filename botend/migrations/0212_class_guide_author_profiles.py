# 攻略来源作者资料和可选自定义覆盖。

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0211_class_guide_navigation'),
    ]

    operations = [
        migrations.AddField(
            model_name='classguide',
            name='author_profile',
            field=models.JSONField(blank=True, default=None, null=True, verbose_name='自定义作者资料'),
        ),
        migrations.AddField(
            model_name='classguide',
            name='source_author_profile',
            field=models.JSONField(blank=True, default=dict, verbose_name='来源作者资料'),
        ),
    ]
