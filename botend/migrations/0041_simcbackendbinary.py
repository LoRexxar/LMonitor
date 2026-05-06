from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0040_simcsecondarystatrule'),
    ]

    operations = [
        migrations.CreateModel(
            name='SimcBackendBinary',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('platform', models.CharField(default='win64', help_text='平台标识，如 win64/linux64', max_length=32)),
                ('simc_path', models.CharField(default='', help_text='SimC可执行文件路径', max_length=500)),
                ('current_version', models.CharField(default='', help_text='当前SimC版本号/构建标识', max_length=128)),
                ('auto_update', models.BooleanField(default=True, help_text='是否自动更新')),
                ('last_checked_at', models.DateTimeField(blank=True, help_text='上次检查时间', null=True)),
                ('last_updated_at', models.DateTimeField(blank=True, help_text='上次更新时间', null=True)),
            ],
            options={
                'verbose_name': 'SimC后端软件',
                'verbose_name_plural': 'SimC后端软件',
                'db_table': 'simc_backend_binary',
            },
        ),
    ]

