import hashlib
import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import connection, models, transaction
from django.utils import timezone


class DashboardUserGroup(models.Model):
    """独立于 Django auth 权限体系的 Dashboard 业务用户组。"""

    name = models.CharField(max_length=150, unique=True)
    description = models.CharField(max_length=500, blank=True, default='')
    is_active = models.BooleanField(default=True)
    permission_codes = models.JSONField(default=list, blank=True)
    users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through='DashboardUserGroupMembership',
        related_name='dashboard_user_groups',
    )

    class Meta:
        ordering = ('name', 'pk')

    def __str__(self):
        return str(self.name)


class DashboardUserGroupMembership(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='dashboard_user_group_memberships',
    )
    group = models.ForeignKey(
        DashboardUserGroup,
        on_delete=models.CASCADE,
        related_name='memberships',
    )

    class Meta:
        ordering = ('user_id',)
        constraints = [
            models.UniqueConstraint(
                fields=('user', 'group'),
                name='unique_dashboard_user_group_membership',
            ),
        ]


class MonitorTask(models.Model):
    name = models.CharField(max_length=100)
    target = models.CharField(max_length=2000)
    type = models.IntegerField(default=0)
    env_limit = models.IntegerField(default=0)
    last_scan_time = models.DateTimeField(default=timezone.now)
    wait_time = models.IntegerField(default=600)
    flag = models.CharField(max_length=2000, null=True, default=None)
    is_active = models.BooleanField(default=True)
    proxy_enabled = models.BooleanField(default=False)


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
    title = models.CharField(max_length=500, default=None, null=True)
    url = models.CharField(max_length=2000, default=None, null=True)
    author = models.CharField(max_length=255, default=None, null=True)
    publish_time = models.DateTimeField(default=None, null=True)
    content_html = models.TextField(null=True)
    is_active = models.BooleanField(default=True)


class WowArticle(models.Model):
    title = models.CharField(max_length=255, default=None, null=True)
    title_cn = models.CharField(max_length=255, default=None, null=True, blank=True)
    url = models.CharField(max_length=2000, default=None, null=True)
    url_hash = models.CharField(max_length=64, null=True, blank=True, unique=True)
    author = models.CharField(max_length=255, default=None, null=True)
    description = models.TextField(null=True)
    content = models.TextField(null=True, blank=True)
    content_cn = models.TextField(null=True, blank=True)
    content_blocks = models.TextField(null=True, blank=True)
    content_blocks_cn = models.TextField(null=True, blank=True)
    publish_time = models.DateTimeField(default=timezone.now, null=True)
    reply_count = models.IntegerField(default=0)
    source = models.CharField(max_length=32, default="unknown")
    category = models.CharField(max_length=32, default="unknown")
    is_active = models.BooleanField(default=True)

    class Meta:
        indexes = [
            models.Index(fields=['url_hash']),
            models.Index(fields=['source']),
            models.Index(fields=['category']),
            models.Index(fields=['publish_time']),
        ]

    def save(self, *args, **kwargs):
        if not self.url_hash and self.url:
            self.url_hash = hashlib.sha256(str(self.url).encode('utf-8')).hexdigest()
        super().save(*args, **kwargs)

