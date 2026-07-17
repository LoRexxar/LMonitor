from django.db import migrations, models


def resequence_duplicate_runs(apps, schema_editor):
    """Normalize historical runs before enforcing per-task uniqueness."""
    SimulationRun = apps.get_model('botend', 'SimulationRun')
    task_ids = (
        SimulationRun.objects.order_by()
        .values_list('task_id', flat=True)
        .distinct()
    )
    for task_id in task_ids.iterator():
        run_ids = list(
            SimulationRun.objects.filter(task_id=task_id)
            .order_by('sequence', 'created_at', 'id')
            .values_list('id', flat=True)
        )
        # Use temporary negative values so updates cannot collide if a partial
        # unique index already exists in an unusual deployment state.
        for position, run_id in enumerate(run_ids, start=1):
            SimulationRun.objects.filter(id=run_id).update(sequence=-position)
        for position, run_id in enumerate(run_ids, start=1):
            SimulationRun.objects.filter(id=run_id).update(sequence=position)


class Migration(migrations.Migration):
    dependencies = [
        ('botend', '0110_add_task_references_and_simulation_run'),
    ]

    operations = [
        migrations.RunPython(resequence_duplicate_runs, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='simulationrun',
            constraint=models.UniqueConstraint(
                fields=('task', 'sequence'),
                name='simc_run_task_sequence_uniq',
            ),
        ),
    ]
