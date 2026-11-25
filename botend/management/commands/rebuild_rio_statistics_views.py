"""从已采集的统计缓存生成页面中间文件，不请求上游。"""
import json
from django.core.management.base import BaseCommand, CommandError
from botend.services.rio_statistics_views import refresh_views
from botend.services.rio_statistics_archive import recover_existing
from botend.services.rio_statistics_storage import StorageError
from botend.services.rio_statistics_scope import ScopeUnavailable


class Command(BaseCommand):
    help = '重建数据观察页面展示文件；不访问外网，不改变业务赛季'

    def add_arguments(self, parser):
        parser.add_argument('--reuse-published', action='store_true', help='复用已提交且未变更的正文，快速升级或刷新轻量目录；不进行全量正文复核')
        parser.add_argument('--register-archive', nargs=3, metavar=('SCOPE', 'SEASON', 'BUILD'),
                            help='匹配同赛季已有历史正文与源记录，默认只预览')
        parser.add_argument('--apply-archive', action='store_true', help='提交历史展示索引，不重写源缓存')

    def handle(self, *args, **options):
        try:
            if options['apply_archive'] and not options['register_archive']:
                raise CommandError('--apply-archive 需要 --register-archive')
            if options['register_archive']:
                from botend.services.rio_statistics import active_scope, public_scope
                token, season, build = options['register_archive']
                source = dict(public_scope(active_scope()), token=token, rio_season=season,
                              game_build=build, version_build=build)
                result = recover_existing(source, apply=options['apply_archive'])
                self.stdout.write(json.dumps(result, ensure_ascii=False))
                return
            result = refresh_views(keys=set(), verify=False) if options['reuse_published'] else refresh_views()
        except (StorageError, ScopeUnavailable, OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, ensure_ascii=False))
        if result['state'] != 'complete':
            raise CommandError('部分统计正文未就绪，已保留原有展示数据')
