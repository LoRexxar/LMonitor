"""合并职业攻略与线上 NGA 数据迁移分支。"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('botend', '0214_class_guide_tag_disclaimer'),
        ('botend', '0208_nga_source_facts'),
    ]
    operations = []
