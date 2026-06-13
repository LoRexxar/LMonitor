# -*- coding: utf-8 -*-

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from botend.constants.wow import CLASS_SPEC_MAP
from botend.controller.plugins.portal.SpecDetailPlayerMonitor import SpecDetailPlayerMonitor
from botend.models import PlayerSpecTopPlayer, SeasonMeta


class Command(BaseCommand):
    help = '按指定职业/专精刷新 WoW 专精人物榜，便于本地验证'

    def add_arguments(self, parser):
        parser.add_argument('--class-name', required=True, help='职业英文名，如 Monk')
        parser.add_argument('--spec-name', required=True, help='专精英文名，如 Windwalker')

    def handle(self, *args, **options):
        class_name = options['class_name']
        spec_name = options['spec_name']

        if class_name not in CLASS_SPEC_MAP:
            raise CommandError(f'未知职业: {class_name}')
        if spec_name not in CLASS_SPEC_MAP[class_name]:
            raise CommandError(f'{class_name} 下不存在专精: {spec_name}')

        season = SeasonMeta.objects.filter(is_active=True).first()
        if not season:
            raise CommandError('没有活跃赛季，无法刷新人物榜')
        if not season.rio_season:
            raise CommandError('活跃赛季缺少 rio_season，无法刷新人物榜')

        dummy_task = type('DummyTask', (), {'flag': '', 'save': lambda self: None})()
        monitor = SpecDetailPlayerMonitor(None, dummy_task)
        players = monitor._fetch_top_players(class_name, spec_name, season.rio_season)

        if not players:
            raise CommandError(f'未获取到 {class_name}/{spec_name} 的人物榜数据')

        with transaction.atomic():
            PlayerSpecTopPlayer.objects.filter(
                season_id=season.id,
                class_name=class_name,
                spec_name=spec_name,
            ).delete()

            for i, player in enumerate(players[:20], start=1):
                PlayerSpecTopPlayer.objects.create(
                    season_id=season.id,
                    region=player.get('region', ''),
                    realm=player.get('realm', ''),
                    character_name=player.get('name', ''),
                    class_name=class_name,
                    spec_name=spec_name,
                    rank=i,
                    score=player.get('score'),
                    faction=player.get('faction'),
                    race=player.get('race'),
                    gender=player.get('gender'),
                    guild_name=player.get('guild_name'),
                    realm_rank=player.get('realm_rank'),
                    avatar_url=player.get('avatar_url'),
                    profile_url=player.get('profile_url'),
                    achievement_points=player.get('achievement_points'),
                    item_level=player.get('item_level'),
                    gear_json=player.get('gear', []),
                    talents_json=player.get('talents', []),
                    stats_json={},
                    stats_crawl_status=0,
                    last_updated=timezone.now(),
                )

        self.stdout.write(self.style.SUCCESS(
            f'已刷新 {class_name}/{spec_name} 人物榜，共 {min(len(players), 20)} 条'
        ))
