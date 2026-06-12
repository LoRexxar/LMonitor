from django.db import models
from django.utils import timezone


class SeasonMeta(models.Model):
    """赛季元数据"""
    season_key = models.CharField(max_length=30, unique=True, help_text="赛季标识，如 tww-s3")
    season_name = models.CharField(max_length=100, help_text="赛季名称")
    is_active = models.BooleanField(default=True, help_text="是否当前赛季")
    rio_season = models.CharField(max_length=30, null=True, blank=True, help_text="Raider.IO 赛季标识，如 season-tww-3")
    wcl_partition = models.IntegerField(default=3, help_text="WCL 分区号")
    mplus_zone_id = models.IntegerField(help_text="M+ 区域 ID")
    mplus_zone_name = models.CharField(max_length=100, null=True, blank=True, help_text="M+ 区域名称")
    raid_zone_id = models.IntegerField(help_text="团本区域 ID")
    raid_zone_name = models.CharField(max_length=100, null=True, blank=True, help_text="团本区域名称")
    mplus_encounters = models.JSONField(default=list, blank=True, help_text="M+ 副本列表 [{id, name, short}, ...]")
    raid_encounters = models.JSONField(default=list, blank=True, help_text="团本 Boss 列表 [{id, name, index}, ...]")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wow_spec_season_meta'
        app_label = 'botend'

    def __str__(self):
        return self.season_key


class PlayerSpecTopPlayer(models.Model):
    """人物榜（Raider.IO + Battle.net），每角色每专精每赛季 1 条，Top 20 专用"""
    season_id = models.IntegerField(help_text="赛季 ID（SeasonMeta.id）")
    region = models.CharField(max_length=10, help_text="区域 us/eu/kr/tw")
    realm = models.CharField(max_length=50, help_text="服务器")
    character_name = models.CharField(max_length=50, help_text="角色名")
    class_name = models.CharField(max_length=30, help_text="职业名，如 DeathKnight")
    spec_name = models.CharField(max_length=30, help_text="专精名，如 Frost")
    rank = models.IntegerField(null=True, blank=True, help_text="排名")
    score = models.FloatField(null=True, blank=True, help_text="M+ 分数")
    faction = models.CharField(max_length=10, null=True, blank=True, help_text="阵营")
    race = models.CharField(max_length=30, null=True, blank=True, help_text="种族")
    gender = models.CharField(max_length=10, null=True, blank=True, help_text="性别")
    guild_name = models.CharField(max_length=100, null=True, blank=True, help_text="公会名")
    realm_rank = models.IntegerField(null=True, blank=True, help_text="服务器排名")
    avatar_url = models.CharField(max_length=500, null=True, blank=True, help_text="头像 URL")
    profile_url = models.CharField(max_length=500, null=True, blank=True, help_text="Raider.IO 主页 URL")
    achievement_points = models.IntegerField(null=True, blank=True, help_text="成就点数")
    item_level = models.FloatField(null=True, blank=True, help_text="装等")
    gear_json = models.JSONField(default=list, blank=True, help_text="装备列表 [{slot, name, id, icon, itemLevel, bonusIDs, gems}, ...]")
    talents_json = models.JSONField(default=list, blank=True, help_text="天赋 [{talentID, points}, ...]")
    stats_json = models.JSONField(default=dict, blank=True, help_text="属性 {strength, crit:{rating,pct}, haste:{rating,pct}, ...}")
    stats_crawl_status = models.IntegerField(default=0, help_text="属性采集状态 0=待采集 1=已采集 -1=失败")
    last_updated = models.DateTimeField(null=True, blank=True, help_text="数据更新时间")

    class Meta:
        db_table = 'wow_spec_top_player'
        app_label = 'botend'
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
    season_id = models.IntegerField(help_text="赛季 ID（SeasonMeta.id）")
    dungeon_id = models.IntegerField(help_text="WCL encounter ID")
    dungeon_name = models.CharField(max_length=100, help_text="副本名称")
    class_name = models.CharField(max_length=30, help_text="职业名")
    spec_name = models.CharField(max_length=30, help_text="专精名")

    # 玩家信息
    character_name = models.CharField(max_length=50, help_text="角色名")
    realm = models.CharField(max_length=50, null=True, blank=True, help_text="服务器")
    region = models.CharField(max_length=10, null=True, blank=True, help_text="区域")

    # 实战数据
    dps = models.FloatField(help_text="原始 DPS")
    keystone_level = models.IntegerField(null=True, blank=True, help_text="钥石等级")
    clear_time = models.IntegerField(null=True, blank=True, help_text="通关时间(ms)")
    score = models.FloatField(null=True, blank=True, help_text="M+ 分数")
    medal = models.CharField(max_length=20, null=True, blank=True, help_text="奖牌 gold/silver/bronze")
    affixes = models.JSONField(default=list, blank=True, help_text="词缀列表 [9, 10, 147]")

    # 天赋（原始数据，来自 WCL）
    talents_json = models.JSONField(default=list, blank=True, help_text="天赋 [{talentID, points}, ...]")

    # 装备（原始数据，来自 WCL）
    gear_json = models.JSONField(default=list, blank=True, help_text="装备 [{name, id, icon, itemLevel, quality, bonusIDs, gems}, ...]")

    # 其他
    faction = models.IntegerField(null=True, blank=True, help_text="阵营 0=alliance 1=horde")
    guild_name = models.CharField(max_length=100, null=True, blank=True, help_text="公会名")
    report_code = models.CharField(max_length=50, null=True, blank=True, help_text="WCL report code")
    fight_id = models.IntegerField(null=True, blank=True, help_text="WCL fight ID")
    last_updated = models.DateTimeField(null=True, blank=True, help_text="数据更新时间")

    class Meta:
        db_table = 'wow_spec_dungeon_ranking'
        app_label = 'botend'
        indexes = [
            models.Index(fields=['season_id', 'dungeon_id', 'class_name', 'spec_name'], name='idx_dungeon_spec'),
            models.Index(fields=['class_name', 'spec_name', 'season_id', 'dps'], name='idx_dungeon_spec_dps'),
        ]

    def __str__(self):
        return f"{self.character_name} - {self.dungeon_name} ({self.spec_name}) {self.dps}"


