from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0186_mythic_default_route_display_updated_on'),
    ]

    operations = [
        migrations.AddField(
            model_name='seasonmeta',
            name='delve_sources',
            field=models.JSONField(blank=True, default=list, verbose_name='地下堡来源'),
        ),
        migrations.AddField(
            model_name='seasonmeta',
            name='game_build',
            field=models.CharField(blank=True, default='', max_length=64, verbose_name='正式服构建'),
        ),
        migrations.AddField(
            model_name='seasonmeta',
            name='gear_batch_key',
            field=models.CharField(blank=True, default='', max_length=80, verbose_name='装备目录批次'),
        ),
        migrations.AddField(
            model_name='seasonmeta',
            name='gear_sync_report',
            field=models.JSONField(blank=True, default=dict, verbose_name='装备目录同步报告'),
        ),
        migrations.AddField(
            model_name='seasonmeta',
            name='gear_sync_status',
            field=models.CharField(blank=True, default='', max_length=32, verbose_name='装备目录同步状态'),
        ),
        migrations.AddField(
            model_name='seasonmeta',
            name='gear_synced_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='装备目录同步时间'),
        ),
        migrations.AddField(
            model_name='wowitemsnapshot',
            name='allowable_class_mask',
            field=models.BigIntegerField(blank=True, default=0),
        ),
        migrations.AddField(
            model_name='wowitemsnapshot',
            name='armor_type',
            field=models.CharField(blank=True, default='', max_length=32),
        ),
        migrations.AddField(
            model_name='wowitemsnapshot',
            name='catalog_type',
            field=models.CharField(blank=True, db_index=True, default='', help_text='装备/制造/美化/宝石/附魔', max_length=32),
        ),
        migrations.AddField(
            model_name='wowitemsnapshot',
            name='effect_refs',
            field=models.JSONField(blank=True, default=list, help_text='Spell/ItemEffect 等稳定引用'),
        ),
        migrations.AddField(
            model_name='wowitemsnapshot',
            name='eligible_specs',
            field=models.JSONField(blank=True, default=list, help_text='允许使用的 class:spec 列表'),
        ),
        migrations.AddField(
            model_name='wowitemsnapshot',
            name='enchantment_id',
            field=models.BigIntegerField(blank=True, db_index=True, default=0),
        ),
        migrations.AddField(
            model_name='wowitemsnapshot',
            name='inventory_type',
            field=models.IntegerField(blank=True, default=0, help_text='DB2 InventoryType'),
        ),
        migrations.AddField(
            model_name='wowitemsnapshot',
            name='item_class_id',
            field=models.IntegerField(blank=True, default=0),
        ),
        migrations.AddField(
            model_name='wowitemsnapshot',
            name='item_subclass_id',
            field=models.IntegerField(blank=True, default=0),
        ),
        migrations.AddField(
            model_name='wowitemsnapshot',
            name='metadata',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='wowitemsnapshot',
            name='simc_token',
            field=models.CharField(blank=True, db_index=True, default='', max_length=128),
        ),
        migrations.AddField(
            model_name='wowitemsnapshot',
            name='slot_key',
            field=models.CharField(blank=True, db_index=True, default='', help_text='规范化装备槽位', max_length=32),
        ),
        migrations.AddField(
            model_name='wowitemsnapshot',
            name='unique_group',
            field=models.CharField(blank=True, db_index=True, default='', max_length=128),
        ),
        migrations.AddField(
            model_name='wowitemsnapshot',
            name='weapon_type',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.CreateModel(
            name='WowItemVariantSnapshot',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('batch_key', models.CharField(max_length=80)),
                ('game_build', models.CharField(blank=True, default='', max_length=64)),
                ('variant_key', models.CharField(max_length=160)),
                ('variant_type', models.CharField(choices=[('drop_equipment', '掉落装备'), ('crafted_equipment', '制造装备'), ('embellishment', '美化'), ('gem', '宝石'), ('enchant', '附魔')], max_length=32)),
                ('item_level', models.PositiveIntegerField(default=0)),
                ('upgrade_track', models.CharField(blank=True, default='', max_length=16)),
                ('track_rank', models.PositiveSmallIntegerField(default=0)),
                ('track_max_rank', models.PositiveSmallIntegerField(default=0)),
                ('crafting_quality', models.PositiveSmallIntegerField(default=0)),
                ('bonus_ids', models.JSONField(blank=True, default=list)),
                ('compatible_slots', models.JSONField(blank=True, default=list)),
                ('socket_types', models.JSONField(blank=True, default=list)),
                ('socket_count', models.PositiveSmallIntegerField(default=0)),
                ('stats_json', models.JSONField(blank=True, default=dict)),
                ('effects_json', models.JSONField(blank=True, default=list)),
                ('source_json', models.JSONField(blank=True, default=list)),
                ('crafting_options', models.JSONField(blank=True, default=dict)),
                ('unique_group', models.CharField(blank=True, default='', max_length=128)),
                ('max_equipped', models.PositiveSmallIntegerField(default=0)),
                ('is_intrinsic_embellishment', models.BooleanField(default=False)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('item', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='gear_variants', to='botend.wowitemsnapshot')),
                ('season', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='gear_variants', to='botend.seasonmeta')),
            ],
            options={
                'verbose_name': 'WoW物品赛季变体',
                'verbose_name_plural': 'WoW物品赛季变体',
                'db_table': 'wow_item_variant_snapshot',
                'ordering': ('item__name_zh', 'item_level', 'variant_key'),
                'indexes': [
                    models.Index(fields=['season', 'batch_key', 'variant_type'], name='gear_var_batch_type_idx'),
                    models.Index(fields=['batch_key', 'variant_type', 'item_level'], name='gear_var_type_level_idx'),
                    models.Index(fields=['batch_key', 'item'], name='gear_var_batch_item_idx'),
                ],
                'constraints': [
                    models.UniqueConstraint(fields=('season', 'batch_key', 'item', 'variant_key'), name='uniq_item_variant_batch_key'),
                ],
            },
        ),
    ]
