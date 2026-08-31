"""把当前职业配装器批次的图标直接同步到 OSS。"""

from django.core.management.base import BaseCommand, CommandError

from botend.models import SeasonMeta, WowItemSnapshot
from botend.services.gear_builder_icon_sync import GearBuilderIconSync, GearBuilderIconSyncError


class Command(BaseCommand):
    help = '流式同步职业配装器当前批次图标：检查 OSS、下载单张并立即上传。'

    def add_arguments(self, parser):
        parser.add_argument('--season-key', default='', help='赛季标识；默认使用当前激活赛季')
        parser.add_argument('--batch-key', default='', help='装备批次；默认使用赛季当前批次')
        parser.add_argument('--size', default='medium', choices=['tiny', 'small', 'medium'])
        parser.add_argument('--prefix', default='wow_icons_oss', help='OSS 对象前缀')
        parser.add_argument('--workers', type=int, default=4, help='有界并发数，默认 4，最大 12')
        parser.add_argument('--timeout', type=int, default=20, help='单张图标请求超时秒数')
        parser.add_argument('--force', action='store_true', help='不检查 OSS，强制覆盖上传')
        parser.add_argument('--no-proxy', action='store_true', help='忽略项目代理和系统环境代理（仅用于排障）')

    def handle(self, *args, **options):
        seasons = SeasonMeta.objects.all()
        if options['season_key']:
            season = seasons.filter(season_key=options['season_key']).first()
        else:
            season = seasons.filter(is_active=True).order_by('-updated_at', '-id').first()
        if not season:
            raise CommandError('找不到要同步图标的赛季。')
        batch_key = str(options['batch_key'] or season.gear_batch_key or '').strip()
        if not batch_key:
            raise CommandError(f'赛季 {season.season_key} 没有装备目录批次。')

        icons = WowItemSnapshot.objects.filter(
            gear_variants__season=season,
            gear_variants__batch_key=batch_key,
        ).exclude(icon='').order_by('icon').values_list('icon', flat=True).distinct().iterator(chunk_size=100)
        try:
            service = GearBuilderIconSync(
                size=options['size'], prefix=options['prefix'], workers=options['workers'],
                timeout=options['timeout'], force=options['force'], no_proxy=options['no_proxy'],
                progress=self.stdout.write,
            )
            report = service.sync(icons)
        except GearBuilderIconSyncError as exc:
            raise CommandError(str(exc)) from exc
        if not report['processed']:
            raise CommandError(f'批次 {batch_key} 没有可同步的图标。')
        if report['failed']:
            for error in report['errors']:
                self.stderr.write(error)
            raise CommandError(f'有 {report["failed"]} 个图标同步失败。')
        self.stdout.write(self.style.SUCCESS(f'批次 {batch_key} 的图标已全部同步到 OSS。'))
