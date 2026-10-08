"""复用定时任务采集器，或将已有数据库记录发布为文件。"""
from django.core.management.base import BaseCommand, CommandError
from botend.services.mythicstats_snapshot import collect_snapshots, publish_database_snapshots


class Command(BaseCommand):
    help = '统一刷新 Mythicstats 榜单文件，包含全部副本、单副本与最近三个周次'

    def add_arguments(self, parser):
        parser.add_argument('--season', default='', help='校验来源当前赛季；默认自动识别')
        parser.add_argument('--from-database', action='store_true', help='离线发布已有数据库历史，不访问上游')

    def handle(self, *args, **options):
        try:
            if options['from_database']:
                self.stdout.write(f'已从数据库发布 {publish_database_snapshots()} 个分片')
                return
            result = collect_snapshots(season_hint=options['season'])
            if result['busy']:
                raise CommandError('已有采集任务运行，请稍后重试')
            self.stdout.write(f"赛季 {result['season']}：已发布 {result['built']} 个分片，失败 {result['failed']} 个")
            if result['failed'] or not result['latest_published']:
                raise CommandError('部分数据未更新，已保留对应旧版，请检查后台发布状态并重试')
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError(f'采集发布失败，保留旧版：{exc}') from exc
