from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('botend', '0118_simc_apl_symbols_and_validation')]
    operations = [
        migrations.AddField(model_name='simcapl', name='validation_status', field=models.CharField(default='draft', max_length=16, choices=[('draft', '草稿'), ('valid', '有效'), ('invalid', '无效'), ('stale', '已过期')])),
        migrations.AddField(model_name='simcapl', name='validated_content_hash', field=models.CharField(blank=True, default='', max_length=64)),
        migrations.AddField(model_name='simcapl', name='validation_revision', field=models.CharField(blank=True, default='', max_length=128)),
        migrations.AddField(model_name='simcapl', name='validation_game_build', field=models.CharField(blank=True, default='', max_length=64)),
        migrations.AddField(model_name='simcapl', name='validation_stale_reason', field=models.CharField(blank=True, default='', max_length=64)),
        migrations.AddField(model_name='simcapl', name='validation_diagnostics', field=models.JSONField(blank=True, default=list)),
        migrations.AddField(model_name='simcapl', name='validated_at', field=models.DateTimeField(blank=True, null=True)),
        migrations.AlterField(model_name='simcapl', name='is_selectable', field=models.BooleanField(default=False, help_text='任务发起时是否可选择')),

    ]
