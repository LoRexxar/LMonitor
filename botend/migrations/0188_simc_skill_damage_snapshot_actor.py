from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0187_gear_builder_catalog'),
    ]

    operations = [
        migrations.CreateModel(
            name='SimcSkillDamageSnapshotActor',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('ordinal', models.PositiveIntegerField()),
                ('class_name', models.CharField(max_length=64)),
                ('specialization', models.CharField(max_length=64)),
                ('actor_payload', models.JSONField(default=dict)),
                ('unresolved_payload', models.JSONField(blank=True, default=list)),
                ('raw_action_count', models.PositiveIntegerField(default=0)),
                ('display_action_count', models.PositiveIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('snapshot', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='actor_rows',
                    to='botend.simcskilldamagesnapshot',
                )),
            ],
            options={
                'db_table': 'simc_skill_damage_snapshot_actor',
                'ordering': ['ordinal', 'id'],
                'constraints': [
                    models.UniqueConstraint(
                        fields=('snapshot', 'class_name', 'specialization'),
                        name='simc_skill_snapshot_actor_identity_uniq',
                    ),
                    models.UniqueConstraint(
                        fields=('snapshot', 'ordinal'),
                        name='simc_skill_snapshot_actor_ordinal_uniq',
                    ),
                ],
            },
        ),
    ]