class PortalEvent(models.Model):
    title = models.CharField(max_length=500)
    url = models.CharField(max_length=2000)
    url_hash = models.CharField(max_length=64, unique=True)
    source = models.CharField(max_length=32, default="unknown")
    tag = models.CharField(max_length=64, default="")
    start_at = models.DateTimeField(null=True, blank=True)
    end_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=32, null=True, blank=True)
    summary = models.TextField(default="", blank=True)
    image_url = models.CharField(max_length=2000, default="", blank=True)
    external_id = models.CharField(max_length=128, default="", blank=True)
    raw_data = models.JSONField(default=dict, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'wow_portal_event'
        indexes = [
            models.Index(fields=['url_hash']),
            models.Index(fields=['source', 'is_active']),
            models.Index(fields=['start_at']),
            models.Index(fields=['last_seen_at']),
        ]

    def save(self, *args, **kwargs):
        if not self.url_hash and self.url:
            self.url_hash = hashlib.sha256(str(self.url).encode('utf-8')).hexdigest()
        super().save(*args, **kwargs)


class WowSkillDiffReport(models.Model):
    id = models.BigAutoField(primary_key=True)
    branch = models.CharField(max_length=32, default="wow")
    locale = models.CharField(max_length=8, default="enUS")
    from_build = models.CharField(max_length=64)
    to_build = models.CharField(max_length=64)
    display_from_build = models.CharField(max_length=64, default="", blank=True)
    display_to_build = models.CharField(max_length=64, default="", blank=True)
    content_md = models.TextField(default="", blank=True)
    content_html_path = models.CharField(max_length=500, default="", blank=True)
    changed_tables_json = models.TextField(default="", blank=True)
    spell_count = models.IntegerField(default=0)
    class_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wow_skill_diff_report'
        unique_together = (('branch', 'locale', 'to_build'),)
        indexes = [
            models.Index(fields=['branch', 'locale'], name='wow_skill__branch__b59a5d_idx'),
            models.Index(fields=['to_build'], name='wow_skill__to_bui_1b98a9_idx'),
            models.Index(fields=['created_at'], name='wow_skill__created_0f2f07_idx'),
        ]


class WowHotfixReport(models.Model):
    """
    Wago Hotfix 全量更新报告（面向 Dashboard 列表展示，Portal 暂不接入）。
    """
    id = models.BigAutoField(primary_key=True)
    branch = models.CharField(max_length=32, default="wow")
    locale = models.CharField(max_length=8, default="enUS")

    # 当前 build（Wago hotfix 列表返回的是 build number，例如 68016）
    build_num = models.CharField(max_length=32, default="", blank=True)
    build_str = models.CharField(max_length=64, default="", blank=True)

    from_push = models.BigIntegerField(default=0)
    to_push = models.BigIntegerField(default=0)

    summary_title = models.CharField(max_length=255, default="", blank=True)
    content_md = models.TextField(default="", blank=True)
    content_html_path = models.CharField(max_length=500, default="", blank=True)

    report_url = models.CharField(max_length=500, default="", blank=True)
    wago_url = models.CharField(max_length=500, default="", blank=True)

    changed_tables_json = models.TextField(default="", blank=True)
    table_count = models.IntegerField(default=0)
    entry_count = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wow_hotfix_report'
        unique_together = (('branch', 'locale', 'to_push'),)
        indexes = [
            models.Index(fields=['branch', 'locale'], name='wow_hot__branch__8ad3c7_idx'),
            models.Index(fields=['to_push'], name='wow_hot__to_pus_9a4f12_idx'),
            models.Index(fields=['created_at'], name='wow_hot__created_7c3a19_idx'),
        ]


class WowDailyReport(models.Model):
    report_date = models.DateField(unique=True)
    md_path = models.CharField(max_length=500, default="")
    ext_json = models.TextField(default="", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wow_daily_report'
        indexes = [
            models.Index(fields=['report_date']),
            models.Index(fields=['updated_at']),
        ]


class WowWagoMonitorState(models.Model):
    id = models.BigAutoField(primary_key=True)
    branch = models.CharField(max_length=32, default="wow")
    locale = models.CharField(max_length=8, default="enUS")
    is_active = models.BooleanField(default=True)
    build = models.CharField(max_length=64, default="", blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_run_status = models.CharField(max_length=32, default="", blank=True)
    last_event_at = models.DateTimeField(null=True, blank=True)
    last_event_status = models.CharField(max_length=64, default="", blank=True)
    report_url = models.CharField(max_length=500, default="", blank=True)
    wago_diff_url = models.CharField(max_length=500, default="", blank=True)
    ext = models.TextField(default="", blank=True)
    hotfix_push_id = models.BigIntegerField(default=0)
    hotfix_last_run_at = models.DateTimeField(null=True, blank=True)
    hotfix_last_run_status = models.CharField(max_length=32, default="", blank=True)
    hotfix_last_event_at = models.DateTimeField(null=True, blank=True)
    hotfix_last_event_status = models.CharField(max_length=64, default="", blank=True)
    hotfix_report_url = models.CharField(max_length=500, default="", blank=True)
    hotfix_wago_url = models.CharField(max_length=500, default="", blank=True)
    hotfix_spell_count = models.IntegerField(default=0)
    hotfix_class_count = models.IntegerField(default=0)
    hotfix_summary_title = models.CharField(max_length=255, default="", blank=True)
    hotfix_region_id = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wow_wago_monitor_state'
        unique_together = (('branch', 'locale'),)
        indexes = [
            models.Index(fields=['is_active']),
            models.Index(fields=['branch', 'locale']),
            models.Index(fields=['build']),
            models.Index(fields=['last_run_at']),
            models.Index(fields=['last_event_at']),
            models.Index(fields=['hotfix_push_id']),
            models.Index(fields=['hotfix_last_run_at']),
            models.Index(fields=['hotfix_last_event_at']),
            models.Index(fields=['hotfix_region_id']),
        ]


class WowWagoBuildEvent(models.Model):
    """Wago build diff interval event for traceable processing."""
    id = models.BigAutoField(primary_key=True)
    branch = models.CharField(max_length=32, default="wow")
    locale = models.CharField(max_length=8, default="enUS")
    from_build = models.CharField(max_length=64)
    to_build = models.CharField(max_length=64)
    status = models.CharField(max_length=64, default="detected")
    wago_diff_url = models.CharField(max_length=500, default="", blank=True)
    report = models.ForeignKey(WowSkillDiffReport, null=True, blank=True, on_delete=models.SET_NULL)
    spell_count = models.IntegerField(default=0)
    class_count = models.IntegerField(default=0)
    changed_tables_json = models.TextField(default="", blank=True)
    summary_title = models.CharField(max_length=255, default="", blank=True)
    error_message = models.TextField(default="", blank=True)
    ext = models.TextField(default="", blank=True)
    detected_at = models.DateTimeField(default=timezone.now)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wow_wago_build_event'
        unique_together = (('branch', 'locale', 'from_build', 'to_build'),)
        indexes = [
            models.Index(fields=['branch', 'locale']),
            models.Index(fields=['to_build']),
            models.Index(fields=['status']),
            models.Index(fields=['detected_at']),
            models.Index(fields=['updated_at']),
        ]


class WowWagoHotfixEvent(models.Model):
    """Wago hotfix push event for traceable processing."""
    id = models.BigAutoField(primary_key=True)
    branch = models.CharField(max_length=32, default="wow")
    locale = models.CharField(max_length=8, default="enUS")
    from_push = models.BigIntegerField(default=0)
    to_push = models.BigIntegerField(default=0)
    push_id = models.BigIntegerField(default=0)
    build_num = models.CharField(max_length=32, default="", blank=True)
    build_str = models.CharField(max_length=64, default="", blank=True)
    status = models.CharField(max_length=64, default="detected")
    wago_url = models.CharField(max_length=500, default="", blank=True)
    report = models.ForeignKey(WowHotfixReport, null=True, blank=True, on_delete=models.SET_NULL)
    table_count = models.IntegerField(default=0)
    entry_count = models.IntegerField(default=0)
    changed_tables_json = models.TextField(default="", blank=True)
    summary_title = models.CharField(max_length=255, default="", blank=True)
    error_message = models.TextField(default="", blank=True)
    ext = models.TextField(default="", blank=True)
    detected_at = models.DateTimeField(default=timezone.now)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wow_wago_hotfix_event'
        unique_together = (('branch', 'locale', 'to_push'),)
        indexes = [
            models.Index(fields=['branch', 'locale']),
            models.Index(fields=['to_push']),
            models.Index(fields=['push_id']),
            models.Index(fields=['status']),
            models.Index(fields=['detected_at']),
            models.Index(fields=['updated_at']),
        ]


class WowSpellSnapshotState(models.Model):
    branch = models.CharField(max_length=32, default="wow")
    locale = models.CharField(max_length=8, default="enUS")
    snapshot_build = models.CharField(max_length=64, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wow_spell_snapshot_state'
        unique_together = (('branch', 'locale'),)
        indexes = [
            models.Index(fields=['branch', 'locale']),
            models.Index(fields=['snapshot_build']),
            models.Index(fields=['updated_at']),
        ]


class WowSpellSnapshot(models.Model):
    branch = models.CharField(max_length=32, default="wow")
    locale = models.CharField(max_length=8, default="enUS")
    spell_id = models.BigIntegerField()
    name = models.CharField(max_length=255, default="", blank=True)
    name_zh = models.CharField(max_length=255, default="", blank=True)
    description = models.TextField(default="", blank=True)
    aura_description = models.TextField(default="", blank=True)
    snapshot_build = models.CharField(max_length=64, default="", blank=True)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'wow_spell_snapshot'
        unique_together = (('branch', 'locale', 'spell_id'),)
        indexes = [
            models.Index(fields=['branch', 'locale']),
            models.Index(fields=['spell_id']),
            models.Index(fields=['updated_at']),
        ]


class WowSpellEffectSnapshot(models.Model):
    branch = models.CharField(max_length=32, default="wow")
    locale = models.CharField(max_length=8, default="enUS")
    spell_id = models.BigIntegerField()
    effect_index = models.IntegerField(default=0)
    effect = models.IntegerField(null=True, blank=True)
    effect_aura = models.IntegerField(null=True, blank=True)
    base_points = models.CharField(max_length=64, default="", blank=True)
    coefficient = models.CharField(max_length=64, default="", blank=True)
    pvp_multiplier = models.CharField(max_length=64, default="", blank=True)
    snapshot_build = models.CharField(max_length=64, default="", blank=True)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'wow_spell_effect_snapshot'
        unique_together = (('branch', 'locale', 'spell_id', 'effect_index'),)
        indexes = [
            models.Index(fields=['branch', 'locale']),
            models.Index(fields=['spell_id']),
            models.Index(fields=['spell_id', 'effect_index']),
            models.Index(fields=['updated_at']),
        ]


class WowSpecSpellMapSnapshot(models.Model):
    branch = models.CharField(max_length=32, default="wow")
    locale = models.CharField(max_length=8, default="enUS")
    spec_id = models.IntegerField()
    spell_id = models.BigIntegerField()
    snapshot_build = models.CharField(max_length=64, default="", blank=True)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'wow_spec_spell_map_snapshot'
        unique_together = (('branch', 'locale', 'spec_id', 'spell_id'),)
        indexes = [
            models.Index(fields=['branch', 'locale']),
            models.Index(fields=['spec_id']),
            models.Index(fields=['spell_id']),
            models.Index(fields=['updated_at']),
        ]


class PortalToolLink(models.Model):
    name = models.CharField(max_length=200)
    url = models.CharField(max_length=2000)
    url_hash = models.CharField(max_length=64, unique=True)
    desc = models.CharField(max_length=500, null=True, blank=True)
    source = models.CharField(max_length=32, default="manual")
    sort_order = models.IntegerField(default=0)
    is_topbar = models.BooleanField(default=False)
    topbar_order = models.IntegerField(default=0)
    icon_path = models.CharField(max_length=500, null=True, blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'wow_portal_tool_link'
        indexes = [
            models.Index(fields=['url_hash']),
            models.Index(fields=['is_active']),
            models.Index(fields=['is_topbar']),
            models.Index(fields=['sort_order']),
        ]

    def save(self, *args, **kwargs):
        if not self.url_hash and self.url:
            self.url_hash = hashlib.sha256(str(self.url).encode('utf-8')).hexdigest()
        super().save(*args, **kwargs)


class PortalMplusRun(models.Model):
    rank = models.IntegerField(default=0)
    dungeon = models.CharField(max_length=128, default="")
    dungeon_slug = models.CharField(max_length=128, null=True, blank=True)
    level = models.IntegerField(default=0)
    time_seconds = models.IntegerField(default=0)
    score = models.FloatField(null=True, blank=True)
    run_url = models.CharField(max_length=2000, null=True, blank=True)
    party_json = models.TextField(null=True, blank=True)
    tank = models.CharField(max_length=128, null=True, blank=True)
    healer = models.CharField(max_length=128, null=True, blank=True)
    dps_json = models.TextField(null=True, blank=True)
    source = models.CharField(max_length=32, default="unknown")
    region = models.CharField(max_length=32, null=True, blank=True)
    season = models.CharField(max_length=64, null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'wow_portal_mplus_run'
        indexes = [
            models.Index(fields=['season', 'region']),
            models.Index(fields=['dungeon']),
            models.Index(fields=['dungeon_slug']),
        ]


class PortalMplusSeasonCutoff(models.Model):
    season = models.CharField(max_length=64, default="unknown")
    region = models.CharField(max_length=16, default="world")
    cutoff_0_1 = models.FloatField(null=True, blank=True)
    cutoff_1 = models.FloatField(null=True, blank=True)
    cutoff_0_1_prev = models.FloatField(null=True, blank=True)
    cutoff_1_prev = models.FloatField(null=True, blank=True)
    prev_updated_at = models.DateField(null=True, blank=True)
    source = models.CharField(max_length=32, default="raiderio")
    source_updated_at = models.CharField(max_length=128, default="", blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wow_portal_mplus_season_cutoff'
        unique_together = (('season', 'region'),)
        indexes = [
            models.Index(fields=['season', 'region']),
            models.Index(fields=['updated_at']),
        ]


class PortalPeakSpecRankRow(models.Model):
    season = models.CharField(max_length=64, default="unknown")
    region = models.CharField(max_length=32, default="world")

    class_slug = models.CharField(max_length=64, default="")
    class_name = models.CharField(max_length=128, default="")
    spec_slug = models.CharField(max_length=64, default="")
    spec_name = models.CharField(max_length=128, default="")
    spec_role = models.CharField(max_length=16, default="")

    rank = models.IntegerField(default=0)
    character_name = models.CharField(max_length=128, default="")
    character_path = models.CharField(max_length=500, default="", blank=True)
    score = models.FloatField(null=True, blank=True)
    score_color = models.CharField(max_length=16, default="", blank=True)

    rio_region_slug = models.CharField(max_length=16, default="", blank=True)
    realm_slug = models.CharField(max_length=64, default="", blank=True)
    realm_name = models.CharField(max_length=128, default="", blank=True)

    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'wow_portal_peak_spec_rank_row'
        unique_together = (('season', 'region', 'class_slug', 'spec_slug', 'rank'),)
        indexes = [
            models.Index(fields=['season', 'region']),
            models.Index(fields=['spec_role']),
            models.Index(fields=['class_slug', 'spec_slug']),
            models.Index(fields=['updated_at']),
        ]


class PortalMythicstatsDpsRow(models.Model):
    season = models.CharField(max_length=64, default="unknown")
    period_id = models.IntegerField()
    period_label = models.CharField(max_length=64, default="")
    week = models.IntegerField(null=True, blank=True)
    dungeon_id = models.IntegerField(default=0)
    dungeon_name = models.CharField(max_length=128, default="")
    role = models.CharField(max_length=16, default="damage")
    rank = models.IntegerField(default=0)
    diff_raw = models.CharField(max_length=16, default="", blank=True)
    diff_value = models.IntegerField(null=True, blank=True)
    tier = models.CharField(max_length=4, default="", blank=True)
    avg_text = models.CharField(max_length=32, default="", blank=True)
    avg_value = models.FloatField(null=True, blank=True)
    top_text = models.CharField(max_length=32, default="", blank=True)
    top_value = models.FloatField(null=True, blank=True)
    runs_text = models.CharField(max_length=32, default="", blank=True)
    runs_value = models.IntegerField(null=True, blank=True)
    spec_name = models.CharField(max_length=128, default="")
    spec_slug = models.CharField(max_length=128, default="")
    spec_url = models.CharField(max_length=2000, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wow_portal_mythicstats_dps_row'
        unique_together = (('season', 'period_id', 'dungeon_id', 'role', 'spec_slug'),)
        indexes = [
            models.Index(fields=['season', 'period_id']),
            models.Index(fields=['season', 'period_id', 'dungeon_id', 'role']),
            models.Index(fields=['spec_slug']),
            models.Index(fields=['updated_at']),
        ]

class VideoMonitorTarget(models.Model):
    name = models.CharField(max_length=200)
    tag = models.CharField(max_length=64)
    platform = models.CharField(max_length=32, default="bilibili")
    target_url = models.CharField(max_length=2000)
    target_url_hash = models.CharField(max_length=64)
    last_seen_bvid = models.CharField(max_length=32, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    ext_json = models.TextField(null=True, blank=True)

    class Meta:
        db_table = 'wow_video_monitor_target'
        unique_together = (('platform', 'target_url_hash'),)
        indexes = [
            models.Index(fields=['tag']),
            models.Index(fields=['target_url_hash']),
        ]

    def save(self, *args, **kwargs):
        if not self.target_url_hash and self.target_url:
            self.target_url_hash = hashlib.sha256(str(self.target_url).encode('utf-8')).hexdigest()
        super().save(*args, **kwargs)

class PortalVideo(models.Model):
    title = models.CharField(max_length=500)
    url = models.CharField(max_length=2000)
    url_hash = models.CharField(max_length=64, unique=True)
    bvid = models.CharField(max_length=32, null=True, blank=True)
    cover_url = models.CharField(max_length=2000, null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    author_name = models.CharField(max_length=255, default="")
    author_url = models.CharField(max_length=2000, default="")
    tag = models.CharField(max_length=64, default="")
    target = models.ForeignKey(VideoMonitorTarget, on_delete=models.SET_NULL, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    extra_json = models.TextField(null=True, blank=True)

    class Meta:
        db_table = 'wow_portal_video'
        indexes = [
            models.Index(fields=['url_hash']),
            models.Index(fields=['tag']),
            models.Index(fields=['published_at']),
        ]

    def save(self, *args, **kwargs):
        if not self.url_hash and self.url:
            self.url_hash = hashlib.sha256(str(self.url).encode('utf-8')).hexdigest()
        super().save(*args, **kwargs)

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


class SimcResourceVersion(models.Model):
    """
    SimC 资源不可变版本快照 - 冻结 Profile/Template/APL 内容用于任务执行。

    每次创建任务时，根据 resource_type + resource_id + content_hash 生成或复用版本记录。
    版本行创建后禁止修改，确保历史任务可重现。
    """
    id = models.BigAutoField(primary_key=True)
    resource_type = models.CharField(max_length=20, help_text="资源类型: profile/template/apl/talent")
    resource_id = models.BigIntegerField(help_text="资源ID（对应 SimcProfile.id/SimcContentTemplate.id/SimcApl.id）")
    content_hash = models.CharField(max_length=64, help_text="内容SHA256，用于版本去重")
    payload = models.JSONField(help_text="冻结的资源内容和元数据")
    created_at = models.DateTimeField(auto_now_add=True, help_text="创建时间")

    class Meta:
        db_table = 'simc_resource_version'
        verbose_name = 'SimC资源版本'
        verbose_name_plural = 'SimC资源版本'
        unique_together = (('resource_type', 'resource_id', 'content_hash'),)
        indexes = [
            models.Index(fields=['resource_type', 'resource_id', '-created_at']),
            models.Index(fields=['content_hash']),
        ]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            try:
                existing = SimcResourceVersion.objects.get(pk=self.pk)
            except SimcResourceVersion.DoesNotExist:
                pass
            else:
                if (existing.resource_type != self.resource_type or
                    existing.resource_id != self.resource_id or
                    existing.content_hash != self.content_hash or
                    existing.payload != self.payload):
                    raise ValueError(
                        f"SimcResourceVersion {self.pk} is immutable and cannot be modified"
                    )
        super().save(*args, **kwargs)


class SimcAplSymbol(models.Model):
    """仅由 token 和类型唯一标识的无版本 SimC APL 字段。"""

    KIND_ACTION = 'action'
    KIND_PSEUDO_ACTION = 'pseudo_action'
    KIND_ACTION_OPTION = 'action_option'
    KIND_EXPRESSION = 'expression'
    KIND_OPTION = 'option'
    KIND_NAMESPACE = 'namespace'
    KIND_RESOURCE = 'resource'
    KIND_BUFF = 'buff'
    KIND_DEBUFF = 'debuff'
    KIND_DOT = 'dot'
    KIND_COOLDOWN = 'cooldown'
    KIND_TALENT = 'talent'
    KIND_HERO_TREE = 'hero_tree'
    SYMBOL_KIND_CHOICES = (
        (KIND_ACTION, 'Action'),
        (KIND_PSEUDO_ACTION, 'Pseudo action'),
        (KIND_ACTION_OPTION, 'Action option'),
        (KIND_EXPRESSION, 'Expression'),
        (KIND_OPTION, 'Option'),
        (KIND_NAMESPACE, 'Namespace'),
        (KIND_RESOURCE, 'Resource'),
        (KIND_BUFF, 'Buff'),
        (KIND_DEBUFF, 'Debuff'),
        (KIND_DOT, 'Damage over time'),
        (KIND_COOLDOWN, 'Cooldown'),
        (KIND_TALENT, 'Talent'),
        (KIND_HERO_TREE, 'Hero tree'),
    )

    SOURCE_SIMC_MANIFEST = 'simc_manifest'
    SOURCE_MANIFEST = SOURCE_SIMC_MANIFEST
    SOURCE_SYSTEM_APL = 'system_apl'
    SOURCE_WAGO = 'wago'
    SOURCE_MANUAL = 'manual'
    SOURCE_CHOICES = (
        (SOURCE_SIMC_MANIFEST, 'SimC manifest'),
        (SOURCE_SYSTEM_APL, 'System APL'),
        (SOURCE_WAGO, 'Wago'),
        (SOURCE_MANUAL, 'Verified manual fact'),
    )

    id = models.BigAutoField(primary_key=True)
    token = models.CharField(max_length=200)
    symbol_kind = models.CharField(
        max_length=32, choices=SYMBOL_KIND_CHOICES, default=KIND_ACTION,
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'simc_apl_symbol'
        verbose_name = 'SimC APL field'
        verbose_name_plural = 'SimC APL fields'
        ordering = ['symbol_kind', 'token', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['token', 'symbol_kind'], name='simc_symbol_token_kind_uniq',
            ),
        ]
        indexes = [
            models.Index(fields=['symbol_kind', 'token'], name='simc_sym_kind_token_idx'),
        ]

    def __str__(self):
        return f'{self.symbol_kind}:{self.token}'

    @classmethod
    def prepare(cls, instance):
        """统一普通写入与批量写入时的机器标识。"""
        instance.token = (instance.token or '').strip().lower()
        return instance

    def clean(self):
        super().clean()
        self.prepare(self)

    def save(self, *args, **kwargs):
        before_token = self.token
        self.prepare(self)
        if kwargs.get('update_fields') is not None and self.token != before_token:
            update_fields = set(kwargs['update_fields'])
            update_fields.add('token')
            kwargs['update_fields'] = update_fields
        return super().save(*args, **kwargs)

    @classmethod
    def sync_revision_catalog(cls, simc_revision, wow_build, facts):
        """同步当前目录，但不把版本作为字段或归属的数据库身份。

        ``simc_revision`` 和 ``wow_build`` 仅用于校验导出输入及追溯数据包，
        不持久化到字段主体或归属记录。
        """
        if not str(simc_revision or '').strip() or not str(wow_build or '').strip():
            raise ValueError('simc_revision and wow_build are required')
        fact_fields = (
            'class_name', 'spec', 'hero_tree', 'spell_id', 'trait_id', 'source',
            'identity_source', 'identity_reason', 'identity_candidates',
            'aliases', 'options',
        )
        prepared = {}
        for fact in facts:
            values = dict(fact)
            values.pop('is_active', None)
            symbol = cls.prepare(cls(
                token=values.pop('token', ''),
                symbol_kind=values.pop('symbol_kind', cls.KIND_ACTION),
            ))
            scope = SimcAplSymbolScope.prepare(SimcAplSymbolScope(**values))
            identity_key = (
                symbol.token, symbol.symbol_kind, scope.class_key,
                scope.spec_key, scope.hero_tree_key,
            )
            payload = {field: getattr(scope, field) for field in fact_fields}
            previous = prepared.get(identity_key)
            if previous is not None and previous != payload:
                raise ValueError(f'conflicting duplicate identity: {identity_key!r}')
            prepared[identity_key] = payload

        with transaction.atomic():
            symbol_keys = {(key[0], key[1]) for key in prepared}
            existing_symbols = {
                (row.token, row.symbol_kind): row
                for row in cls.objects.filter(
                    token__in={token for token, _kind in symbol_keys},
                    symbol_kind__in={kind for _token, kind in symbol_keys},
                )
                if (row.token, row.symbol_kind) in symbol_keys
            }
            missing_symbols = [
                cls(token=token, symbol_kind=kind, is_active=True)
                for token, kind in symbol_keys if (token, kind) not in existing_symbols
            ]
            if missing_symbols:
                cls.objects.bulk_create(missing_symbols, ignore_conflicts=True)
            symbols = {
                (row.token, row.symbol_kind): row
                for row in cls.objects.filter(
                    token__in={token for token, _kind in symbol_keys},
                    symbol_kind__in={kind for _token, kind in symbol_keys},
                )
                if (row.token, row.symbol_kind) in symbol_keys
            }
            rows = []
            for identity_key, payload in prepared.items():
                token, kind, class_key, spec_key, hero_tree_key = identity_key
                values = dict(payload)
                values.update(
                    symbol=symbols[(token, kind)], class_key=class_key,
                    spec_key=spec_key, hero_tree_key=hero_tree_key, is_active=True,
                )
                rows.append(SimcAplSymbolScope(**values))

            SimcAplSymbolScope.objects.filter(is_active=True).exclude(
                source=cls.SOURCE_MANUAL,
            ).update(is_active=False)
            if rows:
                bulk_kwargs = {
                    'update_conflicts': True,
                    'update_fields': (*fact_fields, 'is_active', 'updated_at'),
                    'batch_size': 1000,
                }
                if connection.features.supports_update_conflicts_with_target:
                    bulk_kwargs['unique_fields'] = (
                        'symbol', 'class_key', 'spec_key', 'hero_tree_key',
                    )
                SimcAplSymbolScope.objects.bulk_create(rows, **bulk_kwargs)
            active_ids = SimcAplSymbolScope.objects.filter(
                is_active=True,
            ).values_list('symbol_id', flat=True).distinct()
            cls.objects.update(is_active=False)
            cls.objects.filter(pk__in=active_ids).update(is_active=True)


class SimcAplSymbolScope(models.Model):
    """一个无版本字段的职业/专精归属及本地化元数据。"""

    id = models.BigAutoField(primary_key=True)
    symbol = models.ForeignKey(
        SimcAplSymbol, on_delete=models.CASCADE, related_name='scopes',
    )
    class_name = models.CharField(max_length=50, null=True, blank=True)
    spec = models.CharField(max_length=100, null=True, blank=True)
    hero_tree = models.CharField(max_length=100, null=True, blank=True)
    class_key = models.CharField(max_length=50, default='', editable=False)
    spec_key = models.CharField(max_length=100, default='', editable=False)
    hero_tree_key = models.CharField(max_length=100, default='', editable=False)
    spell_id = models.BigIntegerField(null=True, blank=True)
    trait_id = models.BigIntegerField(null=True, blank=True)
    source = models.CharField(
        max_length=32, choices=SimcAplSymbol.SOURCE_CHOICES,
        default=SimcAplSymbol.SOURCE_MANIFEST,
    )
    identity_source = models.CharField(max_length=64, default='', blank=True)
    identity_reason = models.CharField(max_length=128, default='', blank=True)
    identity_candidates = models.JSONField(default=list, blank=True)
    aliases = models.JSONField(default=list, blank=True)
    options = models.JSONField(default=dict, blank=True)
    name_en = models.CharField(
        max_length=255, default='', blank=True,
        help_text='当前归属下的 APL 英文名称；至少保留原始 token',
    )
    name_zh = models.CharField(
        max_length=255, default='', blank=True,
        help_text='当前归属下的 APL 简体中文名称',
    )
    localization_source = models.CharField(max_length=64, default='', blank=True)
    localization_status = models.CharField(max_length=32, default='', blank=True)
    metadata = models.JSONField(
        default=dict, blank=True,
        help_text='当前职业/专精下的表达式模板、Wowhead 与覆盖审计元数据',
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'simc_apl_symbol_scope'
        verbose_name = 'SimC APL field scope'
        verbose_name_plural = 'SimC APL field scopes'
        ordering = ['symbol__symbol_kind', 'symbol__token', 'class_key', 'spec_key', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['symbol', 'class_key', 'spec_key', 'hero_tree_key'],
                name='simc_scope_symbol_scope_uniq',
            ),
            models.CheckConstraint(
                condition=(models.Q(class_name__isnull=True, class_key='') |
                           (models.Q(class_name__isnull=False) &
                            ~models.Q(class_name='') &
                            models.Q(class_name=models.F('class_key')))),
                name='simc_scope_class_key_ck',
            ),
            models.CheckConstraint(
                condition=(models.Q(spec__isnull=True, spec_key='') |
                           (models.Q(spec__isnull=False) &
                            ~models.Q(spec='') &
                            models.Q(spec=models.F('spec_key')))),
                name='simc_scope_spec_key_ck',
            ),
            models.CheckConstraint(
                condition=(models.Q(hero_tree__isnull=True, hero_tree_key='') |
                           (models.Q(hero_tree__isnull=False) &
                            ~models.Q(hero_tree='') &
                            models.Q(hero_tree=models.F('hero_tree_key')))),
                name='simc_scope_hero_key_ck',
            ),
        ]
        indexes = [
            models.Index(
                fields=['class_name', 'spec', 'hero_tree', 'is_active'],
                name='simc_scope_visibility_idx',
            ),
            models.Index(fields=['spell_id'], name='simc_scope_spell_idx'),
            models.Index(fields=['trait_id'], name='simc_scope_trait_idx'),
        ]

    def __str__(self):
        scope = '/'.join(filter(None, (self.class_name, self.spec, self.hero_tree))) or 'global'
        return f'{self.symbol}:{scope}'

    @property
    def token(self):
        return self.symbol.token

    @property
    def symbol_kind(self):
        return self.symbol.symbol_kind

    @staticmethod
    def normalize_scope(value):
        value = value.strip().lower() if isinstance(value, str) else value
        value = value or None
        return value, value or ''

    @classmethod
    def prepare(cls, instance):
        for scope, key in (
            ('class_name', 'class_key'), ('spec', 'spec_key'),
            ('hero_tree', 'hero_tree_key'),
        ):
            value, canonical = cls.normalize_scope(getattr(instance, scope, None))
            setattr(instance, scope, value)
            setattr(instance, key, canonical)
        return instance

    def clean(self):
        super().clean()
        self.prepare(self)

    def save(self, *args, **kwargs):
        prepared_fields = (
            'class_name', 'class_key', 'spec', 'spec_key',
            'hero_tree', 'hero_tree_key',
        )
        before_prepare = {field: getattr(self, field) for field in prepared_fields}
        self.prepare(self)
        if kwargs.get('update_fields') is not None:
            update_fields = set(kwargs['update_fields'])
            update_fields.update(
                field for field in prepared_fields
                if getattr(self, field) != before_prepare[field]
            )
            kwargs['update_fields'] = update_fields
        return super().save(*args, **kwargs)


class SimcApl(models.Model):
    """
    SimC APL 统一存储：默认 APL（系统/个人维护）与个人 APL 共用一张表。
    通过 source/is_system/owner_user_id 区分。
    """
    id = models.BigAutoField(primary_key=True)
    SOURCE_SIMC_UPSTREAM = 'simc_upstream'
    SOURCE_SIMC_BUILTIN = 'simc_builtin'
    SOURCE_USER = 'user'
    SOURCE_CHOICES = (
        (SOURCE_SIMC_UPSTREAM, 'SimC源码同步'),
        (SOURCE_SIMC_BUILTIN, 'SimC内置APL'),
        (SOURCE_USER, '用户维护'),
    )
    VALIDATION_DRAFT = 'draft'
    VALIDATION_VALID = 'valid'
    VALIDATION_INVALID = 'invalid'
    VALIDATION_STALE = 'stale'
    VALIDATION_STATUS_CHOICES = (
        (VALIDATION_DRAFT, '草稿'), (VALIDATION_VALID, '有效'),
        (VALIDATION_INVALID, '无效'), (VALIDATION_STALE, '已过期'),
    )

    name = models.CharField(max_length=200, help_text="APL名称")
    spec = models.CharField(max_length=100, help_text="适用专精标识，如 warrior_fury")
    class_name = models.CharField(max_length=50, default='', blank=True, help_text="职业英文名，如 warrior")
    content = models.TextField(help_text="APL代码内容")
    source = models.CharField(max_length=32, choices=SOURCE_CHOICES, default=SOURCE_USER, help_text="内容来源")
    is_system = models.BooleanField(default=False, help_text="是否为系统默认APL（只读）")
    owner_user_id = models.BigIntegerField(null=True, blank=True, help_text="所属用户ID，NULL表示全局默认APL")
    is_active = models.BooleanField(default=True, help_text="是否启用")
    is_selectable = models.BooleanField(default=False, help_text="任务发起时是否可选择")
    sync_version = models.CharField(max_length=128, default='', blank=True, help_text="同步来源版本/提交")
    validation_status = models.CharField(max_length=16, choices=VALIDATION_STATUS_CHOICES, default=VALIDATION_DRAFT)
    validated_content_hash = models.CharField(max_length=64, default='', blank=True)
    validation_revision = models.CharField(max_length=128, default='', blank=True)
    validation_game_build = models.CharField(max_length=64, default='', blank=True)
    validation_stale_reason = models.CharField(max_length=64, default='', blank=True)
    validation_diagnostics = models.JSONField(default=list, blank=True)
    validated_at = models.DateTimeField(null=True, blank=True)
    active_unique_key = models.CharField(
        max_length=191, null=True, blank=True, unique=True,
        help_text="活跃 APL 唯一键；停用时为 NULL",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'simc_apl'
        verbose_name = 'SimC APL'
        verbose_name_plural = 'SimC APL'
        indexes = [
            models.Index(fields=['spec', 'is_active'], name='simc_apl_sp_ac_idx'),
            models.Index(fields=['owner_user_id', '-created_at'], name='simc_apl_ow_cr_idx'),
            models.Index(fields=['source', 'is_system'], name='simc_apl_so_sy_idx'),
        ]

    def _compute_active_unique_key(self):
        if not self.is_active:
            return None
        owner = 'global' if self.owner_user_id is None else str(self.owner_user_id)
        spec = str(self.spec or 'unknown').strip().lower()
        if self.is_system:
            return f'system:{owner}:{self.source}:{spec}'
        normalized_name = ' '.join(str(self.name or '').strip().lower().split())
        name_hash = hashlib.sha256(normalized_name.encode('utf-8')).hexdigest()
        return f'user:{owner}:{spec}:{name_hash}'

    def save(self, *args, **kwargs):
        current_hash = hashlib.sha256(str(self.content or '').encode('utf-8')).hexdigest()
        update_fields = kwargs.get('update_fields')
        stale_reason = ''
        if self.validation_status == self.VALIDATION_VALID and self.validated_content_hash != current_hash:
            stale_reason = 'content_changed'
        elif self.pk and self.validation_status == self.VALIDATION_VALID:
            previous_spec = type(self).objects.filter(pk=self.pk).values_list('spec', flat=True).first()
            if previous_spec is not None and previous_spec != self.spec:
                stale_reason = 'spec_changed'
        if stale_reason:
            self.validation_status = self.VALIDATION_STALE
            self.validation_stale_reason = stale_reason
            self.is_selectable = False
            if update_fields is not None:
                kwargs['update_fields'] = list(set(update_fields) | {
                    'validation_status', 'validation_stale_reason', 'is_selectable'})
        self.active_unique_key = self._compute_active_unique_key()
        update_fields = kwargs.get('update_fields')
        if update_fields is not None and 'active_unique_key' not in update_fields:
            kwargs['update_fields'] = list(update_fields) + ['active_unique_key']
        super().save(*args, **kwargs)

    def validation_staleness(self, identity):
        current_hash = hashlib.sha256(str(self.content or '').encode('utf-8')).hexdigest()
        if self.validation_status != self.VALIDATION_VALID:
            return self.validation_stale_reason or 'not_validated'
        if self.validated_content_hash != current_hash:
            return 'content_changed'
        if not identity:
            return 'revision_unavailable'
        if self.validation_revision != identity[0]:
            return 'revision_changed'
        if self.validation_game_build != identity[1]:
            return 'game_build_changed'
        return ''

    def has_current_validation(self, identity):
        return not self.validation_staleness(identity)

    def __str__(self):
        return f'{self.name} ({self.spec})'



class SimcTask(models.Model):
    """
    SimC任务模型 - Phase 2重构：引用型任务保存模块引用和参数，不保存最终正文
    """
    user_id = models.IntegerField(help_text="用户ID")
    name = models.CharField(max_length=200, help_text="任务名称")
    simc_profile_id = models.IntegerField(help_text="用户ID")
    result_file = models.TextField(help_text="任务结果，多个文件以逗号分割", null=True)
    task_type = models.IntegerField(default=1, help_text="任务类型：1=常规模拟，2=属性模拟")
    ext = models.TextField(null=True, blank=True, help_text="扩展信息（legacy兼容）")

    candidate_label = models.CharField(max_length=200, default='', blank=True, help_text="对比任务标签，如 crit+1000")
    # Regular simulations deliberately use the fixed highest priority. Benchmark
    # executions only compete below that ceiling, using their frozen panel value.
    QUEUE_PRIORITY_NORMAL = 100
    QUEUE_PRIORITY_BENCHMARK_LOW = 10
    QUEUE_PRIORITY_BENCHMARK_NORMAL = 20
    QUEUE_PRIORITY_BENCHMARK_HIGH = 30
    queue_priority = models.PositiveSmallIntegerField(
        default=QUEUE_PRIORITY_NORMAL,
        help_text="领取队列优先级；普通模拟固定为最高值，Benchmark 创建时冻结",
    )
    is_benchmark_task = models.BooleanField(
        default=False,
        help_text='仅 Benchmark Execution 创建的任务；用于持久化校验队列优先级边界。',
    )

    # Reference-based fields (live resource FKs)
    profile = models.ForeignKey('SimcProfile', null=True, blank=True, on_delete=models.SET_NULL, related_name='tasks', help_text="引用的玩家配置")
    template = models.ForeignKey('SimcContentTemplate', null=True, blank=True, on_delete=models.SET_NULL, related_name='tasks', help_text="引用的基础模板")
    apl = models.ForeignKey('SimcApl', null=True, blank=True, on_delete=models.SET_NULL, related_name='tasks', help_text="引用的APL")

    # NEW: Version FKs (immutable snapshots)
    profile_version = models.ForeignKey('SimcResourceVersion', null=True, blank=True, on_delete=models.PROTECT, related_name='profile_tasks', help_text="Profile版本快照")
    template_version = models.ForeignKey('SimcResourceVersion', null=True, blank=True, on_delete=models.PROTECT, related_name='template_tasks', help_text="Template版本快照")
    apl_version = models.ForeignKey('SimcResourceVersion', null=True, blank=True, on_delete=models.PROTECT, related_name='apl_tasks', help_text="APL版本快照")
    talent_string = models.ForeignKey('SimcTalentString', null=True, blank=True, on_delete=models.SET_NULL, related_name='tasks', help_text="选择的天赋字符串")
    talent_version = models.ForeignKey('SimcResourceVersion', null=True, blank=True, on_delete=models.PROTECT, related_name='talent_tasks', help_text="天赋字符串版本快照")

    mode = models.CharField(max_length=50, default='normal', blank=True, help_text="任务模式：normal/comparison/attribute_sweep")
    simulation_params = models.JSONField(null=True, blank=True, help_text="模拟参数：iterations, fight_style等")
    mode_params = models.JSONField(null=True, blank=True, help_text="模式参数：对比项、寻优范围等")
    source_task = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='reruns', help_text="重跑来源任务")
    backend = models.ForeignKey(
        'SimcBackendBinary', on_delete=models.PROTECT,
        related_name='tasks', help_text="本任务显式指定的 SimC 执行后端",
    )
    EXECUTION_OWNER_UNASSIGNED = ''
    EXECUTION_OWNER_LOCAL = 'local'
    EXECUTION_OWNER_AGENT = 'agent'
    EXECUTION_OWNER_CHOICES = (
        (EXECUTION_OWNER_UNASSIGNED, '未分配'),
        (EXECUTION_OWNER_LOCAL, '本地 Worker'),
        (EXECUTION_OWNER_AGENT, '独立 Agent'),
    )
    execution_owner = models.CharField(
        max_length=8, choices=EXECUTION_OWNER_CHOICES,
        default=EXECUTION_OWNER_UNASSIGNED, blank=True,
        help_text="首次领取时原子确定的执行面；确定后不可跨执行面领取",
    )

    error_detail = models.TextField(null=True, blank=True, help_text="创建或执行错误详情")
    result_summary = models.TextField(null=True, blank=True, help_text="结果摘要JSON：DPS/HPS等关键指标")
    analysis_result = models.JSONField(default=dict, blank=True, help_text="请求级分析结果")

    modified_time = models.DateTimeField(auto_now=True, help_text="修改时间")
    current_status = models.IntegerField(default=0, help_text="当前状态：0=待执行,1=执行中,2=完成,3=失败")
    create_time = models.DateTimeField(auto_now_add=True, help_text="创建时间")
    started_at = models.DateTimeField(null=True, blank=True, help_text="开始执行时间")
    completed_at = models.DateTimeField(null=True, blank=True, help_text="完成时间")
    is_active = models.BooleanField(default=True, help_text="是否启用")

    class Meta:
        db_table = 'simc_task'
        verbose_name = 'SimC任务'
        verbose_name_plural = 'SimC任务'
        ordering = ['-modified_time']
        indexes = [
            models.Index(fields=['user_id', '-create_time']),
            models.Index(
                fields=['is_active', 'current_status', 'create_time', 'id'],
                name='simctask_pending_q_idx',
            ),
            models.Index(
                fields=['is_active', 'current_status', 'modified_time'],
                name='simctask_stale_q_idx',
            ),
            models.Index(
                fields=['execution_owner', 'is_active', 'current_status', '-queue_priority', 'create_time'],
                name='simctask_owner_queue_idx',
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(is_benchmark_task=False, queue_priority=100)
                    | models.Q(
                        is_benchmark_task=True,
                        queue_priority__in=(10, 20, 30),
                    )
                ),
                name='simctask_queue_priority_ck',
            ),
            models.CheckConstraint(
                condition=models.Q(execution_owner__in=('', 'local', 'agent')),
                name='simctask_execution_owner_ck',
            ),
        ]


class SimcTaskFavorite(models.Model):
    """账号级 SimC 任务收藏关系；不改变任务本身及其归属。"""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='simc_task_favorites',
    )
    task = models.ForeignKey(
        SimcTask,
        on_delete=models.CASCADE,
        related_name='favorite_relations',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'simc_task_favorite'
        ordering = ('-created_at', '-id')
        constraints = [
            models.UniqueConstraint(
                fields=('user', 'task'),
                name='unique_simc_task_favorite',
            ),
        ]
        indexes = [
            models.Index(
                fields=('user', '-created_at'),
                name='simctaskfav_user_created_idx',
            ),
        ]


class SimcTaskArtifact(models.Model):
    """SimC任务产物 - 分离存储HTML报告、JSON统计等文件路径"""
    task = models.ForeignKey(SimcTask, on_delete=models.CASCADE, related_name='artifacts')
    run = models.ForeignKey('SimulationRun', null=True, blank=True, on_delete=models.PROTECT,
                            related_name='artifacts', help_text="生成该产物的具体执行轮次")
    artifact_type = models.CharField(max_length=50, help_text="产物类型：html_report/json_stats/log")
    file_path = models.CharField(max_length=500, help_text="相对static/的文件路径")
    file_size = models.BigIntegerField(default=0, help_text="文件大小（字节）")
    content_hash = models.CharField(max_length=64, default='', blank=True,
                                    help_text="完成时验证的产物SHA-256")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'simc_task_artifact'
        verbose_name = 'SimC任务产物'
        verbose_name_plural = 'SimC任务产物'
        indexes = [
            models.Index(fields=['task', 'artifact_type']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['task', 'run', 'artifact_type'],
                name='simc_artifact_task_run_type_uniq',
            ),
        ]


class SimulationRun(models.Model):
    """
    SimulationRun - 单次 SimC 进程执行记录
    每个 Task 可能有多个 Run（多候选/多轮），每个 Run 是一次真实执行
    """
    task = models.ForeignKey(SimcTask, on_delete=models.CASCADE, related_name='simulation_runs', help_text="所属任务")
    sequence = models.IntegerField(default=1, help_text="执行序号（轮次/候选编号）")
    candidate_key = models.CharField(max_length=200, default='', blank=True, help_text="候选稳定标识")
    candidate_label = models.CharField(max_length=200, default='', blank=True, help_text="候选标签，如 baseline/crit+1000/apl_variant_2")
    round_number = models.PositiveIntegerField(default=1, help_text="候选轮次")
    candidate_params = models.JSONField(default=dict, blank=True, help_text="候选参数快照")
    display_metadata = models.JSONField(default=dict, blank=True, help_text="冻结的候选展示元数据")

    status = models.CharField(max_length=20, default='pending', help_text="状态：pending/running/completed/failed")
    input_hash = models.CharField(max_length=64, default='', blank=True, help_text="本次输入的SHA256")
    resource_manifest = models.JSONField(null=True, blank=True, help_text="本次执行时解析的资源版本元数据")
    lease_token_hash = models.CharField(max_length=80, default='', blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    lease_heartbeat_at = models.DateTimeField(null=True, blank=True)
    lease_instance_id = models.CharField(max_length=128, default='', blank=True)
    lease_agent = models.ForeignKey(
        'SimcAgent', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='leased_runs',
    )
    completion_id = models.CharField(max_length=64, default='', blank=True)

    result_summary = models.JSONField(null=True, blank=True, help_text="结果摘要：DPS/HPS等关键指标")
    error_detail = models.TextField(null=True, blank=True, help_text="执行错误详情")

    started_at = models.DateTimeField(null=True, blank=True, help_text="开始时间")
    completed_at = models.DateTimeField(null=True, blank=True, help_text="完成时间")
    created_at = models.DateTimeField(auto_now_add=True, help_text="创建时间")

    class Meta:
        db_table = 'simc_simulation_run'
        verbose_name = 'SimC执行记录'
        verbose_name_plural = 'SimC执行记录'
        ordering = ['task', 'sequence']
        indexes = [
            models.Index(fields=['task', 'sequence']),
            models.Index(fields=['status']),
            models.Index(fields=['input_hash']),
            models.Index(fields=['status', 'lease_expires_at'], name='simc_run_lease_q_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['task', 'sequence'],
                name='simc_run_task_sequence_uniq',
            ),
        ]


class SimcProfile(models.Model):
    """
    SimC配置模型 - 玩家配置预设，绑定专精并保存 Battle.net 或手动装备块导入信息。
    """
    SOURCE_USER = 'user'
    SOURCE_SIMC_UPSTREAM = 'simc_upstream'
    SOURCE_WCL = 'wcl'
    SOURCE_CHOICES = (
        (SOURCE_USER, '用户维护'),
        (SOURCE_SIMC_UPSTREAM, 'SimC源码同步'),
    )

    user_id = models.IntegerField(null=True, blank=True, help_text="用户ID，NULL表示系统玩家配置")
    name = models.CharField(max_length=200, help_text="配置名称")
    source = models.CharField(max_length=32, choices=SOURCE_CHOICES, default=SOURCE_USER, help_text="配置来源")
    system_key = models.CharField(max_length=200, null=True, blank=True, unique=True, help_text="系统配置稳定标识")
    class_name = models.CharField(max_length=50, default='', blank=True, help_text="职业英文名，如 warrior")
    version = models.CharField(max_length=16, default='12.0', help_text="Profile 游戏版本，如 12.0")
    profile_set = models.CharField(max_length=16, default='MID1', help_text="SimC 上游 Profile 集合，如 MID1/MID2")
    use_ptr = models.BooleanField(default=False, help_text="模拟时使用 PTR 数据库")
    sync_version = models.CharField(max_length=128, default='', blank=True, help_text="同步来源版本/提交")
    spec = models.CharField(max_length=100, default="fury", help_text="专精标识，如 fury/arms/fire")
    player_config_mode = models.CharField(max_length=50, default="battlenet", help_text="玩家配置来源：battlenet/manual_equipment")
    battlenet_region = models.CharField(max_length=20, default="", blank=True)
    battlenet_realm = models.CharField(max_length=100, default="", blank=True)
    battlenet_character = models.CharField(max_length=100, default="", blank=True)
    player_equipment = models.TextField(default="", blank=True, help_text="手动装备/天赋玩家块")
    talent = models.CharField(max_length=2000, default="")
    # 仅在用户明确填写时保存属性覆盖；NULL 表示继承玩家装备/基线，不伪造默认值。
    gear_strength = models.IntegerField(null=True, blank=True)
    gear_crit = models.IntegerField(null=True, blank=True)
    gear_haste = models.IntegerField(null=True, blank=True)
    gear_mastery = models.IntegerField(null=True, blank=True)
    gear_versatility = models.IntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True, help_text="是否启用")
    
    class Meta:
        db_table = 'simc_profile'
        verbose_name = 'SimC配置'
        verbose_name_plural = 'SimC配置'

    def save(self, *args, **kwargs):
        # The canonical key is derived rather than caller-controlled, so SQLite
        # and MySQL both enforce one active upstream baseline per specialization.
        if self.user_id is None and self.source == self.SOURCE_SIMC_UPSTREAM and self.is_active:
            self.system_key = f'simc_upstream:{self.spec}'
            if kwargs.get('update_fields') is not None:
                kwargs['update_fields'] = set(kwargs['update_fields']) | {'system_key'}
        super().save(*args, **kwargs)


class SimcTalentString(models.Model):
    """可独立选择、按专精归属的 SimC 天赋字符串资源。"""
    name = models.CharField(max_length=200)
    spec = models.CharField(max_length=100, default='fury')
    talent = models.CharField(max_length=2000)
    hero_talent_names = models.JSONField(default=list, blank=True)
    # Authoritative fallback APL for this talent variant.  NULL deliberately
    # inherits the legacy specialization APL so old resources stay runnable.
    default_apl = models.ForeignKey(
        'SimcApl', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='default_for_talent_strings',
    )
    system_key = models.CharField(max_length=160, unique=True, null=True, blank=True)
    owner_user_id = models.IntegerField(null=True, blank=True)
    is_system = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    is_selectable = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'simc_talent_string'
        ordering = ['name', 'id']
        indexes = [models.Index(fields=['spec', 'is_active', 'is_selectable'])]


class SimcSecondaryStatRule(models.Model):
    """
    SimC副属性绿字转换规则（按职业）
    crit/haste/mastery/virt 每1%所需绿字是职业级数据，同一职业所有专精共享。
    """
    class_name = models.CharField(max_length=50, unique=True, help_text="职业标识，如 warrior/mage/priest")
    crit_per_percent = models.FloatField(default=46, help_text="暴击每1%所需绿字")
    haste_per_percent = models.FloatField(default=44, help_text="急速每1%所需绿字")
    mastery_per_percent = models.FloatField(default=46, help_text="精通每1%所需绿字（系数前）")
    versatility_per_percent = models.FloatField(default=54, help_text="全能每1%所需绿字")

    class Meta:
        db_table = 'simc_secondary_stat_rule'
        verbose_name = 'SimC绿字转换规则'
        verbose_name_plural = 'SimC绿字转换规则'


class SimcMasteryCoefficient(models.Model):
    """
    SimC精通系数（按专精）
    mastery_coefficient 是专精级数据，同一职业不同专精不同。
    """
    spec = models.CharField(max_length=50, unique=True, help_text="规范专精标识，如 warrior_fury/mage_fire")
    mastery_coefficient = models.FloatField(default=1.4, help_text="精通系数（最终结果乘以该值）")

    class Meta:
        db_table = 'simc_mastery_coefficient'
        verbose_name = 'SimC精通系数'
        verbose_name_plural = 'SimC精通系数'


class SimcContentTemplate(models.Model):
    """
    SimC 基础输入模板。

    该表只保存用于拼装最终 SimC 输入的大基础模板；玩家配置和 APL
    分别使用 SimcProfile 与 SimcApl 表。
    """
    SOURCE_SIMC_UPSTREAM = 'simc_upstream'
    SOURCE_USER = 'user'
    SOURCE_CHOICES = (
        (SOURCE_SIMC_UPSTREAM, 'SimC源码同步'),
        (SOURCE_USER, '用户维护'),
    )

    name = models.CharField(max_length=200, default='', blank=True, help_text="展示名称")
    source = models.CharField(max_length=32, choices=SOURCE_CHOICES, default=SOURCE_USER, help_text="内容来源")
    spec = models.CharField(max_length=100, default="default", help_text="专精标识，如 warrior_fury/default")
    class_name = models.CharField(max_length=50, default='', blank=True, help_text="职业英文名，如 warrior")
    content = models.TextField(help_text="模板/APL内容")
    is_active = models.BooleanField(default=True, help_text="是否启用")
    is_selectable = models.BooleanField(default=True, help_text="任务发起时是否可选择")
    sync_version = models.CharField(max_length=128, default='', blank=True, help_text="同步来源版本/提交")
    owner_user_id = models.BigIntegerField(null=True, blank=True, help_text="所属用户ID，NULL表示全局模板")
    active_unique_key = models.CharField(max_length=200, null=True, blank=True, unique=True, help_text="活跃时唯一键，非活跃时为NULL")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'simc_content_template'
        verbose_name = 'SimC模板'
        verbose_name_plural = 'SimC模板'
        indexes = [
            models.Index(fields=['spec', 'is_active']),
            models.Index(fields=['source']),
        ]

    def _normalize_name(self):
        """Normalize the optional name used by active-template uniqueness keys."""
        if not self.name:
            return ''
        return self.name.lower().strip()

    def _compute_active_unique_key(self):
        """
        Compute active_unique_key based on owner and spec.
        Returns None if is_active=False.
        """
        if not self.is_active:
            return None

        owner = 'global' if self.owner_user_id is None else self.owner_user_id
        spec = self.spec or 'default'
        return f'base_template:{owner}:{spec}'

    def save(self, *args, **kwargs):
        self.active_unique_key = self._compute_active_unique_key()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name or self.spec or f'基础模板 {self.pk}'


class SimcBackendBinary(models.Model):
    identifier = models.SlugField(max_length=64, unique=True, help_text="稳定标识，如 production/ptr")
    name = models.CharField(max_length=100, help_text="展示名称，如 正式服/PTR")
    platform = models.CharField(max_length=32, default="linux64", help_text="平台标识，如 linux64/linuxarm64")
    simc_path = models.CharField(max_length=500, default="", help_text="SimC本地编译产物路径")
    is_active = models.BooleanField(default=True, help_text="是否允许新任务选择")
    local_worker_enabled = models.BooleanField(default=True, help_text="本地Worker是否接收新任务")
    current_version = models.CharField(max_length=128, default="", help_text="当前SimC版本号/构建标识")
    game_build = models.CharField(
        max_length=64, default="", blank=True,
        help_text="该后端二进制对应的 WoW 数据版本；不参与 APL 字段去重",
    )
    latest_version = models.CharField(max_length=128, default="", blank=True, help_text="检测到的源码上游版本/提交")
    auto_update = models.BooleanField(default=True, help_text="是否自动拉取并编译更新")
    maintenance_enabled = models.BooleanField(default=True, help_text="是否启用每日SimC维护窗口")
    maintenance_daily_time = models.CharField(max_length=5, default="03:00", help_text="每日维护开始时间（Asia/Shanghai，HH:MM）")
    maintenance_window_minutes = models.PositiveIntegerField(default=60, help_text="每日维护窗口分钟数")
    maintenance_policy_revision = models.PositiveIntegerField(default=1, help_text="Agent维护策略版本")
    is_updating = models.BooleanField(default=False, help_text="是否正在本地编译更新")
    update_progress = models.IntegerField(default=0, help_text="更新进度百分比 0-100")
    update_status = models.CharField(max_length=255, default="", blank=True, help_text="更新状态提示")
    last_error = models.CharField(max_length=500, default="", blank=True, help_text="最近更新错误")
    last_checked_at = models.DateTimeField(null=True, blank=True, help_text="上次检查时间")
    last_updated_at = models.DateTimeField(null=True, blank=True, help_text="上次更新时间")

    class Meta:
        db_table = 'simc_backend_binary'
        verbose_name = 'SimC后端软件'
        verbose_name_plural = 'SimC后端软件'

    def __str__(self):
        return f'{self.name} ({self.identifier})'


class SimcAgent(models.Model):
    STATUS_UNREGISTERED = 'unregistered'
    STATUS_ONLINE = 'online'
    STATUS_BUSY = 'busy'
    STATUS_DEGRADED = 'degraded'
    STATUS_CHOICES = (
        (STATUS_UNREGISTERED, 'Unregistered'), (STATUS_ONLINE, 'Online'),
        (STATUS_BUSY, 'Busy'), (STATUS_DEGRADED, 'Degraded'),
    )

    backend = models.ForeignKey(SimcBackendBinary, on_delete=models.PROTECT, related_name='agents')
    host_identifier = models.CharField(max_length=128, unique=True)
    name = models.CharField(max_length=100, default='', blank=True)
    is_active = models.BooleanField(default=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_UNREGISTERED)
    platform = models.CharField(max_length=32, default='')
    agent_version = models.CharField(max_length=64, default='', blank=True)
    agent_revision = models.CharField(
        max_length=64, default='', blank=True,
        help_text='运行中的 LMonitor Agent Git commit',
    )
    protocol_version = models.PositiveIntegerField(default=1)
    capabilities = models.JSONField(default=dict, blank=True)
    instance_id = models.CharField(max_length=128, default='', blank=True)
    current_version = models.CharField(max_length=128, default='', blank=True)
    binary_available = models.BooleanField(default=False)
    token_id = models.CharField(max_length=32, null=True, blank=True, unique=True)
    token_hash = models.CharField(max_length=255, default='', blank=True)
    registered_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'simc_agent'
        constraints = [
            models.CheckConstraint(condition=models.Q(status__in=(
                'unregistered', 'online', 'busy', 'degraded',
            )), name='simc_agent_status_ck'),
            models.CheckConstraint(condition=(
                models.Q(token_hash='') & (models.Q(token_id__isnull=True) | models.Q(token_id=''))
            ) | (
                ~models.Q(token_hash='') & models.Q(token_id__isnull=False) & ~models.Q(token_id='')
            ), name='simc_agent_token_pair_ck'),
        ]

    def is_online(self, timeout_seconds=90, now=None):
        if self.status not in {self.STATUS_ONLINE, self.STATUS_BUSY, self.STATUS_DEGRADED} or not self.last_seen_at:
            return False
        return self.last_seen_at >= (now or timezone.now()) - timedelta(seconds=timeout_seconds)


class SimcAgentMaintenanceTask(models.Model):
    """A Dashboard-requested, agent-polled one-off SimC maintenance operation."""
    STATUS_PENDING = 'pending'
    STATUS_RUNNING = 'running'
    STATUS_SUCCESS = 'success'
    STATUS_FAILED = 'failed'
    STATUS_CANCELLED = 'cancelled'
    STATUS_CHOICES = (
        (STATUS_PENDING, 'Pending'), (STATUS_RUNNING, 'Running'),
        (STATUS_SUCCESS, 'Success'), (STATUS_FAILED, 'Failed'), (STATUS_CANCELLED, 'Cancelled'),
    )

    agent = models.ForeignKey(SimcAgent, on_delete=models.PROTECT, related_name='maintenance_tasks')
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    requested_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=500, default='', blank=True)

    class Meta:
        db_table = 'simc_agent_maintenance_task'
        ordering = ['-requested_at', '-id']
        indexes = [models.Index(fields=['agent', 'status', 'id'], name='simc_agmaint_agent_state_idx')]
        constraints = [models.CheckConstraint(
            condition=models.Q(status__in=('pending', 'running', 'success', 'failed', 'cancelled')),
            name='simc_agmaint_status_ck',
        )]


class SimcAgentEnrollmentCode(models.Model):
    code_id = models.CharField(max_length=32, unique=True)
    secret_hash = models.CharField(max_length=255)
    backend = models.ForeignKey(
        SimcBackendBinary, on_delete=models.PROTECT, related_name='agent_enrollment_codes',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='created_simc_agent_enrollment_codes',
    )
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    consumed_by_agent = models.ForeignKey(
        SimcAgent, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='consumed_enrollment_codes',
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'simc_agent_enrollment_code'
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['backend', 'expires_at'], name='simc_agcode_back_exp_idx'),
            models.Index(fields=['consumed_at', 'revoked_at'], name='simc_agcode_state_idx'),
        ]


class SimcBenchmarkPanel(models.Model):
    """A reusable benchmark definition; execution remains owned by SimcTask/SimulationRun."""

    BENCHMARK_TYPE_STANDARD = 'standard'
    BENCHMARK_TYPE_OPTION_GAIN = 'option_gain'
    BENCHMARK_TYPE_CHOICES = (
        (BENCHMARK_TYPE_STANDARD, '普通模拟'),
        (BENCHMARK_TYPE_OPTION_GAIN, '对比模拟'),
    )

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True)
    description = models.TextField(default='', blank=True)
    benchmark_type = models.CharField(
        max_length=24, choices=BENCHMARK_TYPE_CHOICES, default=BENCHMARK_TYPE_STANDARD,
    )
    comparison_option = models.CharField(max_length=50, default='', blank=True)
    comparison_config = models.JSONField(default=dict, blank=True)
    created_by_id = models.BigIntegerField()
    is_active = models.BooleanField(default=True)
    is_public = models.BooleanField(default=False)
    schedule_enabled = models.BooleanField(default=False)
    interval_seconds = models.PositiveIntegerField(
        default=86400, validators=[MinValueValidator(1)],
    )
    queue_priority = models.PositiveSmallIntegerField(
        default=SimcTask.QUEUE_PRIORITY_BENCHMARK_NORMAL,
        help_text='Benchmark 队列优先级；创建 Execution 时冻结到其 Task，始终低于普通模拟。',
    )
    next_run_at = models.DateTimeField(null=True, blank=True)
    last_scheduled_at = models.DateTimeField(null=True, blank=True)
    published_execution = models.ForeignKey(
        'SimcBenchmarkExecution', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='published_by_panels',
    )
    aggregate_baseline_execution = models.ForeignKey(
        'SimcBenchmarkExecution', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='aggregate_baseline_for_panels',
        help_text='Full rerun boundary; older Results remain auditable but are excluded from current aggregation.',
    )
    active_execution = models.OneToOneField(
        'SimcBenchmarkExecution', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='active_for_panel',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'simc_benchmark_panel'
        ordering = ['name', 'id']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(queue_priority__in=(10, 20, 30)),
                name='simc_bench_queue_priority_ck',
            ),
            models.CheckConstraint(
                condition=models.Q(interval_seconds__gt=0),
                name='simc_bench_interval_gt0_ck',
            ),
        ]
        indexes = [
            models.Index(
                fields=['schedule_enabled', 'is_active', 'next_run_at'],
                name='simc_bench_due_idx',
            ),
            models.Index(fields=['is_active', 'is_public'], name='simc_bench_vis_idx'),
        ]


class SimcBenchmarkPurgeTask(models.Model):
    """Durable, auditable background purge for one Benchmark panel graph."""

    STATUS_PENDING = 'pending'
    STATUS_RUNNING = 'running'
    STATUS_CLEANUP_PENDING = 'cleanup_pending'
    STATUS_CLEANING = 'cleaning'
    STATUS_RESTORE_PENDING = 'restore_pending'
    STATUS_RESTORING = 'restoring'
    STATUS_SUCCEEDED = 'succeeded'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = tuple((value, value.replace('_', ' ').title()) for value in (
        STATUS_PENDING, STATUS_RUNNING, STATUS_CLEANUP_PENDING, STATUS_CLEANING,
        STATUS_RESTORE_PENDING, STATUS_RESTORING,
        STATUS_SUCCEEDED, STATUS_FAILED,
    ))

    panel = models.ForeignKey(
        SimcBenchmarkPanel, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='purge_tasks',
    )
    panel_id_snapshot = models.BigIntegerField()
    panel_name = models.CharField(max_length=200)
    requested_by_id = models.BigIntegerField()
    status = models.CharField(
        max_length=24, choices=STATUS_CHOICES, default=STATUS_PENDING,
    )
    fingerprint = models.CharField(max_length=64)
    batch_id = models.CharField(max_length=40, unique=True)
    plan = models.JSONField(default=dict)
    quarantine_map = models.JSONField(default=dict, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    claim_token = models.CharField(max_length=64, default='', blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    error_detail = models.TextField(default='', blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'simc_benchmark_purge_task'
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['status', 'created_at'], name='simc_bench_purge_q_idx'),
        ]


class SimcBenchmarkSpec(models.Model):
    """Specialization and immutable-resource selection within a benchmark panel."""

    panel = models.ForeignKey(
        SimcBenchmarkPanel, on_delete=models.CASCADE, related_name='specs',
    )
    class_name = models.CharField(max_length=50)
    spec_key = models.CharField(max_length=100)
    label = models.CharField(max_length=200)
    apl = models.ForeignKey(
        SimcApl, on_delete=models.PROTECT, related_name='benchmark_specs',
    )
    template = models.ForeignKey(
        SimcContentTemplate, on_delete=models.PROTECT, related_name='benchmark_specs',
    )
    backend = models.ForeignKey(
        SimcBackendBinary, on_delete=models.PROTECT, related_name='benchmark_specs',
    )
    # Raw SimC directives shared by every case of this specialization. Scenario
    # input is appended after this baseline when a task is frozen.
    additional_simc_input = models.TextField(default='', blank=True)
    is_enabled = models.BooleanField(default=True)
    display_order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'simc_benchmark_spec'
        ordering = ['display_order', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['panel', 'spec_key'], name='simc_bench_panel_spec_uniq',
            ),
        ]
        indexes = [
            models.Index(fields=['panel', 'is_enabled'], name='simc_bench_spec_en_idx'),
        ]


class SimcBenchmarkProfile(models.Model):
    """Player profile selected for one specialization in a benchmark panel."""

    panel_spec = models.ForeignKey(
        SimcBenchmarkSpec, on_delete=models.CASCADE, related_name='profiles',
    )
    profile = models.ForeignKey(
        SimcProfile, on_delete=models.PROTECT, related_name='benchmark_profiles',
    )
    talent_string = models.ForeignKey(
        'SimcTalentString', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='benchmark_profiles',
    )
    # Explicit Panel-level APL override. NULL inherits talent.default_apl and,
    # only for legacy rows, the containing specialization APL.
    apl = models.ForeignKey(
        SimcApl, null=True, blank=True, on_delete=models.PROTECT,
        related_name='benchmark_profiles',
    )
    label = models.CharField(max_length=200)
    is_enabled = models.BooleanField(default=True)
    display_order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'simc_benchmark_profile'
        ordering = ['display_order', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['panel_spec', 'profile', 'talent_string'],
                name='simc_bench_spec_profile_talent_uniq',
            ),
        ]
        indexes = [
            models.Index(
                fields=['panel_spec', 'is_enabled'], name='simc_bench_prof_en_idx',
            ),
        ]


class SimcBenchmarkScenario(models.Model):
    """Simulation parameter coordinate configured for a panel."""

    panel = models.ForeignKey(
        SimcBenchmarkPanel, on_delete=models.CASCADE, related_name='scenarios',
    )
    key = models.CharField(max_length=100)
    name = models.CharField(max_length=200)
    simulation_params = models.JSONField(default=dict, blank=True)
    is_enabled = models.BooleanField(default=True)
    display_order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'simc_benchmark_scenario'
        ordering = ['display_order', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['panel', 'key'], name='simc_bench_panel_scenario_uniq',
            ),
        ]
        indexes = [
            models.Index(fields=['panel', 'is_enabled'], name='simc_bench_scen_en_idx'),
        ]


class SimcBenchmarkCandidate(models.Model):
    """A report candidate definition; it does not represent a SimC process run."""

    panel = models.ForeignKey(
        SimcBenchmarkPanel, on_delete=models.CASCADE, related_name='candidates',
    )
    key = models.CharField(max_length=100)
    label = models.CharField(max_length=200)
    candidate_type = models.CharField(max_length=50)
    params = models.JSONField(default=dict, blank=True)
    spec_keys = models.JSONField(default=list, blank=True)
    # Supports absolute remote URLs and application-local /static/... paths.
    icon_url = models.CharField(max_length=500, default='', blank=True)
    effect = models.TextField(default='', blank=True)
    source_label = models.CharField(max_length=200, default='', blank=True)
    is_enabled = models.BooleanField(default=True)
    display_order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'simc_benchmark_candidate'
        ordering = ['display_order', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['panel', 'key'], name='simc_bench_panel_candidate_uniq',
            ),
        ]
        indexes = [
            models.Index(fields=['panel', 'is_enabled'], name='simc_bench_cand_en_idx'),
        ]


class SimcBenchmarkExecution(models.Model):
    """Independent benchmark aggregate job; Tasks are internal execution details."""

    TRIGGER_MANUAL = 'manual'
    TRIGGER_SCHEDULE = 'schedule'
    TRIGGER_CHOICES = (
        (TRIGGER_MANUAL, 'Manual'),
        (TRIGGER_SCHEDULE, 'Schedule'),
    )
    STATUS_PENDING = 'pending'
    STATUS_RUNNING = 'running'
    STATUS_SUCCESS = 'success'
    STATUS_PARTIAL = 'partial'
    STATUS_FAILED = 'failed'
    STATUS_CANCELLED = 'cancelled'
    STATUS_CHOICES = tuple((value, value.title()) for value in (
        STATUS_PENDING, STATUS_RUNNING, STATUS_SUCCESS, STATUS_PARTIAL,
        STATUS_FAILED, STATUS_CANCELLED,
    ))

    panel = models.ForeignKey(
        SimcBenchmarkPanel, on_delete=models.CASCADE, related_name='executions',
    )
    trigger = models.CharField(
        max_length=16, choices=TRIGGER_CHOICES, default=TRIGGER_MANUAL,
    )
    scheduled_slot = models.DateTimeField(null=True, blank=True)
    config_snapshot = models.JSONField(default=dict, blank=True)
    display_metadata = models.JSONField(default=dict, blank=True)
    config_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    result_hash = models.CharField(max_length=64, blank=True, default='')
    results_finalized_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'simc_benchmark_execution'
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(
                fields=['panel', 'scheduled_slot'], name='simc_bench_panel_slot_uniq',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(trigger='schedule', scheduled_slot__isnull=False)
                    | models.Q(trigger='manual', scheduled_slot__isnull=True)
                ),
                name='simc_bench_trigger_slot_ck',
            ),
        ]
        indexes = [
            models.Index(fields=['panel', '-created_at'], name='simc_bench_exec_cr_idx'),
            models.Index(fields=['panel', 'completed_at'], name='simc_bench_exec_co_idx'),
            models.Index(fields=['config_hash'], name='simc_bench_cfg_hash_idx'),
        ]


class SimcBenchmarkCase(models.Model):
    """Maps one benchmark coordinate to its durable SimcTask execution history."""

    execution = models.ForeignKey(
        SimcBenchmarkExecution, on_delete=models.CASCADE, related_name='cases',
    )
    task = models.OneToOneField(
        SimcTask, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='benchmark_case',
    )
    status = models.CharField(
        max_length=16, choices=SimcBenchmarkExecution.STATUS_CHOICES,
        default=SimcBenchmarkExecution.STATUS_PENDING,
    )
    error_detail = models.TextField(blank=True, default='')
    spec_key = models.CharField(max_length=100)
    scenario_key = models.CharField(max_length=100)
    profile_key = models.CharField(max_length=100)
    spec_label = models.CharField(max_length=200)
    scenario_label = models.CharField(max_length=200)
    profile_label = models.CharField(max_length=200)
    coordinate_hash = models.CharField(max_length=64)

    class Meta:
        db_table = 'simc_benchmark_case'
        ordering = ['execution', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['execution', 'spec_key', 'scenario_key', 'profile_key'],
                name='simc_bench_exec_keys_uniq',
            ),
        ]
        indexes = [
            models.Index(fields=['execution', 'spec_key'], name='simc_bench_case_spec_idx'),
            models.Index(fields=['coordinate_hash'], name='simc_bench_coord_hash_idx'),
        ]

    def clean(self):
        super().clean()
        if not self.task_id:
            return
        try:
            task = SimcTask.objects.only('mode').get(pk=self.task_id)
        except (SimcTask.DoesNotExist, ValueError, TypeError):
            raise ValidationError({
                'task': 'The selected SimC task does not exist.',
            })
        if task.mode != 'comparison':
            raise ValidationError({
                'task': 'Benchmark cases require a task in comparison mode.',
            })


