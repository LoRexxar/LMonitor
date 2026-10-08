"""新建监控任务默认每小时执行，不批量修改已有任务。"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('botend', '0234_wow_data_update_schedule')]
    operations = [migrations.AlterField(
        model_name='monitortask', name='wait_time', field=models.IntegerField(default=3600),
    )]
