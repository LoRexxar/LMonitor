"""只读核对每日团本战斗统计的调度门槛与中间文件。"""
import json
from django.core.management.base import BaseCommand
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection
from django.utils import timezone
from botend.models import MonitorTask, MonitorTaskLease, SeasonMeta, WclCollectionCycle
from botend.services.raid_burst_snapshot import private_root, needs_raid_burst_refresh


class Command(BaseCommand):
    help = '只读诊断团本战斗统计：任务开关、执行租约、采集周期和私有快照，不触发采集'
    requires_system_checks = []

    def handle(self, *args, **options):
        now = timezone.now()
        result = {'检查时间': timezone.localtime(now), '提示': [], '任务': [], '最近周期': []}
        tables = set(connection.introspection.table_names())
        models = (MonitorTask, MonitorTaskLease, SeasonMeta, WclCollectionCycle)
        missing = [model._meta.db_table for model in models if model._meta.db_table not in tables]
        if missing:
            result['缺失数据库表'] = missing
            result['提示'].append('数据库迁移不完整，调度器无法正常判断采集周期。请确认当前连接的是线上业务库。')
        else:
            from botend.services.wcl_collection_cycle import active_cycle
            for name in ('SpecDetailRankingMonitor', 'SpecDetailAggregationMonitor'):
                task = MonitorTask.objects.filter(name=name).first()
                if not task:
                    result['提示'].append(f'{name} 未注册')
                    continue
                lease = MonitorTaskLease.objects.filter(task=task, expires_at__gt=now).first()
                result['任务'].append({'名称': name, '启用': task.is_active,
                                       '上次领取时间': task.last_scan_time,
                                       '正在执行的租约截止': lease.expires_at if lease else None})
                if not task.is_active:
                    result['提示'].append(f'{name} 已停用')
            season = SeasonMeta.objects.filter(is_active=True).first()
            if not season:
                result['提示'].append('没有活跃赛季')
            else:
                result['赛季编号'] = season.pk
                for cycle in WclCollectionCycle.objects.filter(season=season).order_by('-slot')[:3]:
                    result['最近周期'].append({'编号': cycle.pk, '计划时间': cycle.slot, '阶段': cycle.phase,
                                               '采集完成': cycle.collected_at, '发布完成': cycle.published_at,
                                               '最近尝试': cycle.last_attempt_at, '下次重试': cycle.next_retry_at,
                                               '已完成排名范围数': len(cycle.completed_scopes),
                                               '存在错误记录': bool(cycle.error)})
                pending = active_cycle(season.pk)
                if pending:
                    result['提示'].append(f'当前周期 {pending.pk} 阶段：{pending.phase}')
                    if pending.phase in ('rankings', 'combatants'):
                        result['提示'].append('采集尚未完成，团本聚合和战斗统计均被前置条件阻止。')
                    if pending.next_retry_at and pending.next_retry_at > now:
                        result['提示'].append('等待记录中的下次重试时间；当前不会重跑。')
                else:
                    published = WclCollectionCycle.objects.filter(season=season, phase='published').order_by('-slot').first()
                    if published:
                        result['需要补建战斗统计'] = needs_raid_burst_refresh(published)
                        if result['需要补建战斗统计']:
                            result['提示'].append('已完成周期缺少本版战斗统计，原团本聚合任务应自动补建。')
                    else:
                        result['提示'].append('没有采集周期；需先运行正常排名采集任务。')
        root = private_root()
        result['私有快照目录'] = str(root)
        result['已发布专精索引数'] = len(list(root.glob('*/*/*/index.json')))
        result['提示'].append('上次领取时间不等于任务成功；本诊断不证明后台工作进程正在运行。')
        self.stdout.write(json.dumps(result, cls=DjangoJSONEncoder, ensure_ascii=False, indent=2))
