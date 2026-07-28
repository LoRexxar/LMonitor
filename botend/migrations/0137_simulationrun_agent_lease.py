from django.db import migrations, models


def deduplicate_run_artifacts(apps, schema_editor):
    Artifact = apps.get_model('botend', 'SimcTaskArtifact')
    duplicates = (Artifact.objects.exclude(run_id__isnull=True)
                  .values('task_id', 'run_id', 'artifact_type')
                  .annotate(total=models.Count('id')).filter(total__gt=1))
    for row in duplicates.iterator():
        ids = list(Artifact.objects.filter(
            task_id=row['task_id'], run_id=row['run_id'],
            artifact_type=row['artifact_type'],
        ).order_by('-created_at', '-id').values_list('id', flat=True))
        Artifact.objects.filter(id__in=ids[1:]).delete()


class Migration(migrations.Migration):
    dependencies = [('botend', '0136_simc_agent_identity')]

    operations = [
        migrations.AddField(model_name='simulationrun', name='lease_token_hash',
                            field=models.CharField(blank=True, default='', max_length=80)),
        migrations.AddField(model_name='simulationrun', name='lease_expires_at',
                            field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name='simulationrun', name='lease_heartbeat_at',
                            field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name='simulationrun', name='lease_instance_id',
                            field=models.CharField(blank=True, default='', max_length=128)),
        migrations.AddField(model_name='simulationrun', name='lease_agent',
                            field=models.ForeignKey(blank=True, null=True,
                                on_delete=models.SET_NULL, related_name='leased_runs',
                                to='botend.simcagent')),
        migrations.AddField(model_name='simulationrun', name='completion_id',
                            field=models.CharField(blank=True, default='', max_length=64)),
        migrations.AddIndex(model_name='simulationrun',
                            index=models.Index(fields=['status', 'lease_expires_at'], name='simc_run_lease_q_idx')),
        migrations.RunPython(deduplicate_run_artifacts, migrations.RunPython.noop),
        migrations.AddConstraint(model_name='simctaskartifact',
            constraint=models.UniqueConstraint(fields=('task', 'run', 'artifact_type'),
                                               name='simc_artifact_task_run_type_uniq')),
    ]
