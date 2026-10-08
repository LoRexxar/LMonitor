"""预热当前冒险手册的掉落投影。"""
from django.core.management.base import BaseCommand
from botend.portal.adventure_journal import current_release, instance_source
from botend.services.gear_builder import active_season
from botend.services.journal_loot_snapshot import read_loot_projection, refresh_journal_loot_snapshots


class Command(BaseCommand):
    help = '预热当前手册的副本掉落，支持限定副本'

    def add_arguments(self, parser):
        parser.add_argument('--instance', type=int, help='副本手册 ID，不填则预热全部副本')
        parser.add_argument('--force', action='store_true', help='重新生成已有投影')

    def handle(self, *args, **options):
        release = current_release()
        if not release:
            self.stdout.write('冒险手册尚未同步')
            return
        season = active_season()
        instances = release.instances.all()
        if options['instance']:
            instances = instances.filter(journal_id=options['instance'])
        count = 0
        for instance in instances:
            source = instance_source(release, instance.journal_id, season=season)
            for difficulty in instance.payload['difficulty_ids']:
                read_loot_projection(release.id, instance.journal_id, difficulty, source)
                count += 1
        built = refresh_journal_loot_snapshots(batch_size=max(count, 1), force=options['force'])
        self.stdout.write(f'本次登记 {count} 个分片，已刷新 {len(built)} 个分片')
