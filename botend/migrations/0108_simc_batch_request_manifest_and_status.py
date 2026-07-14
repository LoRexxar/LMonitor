# Generated manually for batch request_manifest and status tracking
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0107_simc_add_user_isolation_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='simctaskbatch',
            name='request_manifest',
            field=models.TextField(blank=True, help_text='冻结批次输入（JSON）', null=True),
        ),
        migrations.AddField(
            model_name='simctaskbatch',
            name='status',
            field=models.IntegerField(default=0, help_text='0=待创建,1=运行中,2=完成,3=失败'),
        ),
        migrations.AddField(
            model_name='simctaskbatch',
            name='error_detail',
            field=models.TextField(blank=True, help_text='错误详情', null=True),
        ),
        migrations.AddField(
            model_name='simctaskbatch',
            name='completed_at',
            field=models.DateTimeField(blank=True, help_text='完成时间', null=True),
        ),
    ]
