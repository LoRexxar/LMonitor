# -*- coding: utf-8 -*-

import sys

from django.core.management.base import BaseCommand, CommandError

from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
from botend.models import PlayerSpecTopPlayer, SeasonMeta


class Command(BaseCommand):
    help = '验证 WoW 专精详情人物榜与 Raider.IO 世界榜是否一致'

    def add_arguments(self, parser):
        parser.add_argument('--class-name', required=True, help='职业英文名，如 Monk')
        parser.add_argument('--spec-name', required=True, help='专精英文名，如 Windwalker')
        parser.add_argument('--limit', type=int, default=20, help='验证条数，默认 20')

    def handle(self, *args, **options):
        class_name = options['class_name']
        spec_name = options['spec_name']
        limit = options['limit']

        season = SeasonMeta.objects.filter(is_active=True).first()
        if not season:
            raise CommandError('没有活跃赛季，无法验证')
        if not season.rio_season:
            raise CommandError('活跃赛季缺少 rio_season，无法验证')

        db_rows = list(PlayerSpecTopPlayer.objects.filter(
            season_id=season.id,
            class_name=class_name,
            spec_name=spec_name,
        ).order_by('rank').values('rank', 'character_name', 'region', 'score')[:limit])

        if not db_rows:
            raise CommandError(f'数据库中没有 {class_name}/{spec_name} 的人物榜数据')

        base = SpecDetailBase(None, None)
        payload = base.fetch_raiderio_top(
            class_name, spec_name, season.rio_season, region='world', limit=limit, page=0
        )
        rankings = payload.get('rankings', {}) if isinstance(payload, dict) else {}
        ranked_characters = rankings.get('rankedCharacters', []) if isinstance(rankings, dict) else []

        if not ranked_characters:
            raise CommandError('Raider.IO 世界榜返回为空，无法验证')

        rio_rows = []
        for i, item in enumerate(ranked_characters[:limit], start=1):
            char = item.get('character', {}) or {}
            region = (char.get('region', {}) or {}).get('short_name', '') or (char.get('region', {}) or {}).get('slug', '')
            rio_rows.append({
                'rank': i,
                'character_name': char.get('name', ''),
                'region': region.upper() if region else '',
                'score': float(item.get('score', 0) or 0),
            })

        mismatches = []
        for idx, rio_row in enumerate(rio_rows):
            if idx >= len(db_rows):
                mismatches.append({
                    'type': 'missing_db_row',
                    'expected': rio_row,
                })
                continue

            db_row = db_rows[idx]
            db_score = float(db_row.get('score', 0) or 0)
            if (
                int(db_row.get('rank') or 0) != rio_row['rank'] or
                (db_row.get('character_name') or '') != rio_row['character_name'] or
                (db_row.get('region') or '').upper() != rio_row['region'] or
                abs(db_score - rio_row['score']) > 0.01
            ):
                mismatches.append({
                    'position': idx + 1,
                    'db': {
                        'rank': db_row.get('rank'),
                        'character_name': db_row.get('character_name'),
                        'region': (db_row.get('region') or '').upper(),
                        'score': db_score,
                    },
                    'expected': rio_row,
                })

        if mismatches:
            self.stderr.write(self.style.ERROR(
                f'FAIL: {class_name}/{spec_name} 与 Raider.IO 世界榜不一致，共 {len(mismatches)} 处差异'
            ))
            for mismatch in mismatches[:10]:
                self.stderr.write(str(mismatch))
            sys.exit(1)

        self.stdout.write(self.style.SUCCESS(
            f'PASS: {class_name}/{spec_name} 前 {len(rio_rows)} 名与 Raider.IO 世界榜一致'
        ))
