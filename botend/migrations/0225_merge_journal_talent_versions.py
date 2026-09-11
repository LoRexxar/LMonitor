"""合并冒险手册与职业天赋版本迁移分支。"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('botend', '0224_merge_journal_navigation'),
        ('botend', '0222_normalize_talent_versions'),
    ]
    operations = []