class SimcBenchmarkResult(models.Model):
    """One immutable raw DPS value in a finalized benchmark aggregate."""

    case = models.ForeignKey(
        SimcBenchmarkCase, on_delete=models.CASCADE, related_name='results',
    )
    candidate_key = models.CharField(max_length=100)
    dps = models.FloatField()
    hero_talent_names = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'simc_benchmark_result'
        ordering = ['case_id', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['case', 'candidate_key'], name='simc_bench_result_cand_uniq',
            ),
            models.CheckConstraint(
                condition=models.Q(dps__gt=0), name='simc_bench_result_dps_gt0_ck',
            ),
        ]

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


class SystemAlert(models.Model):
    category = models.CharField(max_length=64, help_text="报警分类，如 WECHAT_COOKIE_EXPIRED/SIMC_UPDATE_FAILED")
    subject = models.CharField(max_length=128, default="", blank=True, help_text="报警主体，如 wechat/api.bilibili.com/win64")
    dedup_key = models.CharField(max_length=220, unique=True, help_text="去重键 category@subject")
    level = models.IntegerField(default=3, help_text="级别 1=info 2=warning 3=fatal")
    title = models.CharField(max_length=200, help_text="标题")
    content = models.TextField(default="", blank=True, help_text="详细信息")
    count = models.IntegerField(default=1, help_text="累计触发次数")
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'system_alert'
        verbose_name = '系统报警'
        verbose_name_plural = '系统报警'
        indexes = [
            models.Index(fields=['is_read']),
            models.Index(fields=['category']),
            models.Index(fields=['last_seen_at']),
        ]



