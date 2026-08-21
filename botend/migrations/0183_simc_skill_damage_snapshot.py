from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('botend', '0182_spec_raid_ranking_difficulty')]

    operations = [
        migrations.CreateModel(
            name='SimcSkillDamageSnapshot',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('simc_revision', models.CharField(max_length=40)),
                ('game_build', models.CharField(max_length=64)),
                ('schema_revision', models.PositiveIntegerField(default=1)),
                ('status', models.CharField(choices=[('pending', '待生成'), ('running', '生成中'), ('succeeded', '已完成'), ('failed', '失败')], default='pending', max_length=16)),
                ('payload', models.JSONField(blank=True, default=dict)),
                ('error_text', models.TextField(blank=True, default='')),
                ('generated_spec_count', models.PositiveIntegerField(default=0)),
                ('generated_action_count', models.PositiveIntegerField(default=0)),
                ('requested_by_id', models.BigIntegerField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'db_table': 'simc_skill_damage_snapshot',
                'ordering': ['-created_at', '-id'],
                'indexes': [models.Index(fields=['status', 'completed_at'], name='simc_skill_status_9a7808_idx')],
                'constraints': [models.UniqueConstraint(fields=('simc_revision', 'game_build', 'schema_revision'), name='simc_skill_damage_dataset_identity_uniq')],
            },
        ),
    ]
