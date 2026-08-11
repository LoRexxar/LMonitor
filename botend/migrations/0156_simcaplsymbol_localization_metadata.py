from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0155_simcbenchmarkcandidate_effect'),
    ]

    operations = [
        migrations.AddField(
            model_name='simcaplsymbol',
            name='localization_source',
            field=models.CharField(
                blank=True, default='',
                help_text='本地化来源，例如 wowhead', max_length=64,
            ),
        ),
        migrations.AddField(
            model_name='simcaplsymbol',
            name='localization_status',
            field=models.CharField(
                blank=True, default='',
                help_text='本地化状态，例如 ok/unlocalized/unbound', max_length=32,
            ),
        ),
        migrations.AddField(
            model_name='simcaplsymbol',
            name='metadata',
            field=models.JSONField(
                blank=True, default=dict,
                help_text='表达式模板、覆盖专精与 Wowhead 审计元数据',
            ),
        ),
        migrations.AddField(
            model_name='simcaplsymbol',
            name='name_en',
            field=models.CharField(
                blank=True, default='',
                help_text='APL 英文名称；至少保留原始 token', max_length=255,
            ),
        ),
        migrations.AddField(
            model_name='simcaplsymbol',
            name='name_zh',
            field=models.CharField(
                blank=True, default='',
                help_text='APL 简体中文名称；上游无中文时允许为空', max_length=255,
            ),
        ),
    ]