class SeasonMeta(models.Model):
    """赛季元数据"""
    season_key = models.CharField("赛季标识", max_length=30, unique=True, help_text="赛季标识，如 tww-s3")
    season_name = models.CharField("赛季名称", max_length=100, help_text="赛季名称")
    is_active = models.BooleanField("是否当前赛季", default=True, help_text="是否当前赛季")
    rio_season = models.CharField("Raider.IO赛季", max_length=30, null=True, blank=True, help_text="Raider.IO 赛季标识，如 season-tww-3")
    wcl_partition = models.IntegerField("WCL分区号", default=3, help_text="WCL 分区号")
    mplus_zone_id = models.IntegerField("M+区域ID", help_text="M+ 区域 ID")
    mplus_zone_name = models.CharField("M+区域名称", max_length=100, null=True, blank=True, help_text="M+ 区域名称")
    raid_zone_id = models.IntegerField("团本区域ID", help_text="团本区域 ID")
    raid_zone_name = models.CharField("团本区域名称", max_length=100, null=True, blank=True, help_text="团本区域名称")
    raid_zones = models.JSONField("团本区域列表", default=list, blank=True,
        help_text='[{"zone_id": 123, "zone_name": "Raid Name", "encounters": [{"id": 1, "name": "Boss"}]}]')
    mplus_encounters = models.JSONField("M+副本列表", default=list, blank=True, help_text="M+ 副本列表 [{id, name, short}, ...]")
    raid_encounters = models.JSONField("团本Boss列表", default=list, blank=True, help_text="团本 Boss 列表 [{id, name, index}, ...]")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wow_spec_season_meta'
        app_label = 'botend'
        verbose_name = '赛季元数据'
        verbose_name_plural = '赛季元数据'

    def __str__(self):
        return self.season_key


