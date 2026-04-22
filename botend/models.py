from django.db import models
from django.utils import timezone


class MonitorTask(models.Model):
    name = models.CharField(max_length=100)
    target = models.CharField(max_length=2000)
    type = models.IntegerField(default=0)
    last_scan_time = models.DateTimeField(auto_now=True)
    wait_time = models.IntegerField(default=600)
    flag = models.CharField(max_length=2000, null=True, default=None)
    is_active = models.BooleanField(default=True)


class TargetAuth(models.Model):
    domain = models.CharField(max_length=200)
    cookie = models.TextField(null=True)
    is_login = models.BooleanField(default=True)
    ext = models.CharField(max_length=100, null=True, default=None)


class MonitorWebhook(models.Model):
    task_id = models.IntegerField()
    task_name = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)


class WechatAccountTask(models.Model):
    biz = models.CharField(max_length=50)
    account = models.CharField(max_length=255, null=True)
    summary = models.CharField(max_length=500, null=True)
    last_publish_time = models.DateTimeField(auto_now_add=True, null=True)
    last_spider_time = models.DateTimeField(auto_now=True, null=True)
    is_zombie = models.IntegerField(default=0)


class WechatArticle(models.Model):
    account = models.CharField(max_length=255, null=True)
    title = models.CharField(max_length=255, default=None, null=True)
    url = models.CharField(max_length=2000, default=None, null=True)
    author = models.CharField(max_length=255, default=None, null=True)
    publish_time = models.DateTimeField(default=None, null=True)
    biz = models.CharField(max_length=50)
    digest = models.CharField(max_length=2000, default=None, null=True)
    cover = models.CharField(max_length=255, default=None, null=True)
    content_html = models.TextField(default=None, null=True)
    source_url = models.CharField(max_length=555, default=None, null=True)
    sn = models.CharField(max_length=50, default=None, null=True)
    state = models.IntegerField(default=0)


class VulnMonitorTask(models.Model):
    task_name = models.CharField(max_length=255)
    target = models.CharField(max_length=1000, null=True)
    last_spider_time = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)


class VulnData(models.Model):
    sid = models.CharField(max_length=200, null=True)
    cveid = models.CharField(max_length=200, null=True)
    title = models.CharField(max_length=500)
    type = models.CharField(max_length=100, null=True)
    score = models.CharField(max_length=10, default="0")
    severity = models.IntegerField(default=0)
    publish_time = models.DateTimeField()
    link = models.CharField(max_length=1000, null=True)
    description = models.TextField(null=True)
    solutions = models.TextField(null=True)
    source = models.CharField(max_length=1000, null=True)
    reference = models.CharField(max_length=1000, null=True)
    tag = models.CharField(max_length=200, null=True)
    is_poc = models.BooleanField(default=False)
    is_exp = models.BooleanField(default=False)
    is_verify = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    state = models.IntegerField(default=0)


class RssMonitorTask(models.Model):
    name = models.CharField(max_length=255)
    link = models.CharField(max_length=1000)
    tag = models.CharField(max_length=255, null=True)
    last_spider_time = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)


class RssArticle(models.Model):
    rss_id = models.IntegerField()
    title = models.CharField(max_length=255, default=None, null=True)
    url = models.CharField(max_length=2000, default=None, null=True)
    author = models.CharField(max_length=255, default=None, null=True)
    publish_time = models.DateTimeField(default=None, null=True)
    content_html = models.TextField(null=True)
    is_active = models.BooleanField(default=True)


class WowArticle(models.Model):
    title = models.CharField(max_length=255, default=None, null=True)
    url = models.CharField(max_length=2000, default=None, null=True)
    author = models.CharField(max_length=255, default=None, null=True)
    description = models.TextField(null=True)
    publish_time = models.DateTimeField(default=timezone.now, null=True)
    is_active = models.BooleanField(default=True)

class GeWechatAuth(models.Model):
    appId = models.CharField(max_length=100)
    qrImgBase64 = models.TextField(null=True)
    uuid = models.CharField(max_length=100, null=True)
    create_time = models.DateTimeField(auto_now_add=True)
    login_status = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

class GeWechatRoomList(models.Model):
    room_id = models.CharField(max_length=100)
    room_name = models.CharField(max_length=100, null=True)
    room_member_count = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

class GeWechatTask(models.Model):
    msg_type = models.IntegerField(default=1)
    content_regex = models.CharField(max_length=100, null=True)
    response = models.TextField(null=True)
    # 0: admin 1: all 2：self 3：room
    active_type = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)


