from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0130_simc_benchmark_panels'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='simctask',
            index=models.Index(
                fields=['is_active', 'current_status', 'create_time', 'id'],
                name='simctask_pending_q_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='simctask',
            index=models.Index(
                fields=['is_active', 'current_status', 'modified_time'],
                name='simctask_stale_q_idx',
            ),
        ),
    ]