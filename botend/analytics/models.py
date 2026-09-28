from django.db import models


class SiteAnalyticsConfig(models.Model):
    enabled = models.BooleanField('启用采集', default=True)
    excluded_prefixes = models.JSONField('排除路径前缀', default=list, blank=True)
    trusted_proxy_cidrs = models.JSONField('可信代理网段', default=list, blank=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        verbose_name = '站内统计设置'


class SitePageView(models.Model):
    event_id = models.UUIDField('上报编号', unique=True)
    occurred_at = models.DateTimeField('访问时间', db_index=True)
    path = models.CharField('页面路径', max_length=512)
    area = models.CharField('站点区域', max_length=24)
    visitor_key = models.CharField('访客摘要', max_length=64)
    session_key = models.CharField('会话摘要', max_length=64)
    ip_key = models.CharField('IP 摘要', max_length=64, blank=True)
    referrer_host = models.CharField('来源域名', max_length=253, blank=True)
    device = models.CharField('设备', max_length=24)
    browser = models.CharField('浏览器', max_length=24)
    os = models.CharField('操作系统', max_length=24)
    authenticated = models.BooleanField('已登录', default=False)

    class Meta:
        verbose_name = '页面访问记录'
        indexes = [
            models.Index(fields=['area', 'occurred_at'], name='site_pv_area_time'),
            models.Index(fields=['visitor_key', 'occurred_at'], name='site_pv_visitor_time'),
            models.Index(fields=['ip_key', 'occurred_at'], name='site_pv_ip_time'),
        ]
