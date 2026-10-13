"""预热当前冒险手册的掉落投影。"""
from django.core.management.base import BaseCommand, CommandError
from botend.portal.adventure_journal import current_release, instance_source
from botend.services.gear_builder import active_season
from botend.services.journal_loot_snapshot import coordinate, read_loot_projection, refresh_journal_loot_snapshots
from botend.services.journal_snapshot import loot_version


class Command(BaseCommand):
    help = '预热当前手册的副本掉落，支持限定副本'

    def add_arguments(self, parser):
        parser.add_argument('--instance', type=int, help='副本手册 ID，不填则预热全部副本')
        parser.add_argument('--force', action='store_true', help='重新生成已有投影')

    def handle(self, *args, **options):
        release = current_release()
        if not release:
            raise CommandError('冒险手册尚未同步')
        season = active_season()
        instances = release.instances.all()
        if options['instance'] is not None:
            instances = instances.filter(journal_id=options['instance'])
        targets = []
        for instance in instances:
            source = instance_source(release, instance.journal_id, season=season)
            bosses = list(instance.encounters.all())
            for difficulty in instance.payload['difficulty_ids']:
                version = loot_version(bosses, difficulty)
                read_loot_projection(release.id, instance.journal_id, difficulty, source,
                                     journal_version=version)
                targets.append(coordinate(release.id, instance.journal_id, difficulty, source, journal_version=version))
        if not targets:
            raise CommandError('未找到可预热的副本掉落分片')
        try:
            built = refresh_journal_loot_snapshots(batch_size=len(targets), force=options['force'],
                                                  targets=targets, strict=True)
        except (RuntimeError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f'本次验证 {len(targets)} 个目标分片，已刷新 {len(built)} 个分片')
