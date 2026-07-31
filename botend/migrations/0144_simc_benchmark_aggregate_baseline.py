from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('botend', '0143_simc_agent_maintenance_tasks')]

    operations = [
        migrations.AddField(
            model_name='simcbenchmarkpanel',
            name='aggregate_baseline_execution',
            field=models.ForeignKey(
                blank=True,
                help_text='Full rerun boundary; older Results remain auditable but are excluded from current aggregation.',
                null=True,
                on_delete=models.deletion.SET_NULL,
                related_name='aggregate_baseline_for_panels',
                to='botend.simcbenchmarkexecution',
            ),
        ),
    ]
