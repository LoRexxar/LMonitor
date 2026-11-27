# 保留本地分割前的原始日志文件名。

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0295_combat_public_directory'),
    ]

    operations = [
        migrations.AddField(
            model_name='combatlogreport',
            name='source_filename',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
    ]
