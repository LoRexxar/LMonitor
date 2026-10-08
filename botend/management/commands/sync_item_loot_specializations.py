"""单独核对并补齐已有装备和冒险手册掉落的拾取专精。"""
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from botend.services.item_loot_specializations import prepare_specializations, publish_specializations
from botend.services.journal_source import WagoJournalSource


class Command(BaseCommand):
    help = '按分支、物品 ID 核对拾取专精；加 --apply 原位更新现有资料'

    def add_arguments(self, parser):
        parser.add_argument('--branch', required=True, choices=('retail', 'ptr', 'beta'))
        parser.add_argument('--build', required=True)
        parser.add_argument('--directory', default=str(Path(settings.BASE_DIR) / '.cache' / 'loot-specializations'))
        parser.add_argument('--offline', action='store_true')
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        try:
            source = WagoJournalSource(options['build'], options['directory'], offline=options['offline'],
                refresh=not options['offline'], max_workers=2, progress=self.stderr.write)
            resolved = prepare_specializations(options['branch'], source)
            changed = publish_specializations(options['branch'], resolved) if options['apply'] else {}
            self.stdout.write(json.dumps({'branch': options['branch'], 'build': source.build,
                'resolved': len(resolved), 'changed': changed, 'items': resolved}, ensure_ascii=False, indent=2))
        except Exception as exc:
            raise CommandError(str(exc)) from exc
