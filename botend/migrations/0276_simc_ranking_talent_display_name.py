"""排名展示名称由写入端维护，读取端不解析攻略或天赋。"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('botend', '0275_simc_reference_ranking_entry')]

    operations = [migrations.AddField(
        model_name='simcreferencerankingentry', name='talent_display_name',
        field=models.TextField(default='', blank=True),
    )]
