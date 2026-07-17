import hashlib

from django.db import models
from django.utils import timezone


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
    resource_type = models.CharField(max_length=20, help_text="资源类型: profile/template/apl")
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


class SimcApl(models.Model):
    """
    SimC APL 统一存储：默认 APL（系统/个人维护）与个人 APL 共用一张表。
    通过 source/is_system/owner_user_id 区分。
    """
    id = models.BigAutoField(primary_key=True)
    SOURCE_SIMC_UPSTREAM = 'simc_upstream'
    SOURCE_USER = 'user'
    SOURCE_CHOICES = (
        (SOURCE_SIMC_UPSTREAM, 'SimC源码同步'),
        (SOURCE_USER, '用户维护'),
    )

    name = models.CharField(max_length=200, help_text="APL名称")
    spec = models.CharField(max_length=100, help_text="适用专精标识，如 warrior_fury")
    class_name = models.CharField(max_length=50, default='', blank=True, help_text="职业英文名，如 warrior")
    content = models.TextField(help_text="APL代码内容")
    source = models.CharField(max_length=32, choices=SOURCE_CHOICES, default=SOURCE_USER, help_text="内容来源")
    is_system = models.BooleanField(default=False, help_text="是否为系统默认APL（只读）")
    owner_user_id = models.BigIntegerField(null=True, blank=True, help_text="所属用户ID，NULL表示全局默认APL")
    is_active = models.BooleanField(default=True, help_text="是否启用")
    is_selectable = models.BooleanField(default=True, help_text="任务发起时是否可选择")
    sync_version = models.CharField(max_length=128, default='', blank=True, help_text="同步来源版本/提交")
    active_unique_key = models.CharField(
        max_length=255, null=True, blank=True, unique=True,
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
        self.active_unique_key = self._compute_active_unique_key()
        update_fields = kwargs.get('update_fields')
        if update_fields is not None and 'active_unique_key' not in update_fields:
            kwargs['update_fields'] = list(update_fields) + ['active_unique_key']
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.name} ({self.spec})'



class SimcTaskBatch(models.Model):
    """SimC任务批次 - 用于聚合对比任务（属性/天赋/饰品/装备/APL对比）"""
    user_id = models.IntegerField(help_text="用户ID")
    name = models.CharField(max_length=200, help_text="批次名称")
    batch_type = models.CharField(max_length=50, default='comparison', help_text="批次类型：comparison/attribute_sweep")
    request_manifest = models.TextField(null=True, blank=True, help_text="冻结批次输入（JSON）")
    status = models.IntegerField(default=0, help_text="0=待创建,1=运行中,2=完成,3=失败")
    error_detail = models.TextField(null=True, blank=True, help_text="错误详情")
    completed_at = models.DateTimeField(null=True, blank=True, help_text="完成时间")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'simc_task_batch'
        verbose_name = 'SimC任务批次'
        verbose_name_plural = 'SimC任务批次'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user_id', '-created_at']),
        ]


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

    batch = models.ForeignKey(SimcTaskBatch, null=True, blank=True, on_delete=models.SET_NULL, help_text="所属批次")
    candidate_label = models.CharField(max_length=200, default='', blank=True, help_text="对比任务标签，如 crit+1000")

    # Reference-based fields (live resource FKs)
    profile = models.ForeignKey('SimcProfile', null=True, blank=True, on_delete=models.SET_NULL, related_name='tasks', help_text="引用的玩家配置")
    template = models.ForeignKey('SimcContentTemplate', null=True, blank=True, on_delete=models.SET_NULL, related_name='tasks', help_text="引用的基础模板")
    apl = models.ForeignKey('SimcApl', null=True, blank=True, on_delete=models.SET_NULL, related_name='tasks', help_text="引用的APL")

    # NEW: Version FKs (immutable snapshots)
    profile_version = models.ForeignKey('SimcResourceVersion', null=True, blank=True, on_delete=models.SET_NULL, related_name='profile_tasks', help_text="Profile版本快照")
    template_version = models.ForeignKey('SimcResourceVersion', null=True, blank=True, on_delete=models.SET_NULL, related_name='template_tasks', help_text="Template版本快照")
    apl_version = models.ForeignKey('SimcResourceVersion', null=True, blank=True, on_delete=models.SET_NULL, related_name='apl_tasks', help_text="APL版本快照")

    mode = models.CharField(max_length=50, default='normal', blank=True, help_text="任务模式：normal/comparison/attribute_sweep")
    simulation_params = models.JSONField(null=True, blank=True, help_text="模拟参数：iterations, fight_style等")
    mode_params = models.JSONField(null=True, blank=True, help_text="模式参数：对比项、寻优范围等")
    source_task = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='reruns', help_text="重跑来源任务")

    error_detail = models.TextField(null=True, blank=True, help_text="创建或执行错误详情")
    result_summary = models.TextField(null=True, blank=True, help_text="结果摘要JSON：DPS/HPS等关键指标")

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
            models.Index(fields=['batch', '-create_time']),
        ]


class SimcTaskArtifact(models.Model):
    """SimC任务产物 - 分离存储HTML报告、JSON统计等文件路径"""
    task = models.ForeignKey(SimcTask, on_delete=models.CASCADE, related_name='artifacts')
    run = models.ForeignKey('SimulationRun', null=True, blank=True, on_delete=models.PROTECT,
                            related_name='artifacts', help_text="生成该产物的具体执行轮次")
    artifact_type = models.CharField(max_length=50, help_text="产物类型：html_report/json_stats/log")
    file_path = models.CharField(max_length=500, help_text="相对static/的文件路径")
    file_size = models.BigIntegerField(default=0, help_text="文件大小（字节）")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'simc_task_artifact'
        verbose_name = 'SimC任务产物'
        verbose_name_plural = 'SimC任务产物'
        indexes = [
            models.Index(fields=['task', 'artifact_type']),
        ]


