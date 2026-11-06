"""导入已有授权来源的完成记录聚合响应。"""
import json
from django.core.management.base import BaseCommand, CommandError
from botend.services.rio_statistics import current_catalog
from botend.services.rio_statistics_runs import import_file
from botend.services.rio_statistics_storage import StorageError
from botend.services.rio_statistics_scope import ScopeUnavailable


class Command(BaseCommand):
    help = '导入完成记录及限时率统计；不会请求未公开的上游统计端点'

    def add_arguments(self, parser):
        parser.add_argument('source', help='原始聚合 JSON 响应文件')

    def handle(self, *args, **options):
        meta = current_catalog()
        if meta.get('scope_state') != 'ready':
            raise CommandError('当前赛季元数据尚未就绪，请先完成每日元数据采集')
        try:
            changed_keys = set()
            changed = import_file(options['source'], meta, changed_keys=changed_keys)
            from botend.services.rio_statistics_views import refresh_views
            refresh_views(keys=changed_keys, verify=False)
        except (ValueError, TypeError, KeyError, OSError, StorageError, ScopeUnavailable) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps({'已更新': changed}, ensure_ascii=False))
