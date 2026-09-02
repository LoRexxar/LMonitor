from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('botend', '0190_reconcile_gear_catalog_seasons'),
    ]

    operations = [
        migrations.CreateModel(
            name='GearBuilderOwnedItem',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('item_id', models.BigIntegerField(db_index=True, default=0)),
                ('slot_key', models.CharField(blank=True, db_index=True, default='', max_length=32)),
                ('item_level', models.PositiveIntegerField(default=0)),
                ('batch_key', models.CharField(blank=True, default='', max_length=160)),
                ('source', models.CharField(choices=[('manual', '手动加入'), ('simc_equipped', 'SimC 已装备'), ('simc_bag', 'SimC 背包')], default='manual', max_length=24)),
                ('fingerprint', models.CharField(max_length=64)),
                ('quantity', models.PositiveSmallIntegerField(default=1)),
                ('bonus_ids', models.JSONField(blank=True, default=list)),
                ('selected_stats', models.JSONField(blank=True, default=list)),
                ('enhancements_json', models.JSONField(blank=True, default=dict)),
                ('snapshot_json', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='gear_builder_owned_items', to=settings.AUTH_USER_MODEL)),
                ('variant', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='owned_item_records', to='botend.wowitemvariantsnapshot')),
            ],
            options={
                'db_table': 'gear_builder_owned_item',
                'ordering': ('slot_key', '-item_level', '-updated_at', '-id'),
            },
        ),
        migrations.AddConstraint(
            model_name='gearbuilderowneditem',
            constraint=models.UniqueConstraint(fields=('user', 'fingerprint'), name='uniq_gear_owned_user_fingerprint'),
        ),
        migrations.AddIndex(
            model_name='gearbuilderowneditem',
            index=models.Index(fields=['user', 'slot_key'], name='gear_owned_user_slot_idx'),
        ),
        migrations.AddIndex(
            model_name='gearbuilderowneditem',
            index=models.Index(fields=['user', '-updated_at'], name='gear_owned_user_updated_idx'),
        ),
    ]
