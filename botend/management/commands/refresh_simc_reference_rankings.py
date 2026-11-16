"""Explicit backfill of the lightweight ranking index; never runs simulations."""
from django.core.management.base import BaseCommand
from botend.services.simc_reference_ranking_index import refresh_configs, refresh_talent_titles


class Command(BaseCommand):
    help = 'Refresh current reference ranking entries from existing successful simulations only.'

    def add_arguments(self, parser):
        parser.add_argument('--titles-only', action='store_true', help='仅回填榜单天赋名称，不扫描模拟历史。')
        parser.add_argument('--config', type=int, action='append', dest='config_ids',
                            help='Limit to these config IDs (repeatable); default: every config.')

    def handle(self, *args, **options):
        if options['titles_only']:
            changed = refresh_talent_titles(config_ids=options['config_ids'])
            self.stdout.write(self.style.SUCCESS(f'已更新 {changed} 条排名展示名称'))
            return
        stats = refresh_configs(options['config_ids'])
        self.stdout.write(self.style.SUCCESS(
            'configs={configs} changed={changed} invalidated={invalidated}'.format(**stats)))
