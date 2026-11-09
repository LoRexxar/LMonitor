"""合并夜间采集优化与论坛元数据迁移分支，不重复变更表结构。"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('botend', '0267_player_daily_checkpoint_and_freshness'),
        ('botend', '0267_forever_beta_channel'),
    ]

    operations = []
