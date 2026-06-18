# Generated for WoW item metadata snapshot

from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0085_wowtalentnodemetadata_add_description'),
    ]

    operations = [
        migrations.CreateModel(
            name='WowItemSnapshot',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('item_id', models.BigIntegerField(help_text='物品ID（装备/宝石/附魔通用）', unique=True)),
                ('name', models.CharField(blank=True, default='', help_text='英文名称', max_length=255)),
                ('name_zh', models.CharField(blank=True, default='', help_text='中文名称', max_length=255)),
                ('description', models.TextField(blank=True, default='', help_text='英文描述')),
                ('description_zh', models.TextField(blank=True, default='', help_text='中文描述')),
                ('icon', models.CharField(blank=True, default='', help_text='图标名称', max_length=255)),
                ('quality', models.IntegerField(blank=True, default=0, help_text='品质等级')),
                ('source', models.CharField(blank=True, default='wowhead', help_text='数据源', max_length=32)),
                ('updated_at', models.DateTimeField(default=django.utils.timezone.now, help_text='更新时间')),
            ],
            options={
                'verbose_name': 'WoW物品元数据快照',
                'verbose_name_plural': 'WoW物品元数据快照',
                'db_table': 'wow_item_snapshot',
                'indexes': [models.Index(fields=['item_id'], name='idx_item_snapshot_id'), models.Index(fields=['updated_at'], name='idx_item_snapshot_updated')],
            },
        ),
    ]
