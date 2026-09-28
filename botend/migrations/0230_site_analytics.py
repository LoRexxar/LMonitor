# 站内统计：采集设置与页面访问明细。

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0229_bilibili_account_binding'),
    ]

    operations = [
        migrations.CreateModel(
            name='SiteAnalyticsConfig',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('enabled', models.BooleanField(default=True, verbose_name='启用采集')),
                ('excluded_prefixes', models.JSONField(blank=True, default=list, verbose_name='排除路径前缀')),
                ('trusted_proxy_cidrs', models.JSONField(blank=True, default=list, verbose_name='可信代理网段')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='更新时间')),
            ],
            options={
                'verbose_name': '站内统计设置',
            },
        ),
        migrations.CreateModel(
            name='SitePageView',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('event_id', models.UUIDField(unique=True, verbose_name='上报编号')),
                ('occurred_at', models.DateTimeField(db_index=True, verbose_name='访问时间')),
                ('path', models.CharField(max_length=512, verbose_name='页面路径')),
                ('area', models.CharField(max_length=24, verbose_name='站点区域')),
                ('visitor_key', models.CharField(max_length=64, verbose_name='访客摘要')),
                ('session_key', models.CharField(max_length=64, verbose_name='会话摘要')),
                ('ip_key', models.CharField(blank=True, max_length=64, verbose_name='IP 摘要')),
                ('referrer_host', models.CharField(blank=True, max_length=253, verbose_name='来源域名')),
                ('device', models.CharField(max_length=24, verbose_name='设备')),
                ('browser', models.CharField(max_length=24, verbose_name='浏览器')),
                ('os', models.CharField(max_length=24, verbose_name='操作系统')),
                ('authenticated', models.BooleanField(default=False, verbose_name='已登录')),
            ],
            options={
                'verbose_name': '页面访问记录',
                'indexes': [models.Index(fields=['area', 'occurred_at'], name='site_pv_area_time'), models.Index(fields=['visitor_key', 'occurred_at'], name='site_pv_visitor_time'), models.Index(fields=['ip_key', 'occurred_at'], name='site_pv_ip_time')],
            },
        ),
    ]
