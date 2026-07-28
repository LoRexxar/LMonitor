from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('botend', '0131_simctask_queue_indexes')]

    operations = [
        migrations.AddField(
            model_name='simcbenchmarkexecution', name='status',
            field=models.CharField(
                choices=[('pending', 'Pending'), ('running', 'Running'),
                         ('success', 'Success'), ('partial', 'Partial'),
                         ('failed', 'Failed'), ('cancelled', 'Cancelled')],
                default='pending', max_length=16,
            ),
        ),
        migrations.AddField(
            model_name='simcbenchmarkexecution', name='result_hash',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='simcbenchmarkexecution', name='results_finalized_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='simcbenchmarkcase', name='status',
            field=models.CharField(
                choices=[('pending', 'Pending'), ('running', 'Running'),
                         ('success', 'Success'), ('partial', 'Partial'),
                         ('failed', 'Failed'), ('cancelled', 'Cancelled')],
                default='pending', max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name='simcbenchmarkcase', name='task',
            field=models.OneToOneField(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='benchmark_case', to='botend.simctask',
            ),
        ),
        migrations.AddField(
            model_name='simcbenchmarkpanel', name='active_execution',
            field=models.OneToOneField(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='active_for_panel', to='botend.simcbenchmarkexecution',
            ),
        ),
        migrations.CreateModel(
            name='SimcBenchmarkResult',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True,
                                        serialize=False, verbose_name='ID')),
                ('candidate_key', models.CharField(max_length=100)),
                ('dps', models.FloatField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('case', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                           related_name='results',
                                           to='botend.simcbenchmarkcase')),
            ],
            options={
                'db_table': 'simc_benchmark_result',
                'ordering': ['case_id', 'id'],
                'constraints': [
                    models.UniqueConstraint(fields=('case', 'candidate_key'),
                                            name='simc_bench_result_cand_uniq'),
                    models.CheckConstraint(condition=models.Q(dps__gt=0),
                                           name='simc_bench_result_dps_gt0_ck'),
                ],
            },
        ),
    ]