class SimulationRun(models.Model):
    """
    SimulationRun - 单次 SimC 进程执行记录
    每个 Task 可能有多个 Run（多候选/多轮），每个 Run 是一次真实执行
    """
    task = models.ForeignKey(SimcTask, on_delete=models.CASCADE, related_name='simulation_runs', help_text="所属任务")
    sequence = models.IntegerField(default=1, help_text="执行序号（轮次/候选编号）")
    candidate_label = models.CharField(max_length=200, default='', blank=True, help_text="候选标签，如 baseline/crit+1000/apl_variant_2")

    status = models.CharField(max_length=20, default='pending', help_text="状态：pending/running/completed/failed")
    input_hash = models.CharField(max_length=64, default='', blank=True, help_text="本次输入的SHA256")
    resource_manifest = models.JSONField(null=True, blank=True, help_text="本次执行时解析的资源版本元数据")

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
    user_id = models.IntegerField(help_text="用户ID")
    name = models.CharField(max_length=200, help_text="配置名称")
    spec = models.CharField(max_length=100, default="fury", help_text="专精标识，如 fury/arms/fire")
    player_config_mode = models.CharField(max_length=50, default="battlenet", help_text="玩家配置来源：battlenet/manual_equipment")
    battlenet_region = models.CharField(max_length=20, default="", blank=True)
    battlenet_realm = models.CharField(max_length=100, default="", blank=True)
    battlenet_character = models.CharField(max_length=100, default="", blank=True)
    player_equipment = models.TextField(default="", blank=True, help_text="手动装备/天赋玩家块")
    talent = models.CharField(max_length=2000, default="")
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
    spec = models.CharField(max_length=50, unique=True, help_text="专精标识，如 fury/arms/fire")
    mastery_coefficient = models.FloatField(default=1.4, help_text="精通系数（最终结果乘以该值）")

    class Meta:
        db_table = 'simc_mastery_coefficient'
        verbose_name = 'SimC精通系数'
        verbose_name_plural = 'SimC精通系数'


class SimcContentTemplate(models.Model):
    """
    SimC 统一内容模板：基础输入模板、默认玩家装备、个人玩家装备共用一张表。
    APL 已迁移至 SimcApl 独立表。
    """
    TYPE_BASE_TEMPLATE = 'base_template'
    TYPE_DEFAULT_PLAYER = 'default_player'
    TYPE_CUSTOM_PLAYER = 'custom_player'
    TEMPLATE_TYPE_CHOICES = (
        (TYPE_BASE_TEMPLATE, '基础模板'),
        (TYPE_DEFAULT_PLAYER, '默认玩家装备模板'),
        (TYPE_CUSTOM_PLAYER, '用户自定义装备'),
    )
    SOURCE_SIMC_UPSTREAM = 'simc_upstream'
    SOURCE_USER = 'user'
    SOURCE_CHOICES = (
        (SOURCE_SIMC_UPSTREAM, 'SimC源码同步'),
        (SOURCE_USER, '用户维护'),
    )

    name = models.CharField(max_length=200, default='', blank=True, help_text="展示名称")
    template_type = models.CharField(max_length=32, choices=TEMPLATE_TYPE_CHOICES, default=TYPE_BASE_TEMPLATE, help_text="内容类型")
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
            models.Index(fields=['template_type', 'spec', 'is_active']),
            models.Index(fields=['source', 'template_type']),
        ]

    def _normalize_name(self):
        """Normalize name for custom_player uniqueness check (lowercase, strip whitespace)."""
        if not self.name:
            return ''
        return self.name.lower().strip()

    def _compute_active_unique_key(self):
        """
        Compute active_unique_key based on template_type, owner, spec, and name.
        Returns None if is_active=False.
        """
        if not self.is_active:
            return None

        template_type = self.template_type
        owner = 'global' if self.owner_user_id is None else self.owner_user_id
        spec = self.spec or 'default'

        if template_type in (self.TYPE_BASE_TEMPLATE, self.TYPE_DEFAULT_PLAYER):
            if owner == 'global':
                return f'{template_type}:global:{spec}'
            else:
                return f'{template_type}:{owner}:{spec}'
        elif template_type == self.TYPE_CUSTOM_PLAYER:
            return f'{template_type}:{owner}:{spec}'

        return None

    def save(self, *args, **kwargs):
        self.active_unique_key = self._compute_active_unique_key()
        super().save(*args, **kwargs)

    def __str__(self):
        label = self.name or self.spec or self.template_type
        return f'{self.get_template_type_display()} {label}'


class SimcBackendBinary(models.Model):
    platform = models.CharField(max_length=32, default="linux64", help_text="平台标识，如 linux64/linuxarm64")
    simc_path = models.CharField(max_length=500, default="", help_text="SimC本地编译产物路径")
    current_version = models.CharField(max_length=128, default="", help_text="当前SimC版本号/构建标识")
    latest_version = models.CharField(max_length=128, default="", blank=True, help_text="检测到的源码上游版本/提交")
    auto_update = models.BooleanField(default=True, help_text="是否自动拉取并编译更新")
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
