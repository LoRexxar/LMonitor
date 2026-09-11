"""冒险手册的不可变发布快照；同步失败时继续使用上一份完整数据。"""
from django.db import models
from django.utils import timezone


class JournalRelease(models.Model):
    build = models.CharField(max_length=64)
    locale = models.CharField(max_length=8, default='zhCN')
    status = models.CharField(max_length=20, default='fetching')
    manifest = models.JSONField(default=dict)
    report = models.JSONField(default=dict)
    error = models.TextField(blank=True, default='')
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True)

    class Meta:
        ordering = ['-id']


class JournalState(models.Model):
    key = models.CharField(max_length=32, primary_key=True, default='wow-zhCN')
    active_release = models.ForeignKey(JournalRelease, null=True, on_delete=models.PROTECT)
    sync_token = models.CharField(max_length=36, blank=True, default='')
    sync_until = models.DateTimeField(null=True)


class JournalInstance(models.Model):
    release = models.ForeignKey(JournalRelease, on_delete=models.CASCADE, related_name='instances')
    journal_id = models.PositiveIntegerField()
    name = models.CharField(max_length=255)
    kind = models.CharField(max_length=16)
    expansion = models.IntegerField(default=0)
    payload = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['release', 'journal_id'], name='journal_instance_release_id')]


class JournalEncounter(models.Model):
    instance = models.ForeignKey(JournalInstance, on_delete=models.CASCADE, related_name='encounters')
    journal_id = models.PositiveIntegerField()
    name = models.CharField(max_length=255)
    order = models.IntegerField(default=0)
    payload = models.JSONField(default=dict)

    class Meta:
        ordering = ['order', 'journal_id']
        constraints = [models.UniqueConstraint(fields=['instance', 'journal_id'], name='journal_encounter_instance_id')]