class SimcAplKeywordPair(models.Model):
    """
    SimC APL关键字对照表
    """
    apl_keyword = models.CharField(max_length=100, help_text="APL格式关键字")
    cn_keyword = models.CharField(max_length=100, help_text="CN关键字")
    description = models.CharField(max_length=500, null=True, blank=True, help_text="描述")
    create_time = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True, help_text="是否启用")
    
    class Meta:
        db_table = 'simc_apl_keyword_pair'
        verbose_name = 'SimC APL关键字对'
        verbose_name_plural = 'SimC APL关键字对'
    
    def __str__(self):
        return f"{self.apl_keyword} <-> {self.cn_keyword}"


class UserAplStorage(models.Model):
    """
    用户APL代码存储表
    """
    user_id = models.IntegerField(help_text="用户ID")
    title = models.CharField(max_length=200, help_text="APL标题/标识")
    apl_code = models.TextField(help_text="APL代码内容")
    is_active = models.BooleanField(default=True, help_text="是否启用")
    
    class Meta:
        db_table = 'user_apl_storage'
        verbose_name = '用户APL存储'
        verbose_name_plural = '用户APL存储'
    


class SimcTask(models.Model):
    """
    SimC任务模型
    """
    user_id = models.IntegerField(help_text="用户ID")
    name = models.CharField(max_length=200, help_text="任务名称")
    simc_profile_id = models.IntegerField(help_text="用户ID")
    result_file = models.TextField(help_text="任务结果，多个文件以逗号分割", null=True)
    task_type = models.IntegerField(default=1, help_text="任务类型：1=常规模拟，2=属性模拟")
    ext = models.CharField(max_length=500, null=True, blank=True, help_text="扩展信息")
    modified_time = models.DateTimeField(auto_now=True, help_text="修改时间")
    current_status = models.IntegerField(default=0, help_text="当前状态")
    create_time = models.DateTimeField(auto_now_add=True, help_text="创建时间")
    is_active = models.BooleanField(default=True, help_text="是否启用")
    
    class Meta:
        db_table = 'simc_task'
        verbose_name = 'SimC任务'
        verbose_name_plural = 'SimC任务'
        ordering = ['-modified_time']
    
class SimcProfile(models.Model):
    """
    SimC配置模型
    """
    user_id = models.IntegerField(help_text="用户ID")
    name = models.CharField(max_length=200, help_text="配置名称")
    spec = models.CharField(max_length=100, default="fury", help_text="专精标识，如 fury/arms/fire")
    fight_style = models.CharField(max_length=200, default="Patchwerk")
    time = models.IntegerField(default="40")
    target_count = models.IntegerField(default=1)
    talent = models.CharField(max_length=2000, default="")
    action_list = models.TextField(default="", null=True)
    gear_strength = models.IntegerField(default=93330)
    gear_crit = models.IntegerField(default=10730)
    gear_haste = models.IntegerField(default=18641)
    gear_mastery = models.IntegerField(default=21785)
    gear_versatility = models.IntegerField(default=6757)
    is_active = models.BooleanField(default=True, help_text="是否启用")
    
    class Meta:
        db_table = 'simc_profile'
        verbose_name = 'SimC配置'
        verbose_name_plural = 'SimC配置'


class SimcTemplate(models.Model):
    """
    SimC模板模型
    """
    template_content = models.TextField(help_text="模板内容")
    spec = models.CharField(max_length=100, default="default", help_text="模板适配专精，如 fury/arms/default")
    is_active = models.BooleanField(default=True, help_text="是否启用")
    
    class Meta:
        db_table = 'simc_template'
        verbose_name = 'SimC模板'
        verbose_name_plural = 'SimC模板'
    
    def __str__(self):
        return f"SimC模板 (ID: {self.id})"


class WclAnalysisTask(models.Model):
    wcl_url = models.CharField(max_length=2000, help_text="WCL原始链接")
    report_code = models.CharField(max_length=128, help_text="WCL报告ID", null=True, blank=True)
    fight_id = models.CharField(max_length=64, help_text="Fight ID", null=True, blank=True)
    access_token = models.CharField(max_length=64, help_text="公开报告访问令牌")
    status = models.IntegerField(default=0, help_text="状态 0待处理 1处理中 2成功 3失败")
    error_message = models.CharField(max_length=1000, null=True, blank=True, help_text="错误信息")
    source_snapshot_file = models.CharField(max_length=255, null=True, blank=True, help_text="源数据快照文件")
    report_html_file = models.CharField(max_length=255, null=True, blank=True, help_text="最终报告HTML文件")
    summary = models.CharField(max_length=1000, null=True, blank=True, help_text="摘要")
    benchmark_unavailable = models.BooleanField(default=False, help_text="排行榜基准是否不可用")
    is_active = models.BooleanField(default=True, help_text="是否启用")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wcl_analysis_task'
        verbose_name = 'WCL分析任务'
        verbose_name_plural = 'WCL分析任务'
        ordering = ['-created_at']
    
