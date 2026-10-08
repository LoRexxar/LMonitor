"""手动检查或执行同一套装备天赋更新流程。"""
import json
from django.core.management.base import BaseCommand, CommandError
from botend.models import WowDataUpdateState
from botend.services.wow_data_update import sync_game_data, UpdateSource


class Command(BaseCommand):
    help = '检查装备天赋构建；默认只读，--apply 才准备并发布候选'

    def add_arguments(self, parser):
        parser.add_argument('--branch', choices=['retail', 'ptr', 'beta', 'all'], default='all')
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        branches = ('retail', 'ptr', 'beta') if options['branch'] == 'all' else (options['branch'],)
        try:
            if options['apply']:
                report = sync_game_data(branches=branches)
            else:
                source = UpdateSource()
                report = {branch: {'observed_build': source.discover(branch), 'current':
                    WowDataUpdateState.objects.filter(branch=branch).values(
                        'published_build', 'revision', 'status', 'error', 'projections_pending').first()}
                    for branch in branches}
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
        except Exception as exc:
            raise CommandError(str(exc)) from exc
