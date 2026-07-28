from django.db import migrations, models


def backfill_execution_owner(apps, schema_editor):
    Task = apps.get_model('botend', 'SimcTask')
    # Existing in-flight rows already have observable ownership. Pending rows
    # intentionally remain unassigned so either execution plane can claim them.
    Task.objects.filter(
        current_status=1, simulation_runs__lease_agent_id__isnull=False,
    ).update(execution_owner='agent')
    Task.objects.filter(current_status=1, execution_owner='').update(execution_owner='local')


class Migration(migrations.Migration):
    dependencies = [('botend', '0137_simulationrun_agent_lease')]

    operations = [
        migrations.AddField(
            model_name='simctask', name='execution_owner',
            field=models.CharField(
                blank=True,
                choices=[('', '未分配'), ('local', '本地 Worker'), ('agent', '独立 Agent')],
                default='', max_length=8,
                help_text='首次领取时原子确定的执行面；确定后不可跨执行面领取',
            ),
        ),
        migrations.RunPython(backfill_execution_owner, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name='simctask',
            index=models.Index(
                fields=['execution_owner', 'is_active', 'current_status', 'create_time'],
                name='simctask_owner_queue_idx',
            ),
        ),
        migrations.AddConstraint(
            model_name='simctask',
            constraint=models.CheckConstraint(
                condition=models.Q(execution_owner__in=('', 'local', 'agent')),
                name='simctask_execution_owner_ck',
            ),
        ),
    ]
