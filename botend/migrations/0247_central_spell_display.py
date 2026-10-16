from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0246_central_item_effects'),
    ]

    operations = [
        migrations.AddField(
            model_name='wowspellsnapshot',
            name='display_data',
            field=models.JSONField(blank=True, default=dict, help_text='后台合成的最终展示资料及专精差异；页面只读'),
        ),
        migrations.AddField(
            model_name='wowtalentnodemetadata',
            name='is_current',
            field=models.BooleanField(db_index=True, default=True, help_text='是否属于当前分支节点集合；退役节点保留引用'),
        ),
    ]