class PlayerSpecTopPlayer(models.Model):
    """人物资料缓存（Raider.IO + Battle.net），每角色每专精每赛季 1 条"""
    season_id = models.IntegerField("赛季ID", help_text="赛季 ID")
    region = models.CharField("区域", max_length=10, help_text="区域 us/eu/kr/tw")
    realm = models.CharField("服务器", max_length=50, help_text="服务器")
    character_name = models.CharField("角色名", max_length=50, help_text="角色名")
    class_name = models.CharField("职业", max_length=30, help_text="职业名，如 DeathKnight")
    spec_name = models.CharField("专精", max_length=30, help_text="专精名，如 Frost")
    rank = models.IntegerField("排名", null=True, blank=True, help_text="排名")
    score = models.FloatField("M+分数", null=True, blank=True, help_text="M+ 分数")
    faction = models.CharField("阵营", max_length=10, null=True, blank=True, help_text="阵营")
    race = models.CharField("种族", max_length=30, null=True, blank=True, help_text="种族")
    gender = models.CharField("性别", max_length=10, null=True, blank=True, help_text="性别")
    guild_name = models.CharField("公会", max_length=100, null=True, blank=True, help_text="公会名")
    realm_rank = models.IntegerField("服内排名", null=True, blank=True, help_text="服务器排名")
    avatar_url = models.CharField("头像", max_length=500, null=True, blank=True, help_text="头像 URL")
    profile_url = models.CharField("角色主页", max_length=500, null=True, blank=True, help_text="Raider.IO 主页 URL")
    achievement_points = models.IntegerField("成就点数", null=True, blank=True, help_text="成就点数")
    item_level = models.FloatField("装等", null=True, blank=True, help_text="装等")
    gear_json = models.JSONField("装备", default=list, blank=True, help_text="装备列表")
    talents_json = models.JSONField("天赋", default=list, blank=True, help_text="天赋数据缓存")
    talent_build_code = models.TextField("天赋字符串", blank=True, default="", help_text="原始天赋导入字符串")
    stats_json = models.JSONField("属性面板", default=dict, blank=True, help_text="属性面板")
    stats_crawl_status = models.IntegerField("采集状态", default=0, help_text="0=待采集 1=已采集 -1=失败")
    last_updated = models.DateTimeField("更新时间", null=True, blank=True, help_text="数据更新时间")

    class Meta:
        db_table = 'wow_spec_top_player'
        app_label = 'botend'
        verbose_name = '专精人物榜'
        verbose_name_plural = '专精人物榜'
        unique_together = (('region', 'realm', 'character_name', 'spec_name', 'season_id'),)
        indexes = [
            models.Index(fields=['spec_name', 'season_id', 'score'], name='idx_spec_score'),
            models.Index(fields=['season_id']),
            models.Index(fields=['class_name', 'spec_name']),
            models.Index(fields=['season_id', 'class_name', '-score', 'rank', 'id'], name='idx_season_class_top'),
        ]

    def __str__(self):
        return f"{self.character_name}-{self.realm} ({self.spec_name})"


