"""有界采集 Raider.IO 公开统计，不访问未公开端点。"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from botend.services.monitor_task_execution import (
    MonitorTaskAdmissionBusy, monitor_task_execution,
)
from botend.services.rio_statistics import (Collector, MODULES, REGIONS, LEVELS, PERIODS,
                                          DEFAULT_LEVELS, DAILY_REQUEST_BUDGET, MAX_REQUEST_BUDGET)


class Command(BaseCommand):
    help = '采集魔兽世界数据观察；自动执行由每日凌晨长任务队列负责'

    def add_arguments(self, parser):
        parser.add_argument('--season', default='', help='只能使用数据库当前赛季，默认读取 SeasonMeta')
        parser.add_argument('--regions', nargs='+', choices=tuple(REGIONS), default=['cn', 'world'])
        parser.add_argument('--modules', nargs='+', choices=MODULES, default=list(MODULES))
        parser.add_argument('--dungeons', nargs='*', default=[], help='只采集指定副本的空间网格')
        parser.add_argument('--levels', nargs='+', choices=tuple(LEVELS), default=list(DEFAULT_LEVELS), help='仅采集10–12层、16层及以上、20层及以上，默认全部三种')
        parser.add_argument('--periods', nargs='+', choices=tuple(PERIODS), default=['season', 'last-reset'])
        parser.add_argument('--max-requests', type=int, default=DAILY_REQUEST_BUDGET, help=f'单轮请求上限，最多 {MAX_REQUEST_BUDGET}；包含元数据和地图瓦片')
        parser.add_argument('--force', action='store_true', help='忽略数据有效期；仍遵守限流冷却')
        parser.add_argument('--query-file', help='高级筛选 JSON 数组文件；参数与页面筛选一致')
        parser.add_argument('--only-queries', action='store_true', help='只处理订阅和高级筛选清单，不遍历基础预设')
        parser.add_argument('--allow-partial', action='store_true', help='允许预算用尽等未完成状态返回零退出码，进度仍保留')

    def handle(self, *args, **options):
        if not 4 <= options['max_requests'] <= MAX_REQUEST_BUDGET:
            raise CommandError(f'单轮请求上限须为 4—{MAX_REQUEST_BUDGET}')
        try:
            queries = []
            if options['query_file']:
                path = Path(options['query_file'])
                if path.stat().st_size > 256 * 1024:
                    raise ValueError('筛选清单文件过大')
                queries = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(queries, list):
                    raise ValueError('筛选清单必须是数组')
            with monitor_task_execution('RioStatisticsMonitor'):
                with Collector(max_requests=options['max_requests'], force=options['force']) as collector:
                    result = collector.run(season=options['season'], regions=options['regions'], modules=options['modules'],
                        dungeons=options['dungeons'], levels=options['levels'], periods=options['periods'],
                        queries=queries, only_queries=options['only_queries'])
        except MonitorTaskAdmissionBusy as exc:
            raise CommandError(str(exc)) from exc
        except Exception as exc:
            raise CommandError(f'采集未完成：{type(exc).__name__}') from exc
        self.stdout.write(json.dumps(result, ensure_ascii=False))
        if result.get('state') != 'complete' and not options['allow_partial']:
            raise CommandError('本轮尚未全部完成，进度及有效快照已保留；请检查输出状态')
