from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0041_simcbackendbinary'),
    ]

    operations = [
        migrations.AddField(
            model_name='simcbackendbinary',
            name='is_updating',
            field=models.BooleanField(default=False, help_text='是否正在更新'),
        ),
        migrations.AddField(
            model_name='simcbackendbinary',
            name='last_error',
            field=models.CharField(blank=True, default='', help_text='最近更新错误', max_length=500),
        ),
        migrations.AddField(
            model_name='simcbackendbinary',
            name='latest_version',
            field=models.CharField(default='', help_text='检测到的最新版本号', max_length=128),
        ),
        migrations.AddField(
            model_name='simcbackendbinary',
            name='update_progress',
            field=models.IntegerField(default=0, help_text='更新进度百分比 0-100'),
        ),
        migrations.AddField(
            model_name='simcbackendbinary',
            name='update_status',
            field=models.CharField(blank=True, default='', help_text='更新状态提示', max_length=255),
        ),
    ]