class SpecRaidRanking(models.Model):
    """团本排名原始数据，每条=一个玩家在某 Boss 某专精的一次排名记录，来自 WCL API，Mythic only"""
    season_id = models.IntegerField(help_text="赛季 ID（SeasonMeta.id）")
    boss_id = models.IntegerField(help_text="WCL encounter ID")
    boss_name = models.CharField(max_length=100, help_text="Boss 名称")
    class_name = models.CharField(max_length=30, help_text="职业名")
    spec_name = models.CharField(max_length=30, help_text="专精名")

    # 玩家信息
    character_name = models.CharField(max_length=50, help_text="角色名")
    realm = models.CharField(max_length=50, null=True, blank=True, help_text="服务器")
    region = models.CharField(max_length=10, null=True, blank=True, help_text="区域")

    # 实战数据
    dps = models.FloatField(help_text="原始 DPS")
    kill_time = models.IntegerField(null=True, blank=True, help_text="击杀时间(ms)")

    # 天赋
    talents_json = models.JSONField(default=list, blank=True, help_text="天赋 [{talentID, points}, ...]")

    # 装备
    gear_json = models.JSONField(default=list, blank=True, help_text="装备 [{name, id, icon, itemLevel, quality, bonusIDs, gems}, ...]")

    # 其他
    faction = models.IntegerField(null=True, blank=True, help_text="阵营 0=alliance 1=horde")
    guild_name = models.CharField(max_length=100, null=True, blank=True, help_text="公会名")
    report_code = models.CharField(max_length=50, null=True, blank=True, help_text="WCL report code")
    fight_id = models.IntegerField(null=True, blank=True, help_text="WCL fight ID")
    last_updated = models.DateTimeField(null=True, blank=True, help_text="数据更新时间")

    class Meta:
        db_table = 'wow_spec_raid_ranking'
        app_label = 'botend'
        indexes = [
            models.Index(fields=['season_id', 'boss_id', 'class_name', 'spec_name'], name='idx_boss_spec'),
            models.Index(fields=['class_name', 'spec_name', 'season_id', 'dps'], name='idx_raid_spec_dps'),
        ]

    def __str__(self):
        return f"{self.character_name} - {self.boss_name} ({self.spec_name}) {self.dps}"
