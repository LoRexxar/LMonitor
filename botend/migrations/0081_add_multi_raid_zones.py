from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0080_monitortask_last_scan_time_default'),
    ]

    operations = [
        migrations.AddField(
            model_name='seasonmeta',
            name='raid_zones',
            field=models.JSONField(blank=True, default=list, verbose_name='团本区域列表'),
        ),
        migrations.AddField(
            model_name='specraidranking',
            name='raid_zone_id',
            field=models.IntegerField(blank=True, null=True, verbose_name='团本区域ID'),
        ),
        migrations.AddField(
            model_name='specraidranking',
            name='raid_zone_name',
            field=models.CharField(blank=True, default='', max_length=100, verbose_name='团本区域名称'),
        ),
    ]
