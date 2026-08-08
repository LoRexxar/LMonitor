"""Cached aggregate-file read models for the public specialization overview."""
import json
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.urls import reverse

from botend.models import SimcBenchmarkScenario, SimcBenchmarkSpec
from botend.services.spec_stats_service import SpecStatsService


class SpecOverviewService:
    """Serve independent modules without doing public request-time aggregation."""

    SOURCES = {
        'players': 'Raider.IO',
        'mythic-plus': 'Raider.IO',
        'raid': 'Warcraft Logs',
    }
    FILES = {'players': 'leaderboard.json', 'mythic-plus': 'dungeon.json', 'raid': 'raid.json'}
    CACHE_SECONDS = {'players': 60, 'mythic-plus': 300, 'raid': 300}

    @staticmethod
    def _latest_timestamp(value):
        timestamps = []

        def visit(item):
            if isinstance(item, dict):
                for key, nested in item.items():
                    if key in {'updated_at', 'last_updated'} and nested:
                        timestamps.append(nested)
                    else:
                        visit(nested)
            elif isinstance(item, (list, tuple)):
                for nested in item:
                    visit(nested)

        visit(value)
        return max(timestamps, key=lambda item: str(item)) if timestamps else None

    @classmethod
    def _aggregate(cls, module, class_name, spec_name):
        season = SpecStatsService.get_active_season()
        if season is None:
            return {}, None
        media_root = Path(getattr(settings, 'MEDIA_ROOT', '') or 'media')
        path = media_root / 'aggregated' / str(season.id) / class_name / spec_name / cls.FILES[module]
        key = f'spec-overview:{module}:{season.id}:{class_name}:{spec_name}'
        cached = cache.get(key)
        if cached is not None:
            return cached
        try:
            with path.open(encoding='utf-8') as stream:
                payload = json.load(stream)
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
        except (OSError, ValueError, TypeError):
            payload, mtime = {}, None
        result = (payload if isinstance(payload, dict) else {}, mtime)
        cache.set(key, result, cls.CACHE_SECONDS[module])
        return result

    @classmethod
    def players(cls, class_name, spec_name):
        data, mtime = cls._aggregate('players', class_name, spec_name)
        players = list(data.get('players') or [])
        public_players = []
        for player in players:
            row = dict(player)
            player_id = row.get('id')
            if player_id is not None:
                row['detail_url'] = reverse('spec_detail_player_detail', kwargs={
                    'class_name': class_name, 'spec_name': spec_name, 'player_id': player_id,
                })
            public_players.append(row)
        return {
            'source': cls.SOURCES['players'],
            'updated_at': data.get('updated_at') or cls._latest_timestamp(players) or mtime,
            'players': public_players,
            'total': data.get('total', len(public_players)),
            'page': data.get('page', 1),
            'pages': data.get('pages', 1 if public_players else 0),
        }

    @classmethod
    def mythic_plus(cls, class_name, spec_name):
        data, mtime = cls._aggregate('mythic-plus', class_name, spec_name)
        dungeons = data.get('dungeons') or []
        return {'source': cls.SOURCES['mythic-plus'],
                'updated_at': data.get('updated_at') or cls._latest_timestamp(dungeons) or mtime,
                'dungeons': dungeons}

    @classmethod
    def raid(cls, class_name, spec_name):
        data, mtime = cls._aggregate('raid', class_name, spec_name)
        zone_groups = data.get('zone_groups') or []
        return {'source': cls.SOURCES['raid'],
                'updated_at': data.get('updated_at') or cls._latest_timestamp(zone_groups) or mtime,
                'zone_groups': zone_groups}

    @staticmethod
    def discover_simc_dimensions(class_name, spec_name):
        spec_key = f'{class_name}_{spec_name}'.lower()
        panel_spec = SimcBenchmarkSpec.objects.filter(
            panel__is_active=True, panel__is_public=True, is_enabled=True, spec_key=spec_key,
        ).select_related('panel').order_by('panel__name', 'panel_id', 'display_order', 'id').first()
        if panel_spec is None:
            return None
        scenario = SimcBenchmarkScenario.objects.filter(
            panel=panel_spec.panel, is_enabled=True,
        ).order_by('display_order', 'id').first()
        if scenario is None:
            return None
        return {'panel': panel_spec.panel.slug, 'spec': panel_spec.spec_key,
                'scenario': scenario.key}
