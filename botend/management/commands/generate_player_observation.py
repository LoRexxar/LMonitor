"""Opt-in single-character pilot; dry-run never writes or invokes AI."""
import fcntl
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from botend.models import PlayerSpecTopPlayer, SeasonMeta
from botend.raiderio_identity import activity_identity_digest
from botend.services.player_raiderio_activity import player_identity


class Command(BaseCommand):
    help = '玩家观察试运行；默认只读，--apply 才生成，--refresh-sources 才补采来源。'

    def add_arguments(self, parser):
        parser.add_argument('--player-id', type=int, required=True)
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--refresh-sources', action='store_true')
        parser.add_argument('--regenerate', action='store_true', help='同输入重做AI分析；仍不抢占有效租约或绕过退避。')

    def handle(self, *args, **options):
        season = SeasonMeta.objects.filter(is_active=True).first()
        player = PlayerSpecTopPlayer.objects.filter(pk=options['player_id'],
            season_id=season.pk if season else -1, rank__gte=1, rank__lte=20).only(
                'id', 'season_id', 'rank', 'region', 'realm', 'character_name', 'profile_url',
                'class_name', 'spec_name').first()
        if not player:
            raise CommandError('只支持当前赛季Top20的明确玩家ID。')
        identity = player_identity(player)
        if not identity:
            raise CommandError('角色身份未能可靠解析。')
        plan = {'mode': 'dry-run', 'player_id': player.pk, 'season': season.rio_season,
                'identity': identity, 'max_runs': 18, 'max_characters': 40, 'ai_calls': 0,
                'refresh_sources': bool(options['refresh_sources'])}
        if not options['apply']:
            self.stdout.write(json.dumps(plan, ensure_ascii=False))
            return
        root = Path(settings.BASE_DIR) / 'var' / 'player-observation-locks'
        root.mkdir(parents=True, exist_ok=True)
        key = f'{season.pk}-{activity_identity_digest(**identity)}'
        with (root / f'{key}.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CommandError('同一角色的观察采集或分析正在执行。')
            try:
                sources = None
                if options['refresh_sources']:
                    from botend.services.player_observation_sources import refresh_observation_sources
                    sources = refresh_observation_sources(player, max_runs=18, max_characters=40,
                                                          force=True, refresh_recent=True)
                    self.stdout.write(json.dumps({'phase': 'sources', 'result': sources}, ensure_ascii=False))
                    if sources.get('status') not in ('ready', 'fresh', 'partial'):
                        raise CommandError('来源补采已停止；本轮不继续调用AI。')
                from botend.services.player_observation import generate_observation
                result = generate_observation(player, force=options['regenerate'])
                self.stdout.write(json.dumps({'mode': 'applied', 'player_id': player.pk,
                    'source_refresh': sources, 'analysis': result}, ensure_ascii=False, default=str))
                if result.get('status') not in ('ready', 'fresh'):
                    raise CommandError('观察分析未完成；请核对上述状态，不会将空结果当作成功。')
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
