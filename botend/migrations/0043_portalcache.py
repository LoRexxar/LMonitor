from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0042_simcbackendbinary_progress_fields'),
    ]

    operations = [
        migrations.CreateModel(
            name='PortalCache',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(help_text='缓存键，如 exwind_latest/nga_hot/blueposts', max_length=100, unique=True)),
                ('data', models.TextField(blank=True, default='', help_text='JSON字符串')),
                ('status', models.IntegerField(default=0, help_text='状态 0正常 1失败')),
                ('error_message', models.CharField(blank=True, default='', help_text='最近错误摘要', max_length=1000)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Portal缓存',
                'verbose_name_plural': 'Portal缓存',
                'db_table': 'portal_cache',
            },
        ),
    ]

