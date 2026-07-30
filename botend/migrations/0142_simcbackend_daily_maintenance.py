from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0141_simc_profile_optional_attribute_overrides'),
    ]

    operations = [
        migrations.AddField(
            model_name='simcbackendbinary',
            name='maintenance_enabled',
            field=models.BooleanField(default=True, help_text='是否启用每日SimC维护窗口'),
        ),
        migrations.AddField(
            model_name='simcbackendbinary',
            name='maintenance_daily_time',
            field=models.CharField(default='03:00', help_text='每日维护开始时间（Asia/Shanghai，HH:MM）', max_length=5),
        ),
        migrations.AddField(
            model_name='simcbackendbinary',
            name='maintenance_window_minutes',
            field=models.PositiveIntegerField(default=60, help_text='每日维护窗口分钟数'),
        ),
        migrations.AddField(
            model_name='simcbackendbinary',
            name='maintenance_policy_revision',
            field=models.PositiveIntegerField(default=1, help_text='Agent维护策略版本'),
        ),
    ]
