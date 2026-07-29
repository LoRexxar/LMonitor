from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('botend', '0139_simc_agent_enrollment_codes')]

    operations = [
        migrations.AddField(
            model_name='simcagent',
            name='agent_revision',
            field=models.CharField(blank=True, default='', help_text='运行中的 LMonitor Agent Git commit', max_length=64),
        ),
    ]
