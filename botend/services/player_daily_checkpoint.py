"""人物日更阶段检查点；已完成工作可恢复，失败工作保留待办。"""
from datetime import timedelta
import hashlib
import json
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from botend.models import MonitorTask, MonitorTaskLease, MonitorTaskLeaseLost, PlayerDailyCheckpoint, SeasonMeta, WowTalentVersion

LOCAL_TZ = ZoneInfo('Asia/Shanghai')


def scope_token(season, plan):
    versions = list(WowTalentVersion.objects.filter(Q(is_active=True) | Q(is_default_player_tree=True)
        | Q(is_default_stats=True)).order_by('pk').values_list(
        'pk', 'current_build', 'updated_at'))
    data = [season.pk, season.rio_season, season.game_build, season.gear_batch_key,
            season.mplus_zone_id, season.raid_zone_id, versions, plan, 'player-daily-v1']
    return hashlib.sha256(json.dumps(data, default=str, sort_keys=True).encode()).hexdigest()


class DailyProgress:
    def __init__(self, task, season, plan):
        self.task, self.season, self.plan = task, season, plan
        self.day = timezone.localtime(timezone.now(), LOCAL_TZ).date()
        self.scope = scope_token(season, plan)
        self.persistent = isinstance(task, MonitorTask) and task.pk is not None
        self.row = None
        self.stages, self.complete = {}, False
        if self.persistent:
            with transaction.atomic():
                self._lock_owner()
                self.row, _ = PlayerDailyCheckpoint.objects.get_or_create(task=task, defaults={
                    'day': self.day, 'scope': self.scope, 'generation': uuid4().hex})
                self.row = PlayerDailyCheckpoint.objects.select_for_update().get(pk=self.row.pk)
                if self.row.day != self.day or self.row.scope != self.scope:
                    self.row.day, self.row.scope = self.day, self.scope
                    self.row.generation, self.row.stages, self.row.complete = uuid4().hex, {}, False
                    self.row.save()
                self.stages, self.complete = dict(self.row.stages), self.row.complete
        self._save()

    def _lock_owner(self):
        MonitorTask.objects.select_for_update().get(pk=self.task.pk)
        leases = MonitorTaskLease.objects.select_for_update().filter(task_id=self.task.pk,
            expires_at__gt=timezone.now())
        owner = getattr(self.task, '_monitor_task_lease_owner', None)
        if (owner and not leases.filter(owner=owner).exists()) or (not owner and leases.exists()):
            raise MonitorTaskLeaseLost('人物检查点的任务租约已失效')

    def check_scope(self):
        current = SeasonMeta.objects.filter(pk=self.season.pk, is_active=True).first()
        if current is None or scope_token(current, self.plan) != self.scope:
            raise MonitorTaskLeaseLost('人物采集范围已变化，停止当前批次，等待新范围重试')

    def done(self, key):
        return key in self.stages

    def mark(self, key, value=True):
        self.check_scope()
        self.stages[key] = value
        self._save()

    def pending_profiles(self, key):
        value = self.stages.get(key + ':pending')
        if not isinstance(value, dict) or value.get('version') != 1:
            return None
        from botend.services.player_daily_resume import reference, required_after
        try:
            refs = value['remaining']
            if not isinstance(refs, list) or not refs:
                return None
            for ref in refs:
                if not isinstance(ref, dict) or reference(ref) is None:
                    return None
                required_after(ref.get('required_after'))
            if type(value['players']) is not int or value['players'] < 0:
                return None
            if not isinstance(value['identities'], list) or not all(
                    isinstance(identity, str) for identity in value['identities']):
                return None
        except (KeyError, TypeError, ValueError):
            return None
        return value

    def record_profiles(self, key, players, identities, *, previous=None, waiting=()):
        """在资料写入事务完成后记录结果，未成功写入的身份不会被认证。"""
        from botend.services.player_daily_resume import reference
        previous = previous or {}
        remaining = list(waiting)
        for player in players:
            if player.get('_profile_refresh_failed'):
                ref = reference(player)
                if ref is None:
                    # 身份本身不完整时仍需重读榜单，不能冻结不可修复的引用。
                    return
                remaining.append(ref)
        successes = sum(not p.get('_profile_refresh_failed') for p in players)
        value = {'players': previous.get('players', 0) + successes,
                 'identities': sorted(set(previous.get('identities', [])) | set(identities))}
        self.check_scope()
        self.stages.pop(key + ':pending', None)
        if remaining:
            self.stages[key + ':pending'] = {**value, 'version': 1, 'remaining': remaining}
        else:
            self.stages[key] = value
        self._save()

    def finish(self, complete, *, failed_bindings=()):
        self.check_scope()
        self.complete = bool(complete)
        self.stages['bindings:checked'] = True
        self.stages.pop('bindings:pending', None)
        if failed_bindings:
            self.stages['bindings:pending'] = sorted(set(failed_bindings))
        self._save()

    def _retry_at(self):
        from botend.services.player_daily_resume import deadlines
        now = timezone.now()
        fallback = now + timedelta(minutes=30)
        refs, unknown = [], not self.stages.get('bindings:checked', False)
        for cls, specs in self.plan.get('classes', {}).items():
            for spec in specs:
                key = f'{cls}/{spec}:profiles'
                if self.done(key):
                    continue
                pending = self.pending_profiles(key)
                if pending:
                    refs.extend(pending['remaining'])
                else:
                    unknown = True
        bindings = self.stages.get('bindings:pending', [])
        clocks = deadlines(self.season, refs, binding_digests=bindings) if refs or bindings else []
        if unknown or not clocks:
            clocks.append(fallback)
        return max(fallback, min(clocks))

    def _save(self):
        summary = {'kind': 'player_daily', 'day': self.day.isoformat(), 'scope': self.scope,
                   'state': 'complete' if self.complete else 'partial', 'stages': len(self.stages),
                   'retry_at': self._retry_at().isoformat()}
        players = sum(value.get('players', 0) for key, value in self.stages.items()
                      if (key.endswith(':profiles') or key.endswith(':profiles:pending'))
                      and isinstance(value, dict))
        summary['label'] = f'{self.season.season_key}@players={players}@'
        flag = json.dumps(summary, ensure_ascii=False, separators=(',', ':'))
        if self.persistent:
            with transaction.atomic():
                self._lock_owner()
                row = PlayerDailyCheckpoint.objects.select_for_update().get(pk=self.row.pk)
                if row.generation != self.row.generation:
                    raise MonitorTaskLeaseLost('人物检查点已被新批次替换')
                row.stages, row.complete = self.stages, self.complete
                row.save(update_fields=['stages', 'complete', 'updated_at'])
                self.task.flag = flag
                self.task.save(update_fields=['flag'])
        else:
            self.task.flag = flag


def resume_due_at(task, local_now):
    try:
        flag = json.loads(task.flag or '{}')
        if flag.get('kind') != 'player_daily' or flag.get('day') != local_now.date().isoformat():
            return None
        if flag.get('state') == 'complete':
            from botend.controller.plugins.portal.SpecDetailPlayerMonitor import SpecDetailPlayerMonitor, CLASS_SPEC_MAP
            season = SeasonMeta.objects.filter(is_active=True).first()
            plan = {'classes': CLASS_SPEC_MAP, 'ranking_limit': SpecDetailPlayerMonitor.PLAYER_RANKING_LIMIT,
                    'source_contract': 'top20-full-wcl-statistics-separate-v1'}
            if season is None or flag.get('scope') == scope_token(season, plan):
                return None
        due = task.last_scan_time + timedelta(minutes=30)
        from django.utils.dateparse import parse_datetime
        retry = parse_datetime(flag.get('retry_at') or '')
        if retry and timezone.is_aware(retry):
            due = max(due, retry)
        return due if due <= local_now else None
    except (ValueError, TypeError, AttributeError):
        return None