class SpecDungeonRanking(models.Model):
    """M+ 副本排名原始数据，每条=一个玩家在某副本某专精的一次排名记录，来自 WCL API"""
    season_id = models.IntegerField("赛季ID", help_text="赛季 ID")
    dungeon_id = models.IntegerField("副本ID", help_text="WCL encounter ID")
    dungeon_name = models.CharField("副本名称", max_length=100, help_text="副本名称")
    class_name = models.CharField("职业", max_length=30, help_text="职业名")
    spec_name = models.CharField("专精", max_length=30, help_text="专精名")

    # 玩家信息
    character_name = models.CharField("角色名", max_length=50, help_text="角色名")
    realm = models.CharField("服务器", max_length=50, null=True, blank=True, help_text="服务器")
    region = models.CharField("区域", max_length=10, null=True, blank=True, help_text="区域")

    # 实战数据
    dps = models.FloatField("DPS", help_text="原始 DPS")
    keystone_level = models.IntegerField("钥石等级", null=True, blank=True, help_text="钥石等级")
    clear_time = models.IntegerField("通关时间(ms)", null=True, blank=True, help_text="通关时间(ms)")
    score = models.FloatField("M+分数", null=True, blank=True, help_text="M+ 分数")
    medal = models.CharField("奖牌", max_length=20, null=True, blank=True, help_text="gold/silver/bronze")
    affixes = models.JSONField("词缀", default=list, blank=True, help_text="词缀列表")

    # 天赋（原始数据，来自 WCL）
    talents_json = models.JSONField("天赋", default=list, blank=True, help_text="天赋数据缓存")
    talent_build_code = models.TextField("天赋字符串", blank=True, default="", help_text="原始天赋导入字符串")

    # 装备（原始数据，来自 WCL）
    gear_json = models.JSONField("装备", default=list, blank=True, help_text="装备数据")

    # 其他
    faction = models.IntegerField("阵营", null=True, blank=True, help_text="0=联盟 1=部落")
    guild_name = models.CharField("公会", max_length=100, null=True, blank=True, help_text="公会名")
    report_code = models.CharField("WCL报告码", max_length=50, null=True, blank=True, help_text="WCL report code")
    fight_id = models.IntegerField("FightID", null=True, blank=True, help_text="WCL fight ID")
    last_updated = models.DateTimeField("更新时间", null=True, blank=True, help_text="数据更新时间")

    class Meta:
        db_table = 'wow_spec_dungeon_ranking'
        app_label = 'botend'
        verbose_name = 'M+副本排名'
        verbose_name_plural = 'M+副本排名'
        indexes = [
            models.Index(fields=['season_id', 'dungeon_id', 'class_name', 'spec_name'], name='idx_dungeon_spec'),
            models.Index(fields=['class_name', 'spec_name', 'season_id', 'dps'], name='idx_dungeon_spec_dps'),
        ]

    def __str__(self):
        return f"{self.character_name} - {self.dungeon_name} ({self.spec_name}) {self.dps}"


