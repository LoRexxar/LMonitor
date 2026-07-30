from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('botend', '0142_simcbackend_daily_maintenance')]

    operations = [
        migrations.CreateModel(
            name='SimcAgentMaintenanceTask',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('running', 'Running'), ('success', 'Success'), ('failed', 'Failed'), ('cancelled', 'Cancelled')], default='pending', max_length=16)),
                ('requested_at', models.DateTimeField(auto_now_add=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('error', models.CharField(blank=True, default='', max_length=500)),
                ('agent', models.ForeignKey(on_delete=models.deletion.PROTECT, related_name='maintenance_tasks', to='botend.simcagent')),
            ],
            options={'db_table': 'simc_agent_maintenance_task', 'ordering': ['-requested_at', '-id']},
        ),
        migrations.AddIndex(model_name='simcagentmaintenancetask', index=models.Index(fields=['agent', 'status', 'id'], name='simc_agmaint_agent_state_idx')),
        migrations.AddConstraint(model_name='simcagentmaintenancetask', constraint=models.CheckConstraint(condition=models.Q(('status__in', ('pending', 'running', 'success', 'failed', 'cancelled'))), name='simc_agmaint_status_ck')),
    ]
