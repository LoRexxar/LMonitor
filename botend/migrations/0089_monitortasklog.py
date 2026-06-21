# Generated manually for monitor task execution logs

from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0088_wowarticle_content_blocks'),
    ]

    operations = [
        migrations.CreateModel(
            name='MonitorTaskLog',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('task_name', models.CharField(max_length=100)),
                ('task_type', models.IntegerField(default=0)),
                ('target', models.CharField(blank=True, default='', max_length=2000)),
                ('status', models.CharField(max_length=20)),
                ('started_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
                ('duration_ms', models.IntegerField(blank=True, null=True)),
                ('error_type', models.CharField(blank=True, default='', max_length=200)),
                ('error_message', models.TextField(blank=True, default='')),
                ('traceback', models.TextField(blank=True, default='')),
                ('task_flag', models.CharField(blank=True, default=None, max_length=2000, null=True)),
                ('extra', models.JSONField(blank=True, default=dict)),
                ('task', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='logs', to='botend.monitortask')),
            ],
            options={
                'indexes': [
                    models.Index(fields=['task_name', 'status', 'started_at'], name='botend_moni_task_na_0a5c45_idx'),
                    models.Index(fields=['status', 'started_at'], name='botend_moni_status_4e5eb6_idx'),
                    models.Index(fields=['task', 'started_at'], name='botend_moni_task_id_7cbff8_idx'),
                ],
            },
        ),
    ]