class SpecRaidRanking(models.Model):
    """团本排名原始数据，每条=一个玩家在某 Boss 某专精的一次排名记录，来自 WCL API，Mythic only"""
    season_id = models.IntegerField("赛季ID", help_text="赛季 ID（SeasonMeta.id）")
    boss_id = models.IntegerField("BossID", help_text="WCL encounter ID")
    boss_name = models.CharField("Boss名称", max_length=100, help_text="Boss 名称")
    raid_zone_id = models.IntegerField("团本区域ID", null=True, blank=True)
    raid_zone_name = models.CharField("团本区域名称", max_length=100, default='', blank=True)
    class_name = models.CharField("职业", max_length=30, help_text="职业名")
    spec_name = models.CharField("专精", max_length=30, help_text="专精名")

    # 玩家信息
    character_name = models.CharField("角色名", max_length=50, help_text="角色名")
    realm = models.CharField("服务器", max_length=50, null=True, blank=True, help_text="服务器")
    region = models.CharField("区域", max_length=10, null=True, blank=True, help_text="区域")

    # 实战数据
    dps = models.FloatField("DPS", help_text="原始 DPS")
    kill_time = models.IntegerField("击杀时间(ms)", null=True, blank=True, help_text="击杀时间(ms)")

    # 天赋
    talents_json = models.JSONField("天赋", default=list, blank=True, help_text="天赋数据缓存")
    talent_build_code = models.TextField("天赋字符串", blank=True, default="", help_text="原始天赋导入字符串")

    # 装备
    gear_json = models.JSONField("装备", default=list, blank=True, help_text="装备数据")

    # 其他
    faction = models.IntegerField("阵营", null=True, blank=True, help_text="0=联盟 1=部落")
    guild_name = models.CharField("公会", max_length=100, null=True, blank=True, help_text="公会名")
    report_code = models.CharField("WCL报告码", max_length=50, null=True, blank=True, help_text="WCL report code")
    fight_id = models.IntegerField("FightID", null=True, blank=True, help_text="WCL fight ID")
    last_updated = models.DateTimeField("更新时间", null=True, blank=True, help_text="数据更新时间")

    class Meta:
        db_table = 'wow_spec_raid_ranking'
        app_label = 'botend'
        verbose_name = '团本排名'
        verbose_name_plural = '团本排名'
        indexes = [
            models.Index(fields=['season_id', 'boss_id', 'class_name', 'spec_name'], name='idx_boss_spec'),
            models.Index(fields=['class_name', 'spec_name', 'season_id', 'dps'], name='idx_raid_spec_dps'),
        ]

    def __str__(self):
        return f"{self.character_name} - {self.boss_name} ({self.spec_name}) {self.dps}"


