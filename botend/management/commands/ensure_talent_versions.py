# -*- coding: utf-8 -*-

from django.core.management.base import BaseCommand

from botend.models import WowTalentVersion
from botend.wow.talents.default_versions import ensure_default_talent_versions


class Command(BaseCommand):
    help = '确保内置 retail/PTR 天赋元数据版本存在'

    def handle(self, *args, **options):
        ensured = ensure_default_talent_versions(WowTalentVersion)
        for version, created, changed in ensured:
            action = 'created' if created else ('updated' if changed else 'unchanged')
            self.stdout.write(
                f'{action}: {version.key} ({version.label or version.branch}) '
                f'active={version.is_active} simulator={version.is_default_simulator} '
                f'player={version.is_default_player_tree} stats={version.is_default_stats}'
            )
        self.stdout.write(self.style.SUCCESS(f'已确认 {len(ensured)} 个天赋版本'))
