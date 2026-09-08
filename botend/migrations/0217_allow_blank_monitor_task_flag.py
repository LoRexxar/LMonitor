"""允许尚未运行的监控任务保留空标记，避免后台启用时校验失败。"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('botend', '0216_register_class_guide_monitor')]

    operations = [
        migrations.AlterField(
            model_name='monitortask',
            name='flag',
            field=models.CharField(blank=True, default=None, max_length=2000, null=True),
        ),
    ]