class WowTalentVersion(models.Model):
    """WoW 天赋元数据版本组。"""
    key = models.CharField(max_length=64, unique=True)
    label = models.CharField(max_length=128, default='', blank=True)
    branch = models.CharField(max_length=16, default='retail', blank=True)
    major_version = models.CharField(max_length=32, default='', blank=True)
    current_build = models.CharField(max_length=32, default='', blank=True)
    is_active = models.BooleanField(default=False)
    is_default_simulator = models.BooleanField(default=False)
    is_default_player_tree = models.BooleanField(default=False)
    is_default_stats = models.BooleanField(default=False)
    status = models.CharField(max_length=24, default='draft', blank=True)
    source_dir = models.CharField(max_length=255, default='', blank=True)
    notes = models.TextField(default='', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    activated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'wow_talent_version'
        app_label = 'botend'
        indexes = [
            models.Index(fields=['branch', 'major_version'], name='idx_talent_ver_branch_major'),
            models.Index(fields=['is_active'], name='idx_talent_ver_active'),
        ]

    def __str__(self):
        return self.label or self.key


class WowTalentNodeMetadata(models.Model):
    """WoW 天赋节点元数据缓存，用于树形展示和名称/图标补全。"""
    talent_version = models.ForeignKey(
        WowTalentVersion,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='nodes',
    )
    class_name = models.CharField(max_length=30, default="", blank=True)
    spec_name = models.CharField(max_length=30, default="", blank=True)
    tree_type = models.CharField(max_length=16, default="spec", blank=True)
    node_id = models.BigIntegerField(null=True, blank=True)
    spell_id = models.BigIntegerField(null=True, blank=True)
    display_spell_id = models.BigIntegerField(null=True, blank=True)
    talent_id = models.BigIntegerField(null=True, blank=True)
    name = models.CharField(max_length=255, default="", blank=True)
    name_zh = models.CharField(max_length=255, default="", blank=True)
    icon = models.CharField(max_length=255, default="", blank=True)
    row = models.IntegerField(null=True, blank=True)
    column = models.IntegerField(null=True, blank=True)
    max_points = models.IntegerField(default=1)
    parents_json = models.JSONField(default=list, blank=True)
    description = models.TextField(default="", blank=True)
    description_zh = models.TextField(default="", blank=True)
    source = models.CharField(max_length=32, default="derived", blank=True)
    last_updated = models.DateTimeField(default=timezone.now)
    db2_subtree_id = models.IntegerField(default=0, blank=True)
    db2_tree_id = models.IntegerField(null=True, blank=True)
    db2_component_id = models.IntegerField(default=0, blank=True)
    flags = models.IntegerField(default=0, help_text='DB2 TraitNode.Flags；Flags=8 表示赠送天赋（默认授予，无法取消）')

    class Meta:
        db_table = 'wow_talent_node_metadata'
        app_label = 'botend'
        verbose_name = 'WoW天赋节点元数据'
        verbose_name_plural = 'WoW天赋节点元数据'
        unique_together = (('talent_version', 'class_name', 'spec_name', 'tree_type', 'node_id', 'spell_id'),)
        indexes = [
            models.Index(fields=['talent_version', 'class_name', 'spec_name', 'tree_type'], name='idx_talent_meta_ver_spec'),
            models.Index(fields=['spell_id'], name='idx_talent_meta_spell'),
            models.Index(fields=['talent_version', 'talent_id'], name='idx_talent_meta_ver_talent'),
        ]

    def __str__(self):
        return f"{self.class_name}/{self.spec_name}/{self.tree_type}/{self.node_id or self.spell_id}"



class WowItemSnapshot(models.Model):
    """WoW 装备/宝石/附魔元数据快照，用于中文名称、描述和 tooltip 展示。"""
    id = models.AutoField(primary_key=True)
    item_id = models.BigIntegerField(unique=True, help_text="物品ID（装备/宝石/附魔通用）")
    name = models.CharField(max_length=255, default="", blank=True, help_text="英文名称")
    name_zh = models.CharField(max_length=255, default="", blank=True, help_text="中文名称")
    description = models.TextField(default="", blank=True, help_text="英文描述")
    description_zh = models.TextField(default="", blank=True, help_text="中文描述")
    icon = models.CharField(max_length=255, default="", blank=True, help_text="图标名称")
    quality = models.IntegerField(default=0, blank=True, help_text="品质等级")
    source = models.CharField(max_length=32, default="wowhead", blank=True, help_text="数据源")
    updated_at = models.DateTimeField(default=timezone.now, help_text="更新时间")

    class Meta:
        db_table = 'wow_item_snapshot'
        app_label = 'botend'
        verbose_name = 'WoW物品元数据快照'
        verbose_name_plural = 'WoW物品元数据快照'
        indexes = [
            models.Index(fields=['item_id'], name='idx_item_snapshot_id'),
            models.Index(fields=['updated_at'], name='idx_item_snapshot_updated'),
        ]

    def __str__(self):
        return f"{self.item_id}: {self.name_zh or self.name}"


class MythicDungeonDataVersion(models.Model):
    """大秘境路线规划器的数据版本。"""

    key = models.CharField(max_length=80, unique=True)
    label = models.CharField(max_length=160)
    game_version = models.CharField(max_length=40, default='', blank=True)
    season = models.CharField(max_length=80, default='', blank=True)
    schema_version = models.PositiveIntegerField(default=1)
    source_name = models.CharField(max_length=160, default='', blank=True)
    source_reference = models.CharField(max_length=500, default='', blank=True)
    source_hash = models.CharField(max_length=64, default='', blank=True)
    is_active = models.BooleanField(default=False)
    notes = models.TextField(default='', blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    imported_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'mythic_dungeon_data_version'
        ordering = ['-is_active', '-imported_at', 'key']
        indexes = [
            models.Index(fields=['is_active', 'imported_at'], name='md_data_active_idx'),
        ]

    def __str__(self):
        return self.label or self.key


class MythicDungeonSelectionGroup(models.Model):
    """某个数据版本下可独立维护的地下城赛季或分类标签。"""

    data_version = models.ForeignKey(
        MythicDungeonDataVersion,
        on_delete=models.CASCADE,
        related_name='selection_groups',
    )
    key = models.SlugField(max_length=100)
    name = models.CharField(max_length=160)
    name_zh = models.CharField(max_length=160, default='', blank=True)
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'mythic_dungeon_selection_group'
        ordering = ['order', 'name_zh', 'name']
        constraints = [
            models.UniqueConstraint(
                fields=['data_version', 'key'],
                name='uniq_md_select_group_version_key',
            ),
        ]
        indexes = [
            models.Index(
                fields=['data_version', 'is_active', 'order'],
                name='md_sel_group_ver_order_idx',
            ),
        ]

    @property
    def display_name(self):
        return self.name_zh or self.name or self.key

    def clean(self):
        super().clean()
        if (
            self.pk
            and self.memberships.exclude(
                dungeon__data_version_id=self.data_version_id,
            ).exists()
        ):
            raise ValidationError({
                'data_version': '分类已有其他数据版本的地下城成员，不能直接更换数据版本。',
            })

    def __str__(self):
        return self.display_name


class MythicDungeonSpell(models.Model):
    """某个 MDT 数据版本使用的法术公共资料快照。"""

    data_version = models.ForeignKey(
        MythicDungeonDataVersion,
        on_delete=models.CASCADE,
        related_name='spells',
    )
    spell_id = models.BigIntegerField()
    source_branch = models.CharField(max_length=32, default='', blank=True)
    source_locale = models.CharField(max_length=8, default='zhCN', blank=True)
    snapshot_build = models.CharField(max_length=64, default='', blank=True)
    name = models.CharField(max_length=255, default='', blank=True)
    name_zh = models.CharField(max_length=255, default='', blank=True)
    description = models.TextField(default='', blank=True)
    description_zh = models.TextField(default='', blank=True)
    aura_description = models.TextField(default='', blank=True)
    aura_description_zh = models.TextField(default='', blank=True)
    icon_file_data_id = models.BigIntegerField(null=True, blank=True)
    icon_name = models.CharField(max_length=255, default='', blank=True)
    icon_url = models.CharField(max_length=1000, default='', blank=True)
    is_active = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'mythic_dungeon_spell'
        ordering = ['spell_id']
        constraints = [
            models.UniqueConstraint(
                fields=['data_version', 'spell_id'],
                name='uniq_md_spell_version_id',
            ),
        ]
        indexes = [
            models.Index(
                fields=['data_version', 'is_active', 'spell_id'],
                name='md_spell_ver_active_idx',
            ),
            models.Index(fields=['spell_id'], name='md_spell_id_idx'),
            models.Index(
                fields=['source_branch', 'snapshot_build'],
                name='md_spell_source_idx',
            ),
        ]

    @property
    def display_name(self):
        return self.name_zh or self.name or f'技能 #{self.spell_id}'

    def __str__(self):
        return self.display_name


class MythicDungeon(models.Model):
    """某个数据版本内的一座地下城。"""

    data_version = models.ForeignKey(
        MythicDungeonDataVersion,
        on_delete=models.CASCADE,
        related_name='dungeons',
    )
    key = models.SlugField(max_length=100)
    external_index = models.IntegerField(null=True, blank=True)
    name = models.CharField(max_length=160)
    name_zh = models.CharField(max_length=160, default='', blank=True)
    short_name = models.CharField(max_length=32, default='', blank=True)
    map_id = models.IntegerField(null=True, blank=True)
    total_enemy_forces = models.PositiveIntegerField(default=0)
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'mythic_dungeon'
        ordering = ['order', 'name_zh', 'name']
        constraints = [
            models.UniqueConstraint(
                fields=['data_version', 'key'],
                name='uniq_md_dungeon_version_key',
            ),
        ]
        indexes = [
            models.Index(
                fields=['data_version', 'is_active', 'order'],
                name='md_dungeon_ver_order_idx',
            ),
        ]

    @property
    def display_name(self):
        return self.name_zh or self.name

    def __str__(self):
        return self.display_name


class MythicDungeonSelectionMembership(models.Model):
    """地下城在独立赛季或分类标签中的归属及顺序。"""

    selection_group = models.ForeignKey(
        MythicDungeonSelectionGroup,
        on_delete=models.CASCADE,
        related_name='memberships',
    )
    dungeon = models.ForeignKey(
        MythicDungeon,
        on_delete=models.CASCADE,
        related_name='selection_memberships',
    )
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'mythic_dungeon_selection_membership'
        ordering = ['selection_group__order', 'order', 'dungeon__order']
        constraints = [
            models.UniqueConstraint(
                fields=['selection_group', 'dungeon'],
                name='uniq_md_select_group_dungeon',
            ),
        ]
        indexes = [
            models.Index(
                fields=['selection_group', 'is_active', 'order'],
                name='md_sel_member_group_idx',
            ),
            models.Index(
                fields=['dungeon', 'is_active'],
                name='md_sel_member_dungeon_idx',
            ),
        ]

    def clean(self):
        super().clean()
        if (
            self.selection_group_id
            and self.dungeon_id
            and self.selection_group.data_version_id != self.dungeon.data_version_id
        ):
            raise ValidationError({
                'dungeon': '地下城与分类必须属于同一个数据版本。',
            })

    def __str__(self):
        return f'{self.selection_group.display_name} / {self.dungeon.display_name}'


class MythicDungeonFloor(models.Model):
    """地下城楼层和可替换的 Web 地图底图配置。"""

    dungeon = models.ForeignKey(
        MythicDungeon,
        on_delete=models.CASCADE,
        related_name='floors',
    )
    key = models.SlugField(max_length=100)
    floor_index = models.PositiveIntegerField(default=1)
    name = models.CharField(max_length=160)
    name_zh = models.CharField(max_length=160, default='', blank=True)
    background_url = models.CharField(max_length=1000, default='', blank=True)
    background_color = models.CharField(max_length=32, default='#66533f', blank=True)
    map_width = models.PositiveIntegerField(default=1000)
    map_height = models.PositiveIntegerField(default=700)
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'mythic_dungeon_floor'
        ordering = ['order', 'floor_index']
        constraints = [
            models.UniqueConstraint(
                fields=['dungeon', 'key'],
                name='uniq_md_floor_dungeon_key',
            ),
            models.UniqueConstraint(
                fields=['dungeon', 'floor_index'],
                name='uniq_md_floor_dungeon_index',
            ),
        ]
        indexes = [
            models.Index(
                fields=['dungeon', 'is_active', 'order'],
                name='md_floor_dungeon_order_idx',
            ),
        ]

    @property
    def display_name(self):
        return self.name_zh or self.name

    def __str__(self):
        return f'{self.dungeon.display_name} / {self.display_name}'


class MythicDungeonEnemy(models.Model):
    """地下城怪物原型；多个地图点共享同一组属性和技能。"""

    dungeon = models.ForeignKey(
        MythicDungeon,
        on_delete=models.CASCADE,
        related_name='enemies',
    )
    key = models.SlugField(max_length=120)
    npc_id = models.BigIntegerField(null=True, blank=True)
    name = models.CharField(max_length=160)
    name_zh = models.CharField(max_length=160, default='', blank=True)
    enemy_forces = models.PositiveIntegerField(default=0)
    base_health = models.PositiveBigIntegerField(default=0)
    level = models.PositiveIntegerField(default=0)
    creature_type = models.CharField(max_length=80, default='', blank=True)
    icon_url = models.CharField(max_length=1000, default='', blank=True)
    marker_color = models.CharField(max_length=32, default='#94a3b8', blank=True)
    is_boss = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    traits = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'mythic_dungeon_enemy'
        ordering = ['is_boss', 'name_zh', 'name', 'key']
        constraints = [
            models.UniqueConstraint(
                fields=['dungeon', 'key'],
                name='uniq_md_enemy_dungeon_key',
            ),
        ]
        indexes = [
            models.Index(
                fields=['dungeon', 'is_active', 'name'],
                name='md_enemy_dungeon_name_idx',
            ),
            models.Index(fields=['npc_id'], name='md_enemy_npc_idx'),
        ]

    @property
    def display_name(self):
        return self.name_zh or self.name

    def __str__(self):
        return self.display_name


class MythicDungeonAbility(models.Model):
    """怪物技能和可打断、驱散、危险度等路线决策信息。"""

    enemy = models.ForeignKey(
        MythicDungeonEnemy,
        on_delete=models.CASCADE,
        related_name='abilities',
    )
    spell_record = models.ForeignKey(
        MythicDungeonSpell,
        on_delete=models.SET_NULL,
        related_name='ability_links',
        null=True,
        blank=True,
    )
    spell_id = models.BigIntegerField()
    name = models.CharField(max_length=160)
    name_zh = models.CharField(max_length=160, default='', blank=True)
    description = models.TextField(default='', blank=True)
    description_zh = models.TextField(default='', blank=True)
    icon_url = models.CharField(max_length=1000, default='', blank=True)
    interruptible = models.BooleanField(default=False)
    dispel_type = models.CharField(max_length=40, default='', blank=True)
    danger_level = models.PositiveSmallIntegerField(default=1)
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'mythic_dungeon_ability'
        ordering = ['order', '-danger_level', 'name_zh', 'name']
        constraints = [
            models.UniqueConstraint(
                fields=['enemy', 'spell_id'],
                name='uniq_md_ability_enemy_spell',
            ),
        ]
        indexes = [
            models.Index(
                fields=['enemy', 'is_active', 'order'],
                name='md_ability_enemy_order_idx',
            ),
        ]

    @property
    def display_name(self):
        return self.name_zh or self.name

    def __str__(self):
        return f'{self.enemy.display_name} / {self.display_name}'


class MythicDungeonSpawn(models.Model):
    """怪物在某楼层上的刷新点，可携带分组和巡逻路径。"""

    enemy = models.ForeignKey(
        MythicDungeonEnemy,
        on_delete=models.CASCADE,
        related_name='spawns',
    )
    floor = models.ForeignKey(
        MythicDungeonFloor,
        on_delete=models.CASCADE,
        related_name='spawns',
    )
    key = models.SlugField(max_length=120)
    x = models.FloatField(default=50.0)
    y = models.FloatField(default=50.0)
    group_key = models.CharField(max_length=100, default='', blank=True)
    scale = models.FloatField(default=1.0)
    patrol = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'mythic_dungeon_spawn'
        ordering = ['floor__order', 'y', 'x', 'key']
        constraints = [
            models.UniqueConstraint(
                fields=['enemy', 'key'],
                name='uniq_md_spawn_enemy_key',
            ),
        ]
        indexes = [
            models.Index(
                fields=['floor', 'is_active'],
                name='md_spawn_floor_idx',
            ),
            models.Index(fields=['group_key'], name='md_spawn_group_idx'),
        ]

    def __str__(self):
        return f'{self.enemy.display_name} / {self.key}'


class MythicDungeonPoi(models.Model):
    """入口、出口、传送点、Boss 门等地图兴趣点。"""

    floor = models.ForeignKey(
        MythicDungeonFloor,
        on_delete=models.CASCADE,
        related_name='pois',
    )
    key = models.SlugField(max_length=120)
    poi_type = models.CharField(max_length=60, default='note')
    x = models.FloatField(default=50.0)
    y = models.FloatField(default=50.0)
    label = models.CharField(max_length=160, default='', blank=True)
    icon_url = models.CharField(max_length=1000, default='', blank=True)
    target_floor_key = models.CharField(max_length=100, default='', blank=True)
    is_active = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'mythic_dungeon_poi'
        ordering = ['poi_type', 'label', 'key']
        constraints = [
            models.UniqueConstraint(
                fields=['floor', 'key'],
                name='uniq_md_poi_floor_key',
            ),
        ]
        indexes = [
            models.Index(fields=['floor', 'is_active'], name='md_poi_floor_idx'),
        ]

    def __str__(self):
        return self.label or self.key


class MythicDungeonRoute(models.Model):
    """用户保存或公开分享的一条路线。"""

    share_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    owner_user_id = models.IntegerField(null=True, blank=True)
    dungeon = models.ForeignKey(
        MythicDungeon,
        on_delete=models.PROTECT,
        related_name='routes',
    )
    name = models.CharField(max_length=160)
    dungeon_level = models.PositiveIntegerField(default=10)
    route_data = models.JSONField(default=dict, blank=True)
    revision = models.PositiveIntegerField(default=1)
    is_public = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'mythic_dungeon_route'
        ordering = ['-updated_at', '-id']
        indexes = [
            models.Index(
                fields=['owner_user_id', 'is_active', 'updated_at'],
                name='md_route_owner_updated_idx',
            ),
            models.Index(
                fields=['share_id', 'is_public', 'is_active'],
                name='md_route_share_idx',
            ),
        ]

    def __str__(self):
        return self.name


def generate_mythic_route_share_token():
    """生成约 72 bit 熵的 URL-safe 短链接令牌。"""

    return secrets.token_urlsafe(9)


class MythicDungeonRouteShare(models.Model):
    """不依赖账号的只读路线分享快照。"""

    token = models.CharField(
        max_length=16,
        unique=True,
        default=generate_mythic_route_share_token,
        editable=False,
    )
    dungeon = models.ForeignKey(
        MythicDungeon,
        on_delete=models.PROTECT,
        related_name='route_shares',
    )
    name = models.CharField(max_length=160)
    dungeon_level = models.PositiveIntegerField(default=10)
    route_data = models.JSONField(default=dict, blank=True)
    content_hash = models.CharField(max_length=64, unique=True, editable=False)
    is_active = models.BooleanField(default=True)
    view_count = models.PositiveBigIntegerField(default=0)
    last_accessed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'mythic_dungeon_route_share'
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(
                fields=['is_active', 'created_at'],
                name='md_share_active_created_idx',
            ),
        ]

    def __str__(self):
        return f'{self.name} / {self.token}'


class MythicPlannerConfig(models.Model):
    """路线规划器的可维护运行配置。"""

    key = models.CharField(max_length=80, unique=True, default='default')
    default_dungeon_key = models.CharField(max_length=100, default='', blank=True)
    default_dungeon_level = models.PositiveIntegerField(default=10)
    min_dungeon_level = models.PositiveIntegerField(default=2)
    max_dungeon_level = models.PositiveIntegerField(default=35)
    group_selection_default = models.BooleanField(default=True)
    live_sync_enabled = models.BooleanField(default=True)
    allow_public_route_share = models.BooleanField(default=True)
    settings = models.JSONField(default=dict, blank=True)
    updated_by_user_id = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'mythic_planner_config'
        ordering = ['key']

    def __str__(self):
        return self.key
