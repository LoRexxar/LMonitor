from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0191_gear_builder_owned_item'),
    ]

    operations = [
        migrations.CreateModel(
            name='WowTodaySnapshot',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('snapshot_date', models.DateField()),
                ('region', models.CharField(default='na', max_length=16)),
                ('source_region', models.CharField(default='US', max_length=16)),
                ('game_version', models.CharField(default='retail', max_length=24)),
                ('expansion_id', models.PositiveSmallIntegerField(default=0)),
                ('expansion_name', models.CharField(default='当前版本', max_length=64)),
                ('source_url', models.CharField(default='https://www.wowhead.com/today-in-wow', max_length=500)),
                ('content_hash', models.CharField(blank=True, default='', max_length=64)),
                ('sections_json', models.JSONField(blank=True, default=list)),
                ('raw_json', models.JSONField(blank=True, default=list)),
                ('translation_missing', models.PositiveIntegerField(default=0)),
                ('fetched_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'wow_today_snapshot',
                'ordering': ('-snapshot_date', '-fetched_at', '-id'),
            },
        ),
        migrations.AddConstraint(
            model_name='wowtodaysnapshot',
            constraint=models.UniqueConstraint(
                fields=('snapshot_date', 'region', 'game_version'),
                name='uniq_wow_today_snapshot_scope_date',
            ),
        ),
        migrations.AddIndex(
            model_name='wowtodaysnapshot',
            index=models.Index(fields=['region', 'game_version', '-fetched_at'], name='wow_today_scope_fetch_idx'),
        ),
        migrations.AddIndex(
            model_name='wowtodaysnapshot',
            index=models.Index(fields=['snapshot_date'], name='wow_today_date_idx'),
        ),
    ]
