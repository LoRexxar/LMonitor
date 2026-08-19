"""Cached aggregate-file read models for the public specialization overview."""
import json
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.urls import reverse

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

    @staticmethod
    def _summary_fields(row, fields):
        """Copy a small public read-model projection without leaking detail payloads."""
        if not isinstance(row, dict):
            return {}
        return {field: row[field] for field in fields if field in row}

    @classmethod
    def _dungeon_summary(cls, dungeon):
        summary = cls._summary_fields(
            dungeon, ('dungeon_id', 'dungeon_name', 'name', 'short_name', 'sample_size'),
        )
        for field, metric_fields in (
            ('dps', ('median', 'avg')),
            ('keystone', ('avg',)),
            ('clear_time', ('median_fmt',)),
        ):
            metric = cls._summary_fields(dungeon.get(field), metric_fields) if isinstance(dungeon, dict) else {}
            if metric:
                summary[field] = metric
        return summary

    @classmethod
    def _raid_summary(cls, zone):
        summary = cls._summary_fields(zone, ('zone_id', 'zone_name', 'zone_cn', 'name'))
        bosses = zone.get('bosses') if isinstance(zone, dict) else []
        bosses = bosses if isinstance(bosses, list) else []
        summary['bosses'] = []
        for boss in bosses:
            boss_summary = cls._summary_fields(boss, ('boss_id', 'boss_name', 'name', 'sample_size'))
            for field, metric_fields in (
                ('dps', ('median', 'avg')),
                ('kill_time', ('median_fmt',)),
            ):
                metric = cls._summary_fields(boss.get(field), metric_fields) if isinstance(boss, dict) else {}
                if metric:
                    boss_summary[field] = metric
            if boss_summary:
                summary['bosses'].append(boss_summary)
        return summary

    @classmethod
    def mythic_plus(cls, class_name, spec_name):
        data, mtime = cls._aggregate('mythic-plus', class_name, spec_name)
        dungeons = data.get('dungeons') or []
        dungeons = dungeons if isinstance(dungeons, list) else []
        return {'source': cls.SOURCES['mythic-plus'],
                'updated_at': data.get('updated_at') or cls._latest_timestamp(dungeons) or mtime,
                'dungeons': [cls._dungeon_summary(dungeon) for dungeon in dungeons if isinstance(dungeon, dict)]}

    @classmethod
    def raid(cls, class_name, spec_name):
        data, mtime = cls._aggregate('raid', class_name, spec_name)
        zone_groups = data.get('zone_groups') or []
        zone_groups = zone_groups if isinstance(zone_groups, list) else []
        return {'source': cls.SOURCES['raid'],
                'updated_at': data.get('updated_at') or cls._latest_timestamp(zone_groups) or mtime,
                'zone_groups': [cls._raid_summary(zone) for zone in zone_groups if isinstance(zone, dict)]}

    @staticmethod
    def discover_simc_dimensions(class_name, spec_name):
        spec_key = f'{class_name}_{spec_name}'.lower()
        panel_specs = list(SimcBenchmarkSpec.objects.filter(
            panel__is_active=True, panel__is_public=True, is_enabled=True, spec_key=spec_key,
        ).select_related('panel').prefetch_related('profiles__profile').order_by(
            'panel_id', 'display_order', 'id',
        ))
        # Trinket panels also contain every specialization, but they are item
        # comparisons rather than the overview's specialization benchmark.
        def panel_priority(panel_spec):
            value = f'{panel_spec.panel.slug} {panel_spec.panel.name}'.lower()
            if 'default-scenarios' in value or '大秘境天赋模拟' in value:
                return 0
            if '纯单体' in value or 'single' in value:
                return 1
            if 'trinket' in value or '饰品' in value:
                return 10
            return 5

        panel_spec = min(panel_specs, key=panel_priority) if panel_specs else None
        if panel_spec is None:
            return None
        scenarios = list(SimcBenchmarkScenario.objects.filter(
            panel=panel_spec.panel, is_enabled=True,
        ).order_by('display_order', 'id'))
        if not scenarios:
            return None
        profiles = [profile for profile in panel_spec.profiles.all() if profile.is_enabled]
        if not profiles:
            return None
        return {
            'panel': panel_spec.panel.slug,
            'spec': panel_spec.spec_key,
            'scenario': scenarios[0].key,
            'profile': benchmark_profile_key(
                profiles[0].profile_id, profiles[0].talent_string_id,
            ) if profiles else '',
            'scenario_keys': ','.join(scenario.key for scenario in scenarios),
            'profile_keys': ','.join(
                benchmark_profile_key(profile.profile_id, profile.talent_string_id)
                for profile in profiles
            ),
            'scenarios': [
                {'key': scenario.key, 'label': scenario.name,
                 'detail': scenario.simulation_params or {}}
                for scenario in scenarios
            ],
            'profiles': [
                {'key': benchmark_profile_key(profile.profile_id, profile.talent_string_id),
                 'label': profile.label,
                 'profile_name': profile.profile.name}
                for profile in profiles
            ],
        }
