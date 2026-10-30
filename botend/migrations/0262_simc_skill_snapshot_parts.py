from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError
import django.db.models.deletion


def reject_lossy_reverse(apps, schema_editor):
    """Never turn fragmented immutable facts into apparently valid empty rows."""
    using = schema_editor.connection.alias
    actor = apps.get_model('botend', 'SimcSkillDamageSnapshotActor')
    part = apps.get_model('botend', 'SimcSkillDamageSnapshotPart')
    if (part.objects.using(using).exists()
            or actor.objects.using(using).exclude(payload_storage={}).exists()):
        raise IrreversibleError(
            'Snapshot JSON parts exist: retain the part table, manifest and compatible reader.'
        )


class Migration(migrations.Migration):
    dependencies = [('botend', '0261_talent_server_slots')]

    operations = [
        migrations.AddField(
            model_name='simcskilldamagesnapshotactor',
            name='payload_storage',
            field=models.JSONField(blank=True, db_default={}, default=dict),
        ),
        migrations.CreateModel(
            name='SimcSkillDamageSnapshotPart',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('document', models.CharField(
                    choices=[
                        ('actor_payload', 'Actor payload'),
                        ('unresolved_payload', 'Unresolved payload'),
                    ],
                    max_length=32,
                )),
                ('ordinal', models.PositiveIntegerField()),
                ('text', models.TextField()),
                ('actor', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='payload_parts',
                    to='botend.simcskilldamagesnapshotactor',
                )),
            ],
            options={
                'db_table': 'simc_skill_damage_snapshot_part',
                'ordering': ['document', 'ordinal'],
                'constraints': [
                    models.UniqueConstraint(
                        fields=('actor', 'document', 'ordinal'),
                        name='simc_skill_actor_doc_part_uniq',
                    ),
                ],
            },
        ),
        # Reverse runs this guard first, before dropping either the table or marker.
        migrations.RunPython(migrations.RunPython.noop, reject_lossy_reverse),
    ]
