# -*- coding: utf-8 -*-
"""Current-season Mythic+ DPS leaderboards built from local WCL run facts."""

import json
import os
import tempfile
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

from django.conf import settings
from django.utils import timezone

from botend.constants.wow import (
    CLASS_CN,
    CLASS_COLOR,
    SPEC_CN,
    SPEC_ICON,
    SPEC_ROLE,
    canonical_class_spec,
)
from botend.models import SeasonMeta, SpecDungeonRanking
from botend.services.spec_stats_service import _lookup_dungeon_cn


SNAPSHOT_FILENAME = 'mplus-dps-rankings.json'
SAMPLE_CAP_PER_SPEC_DUNGEON = 100
ROW_FIELDS = (
    'dungeon_id', 'class_name', 'spec_name', 'dps', 'keystone_level',
    'region', 'realm', 'character_name', 'last_updated',
)
ROW_FIELD_INDEX = {field: index for index, field in enumerate(ROW_FIELDS)}


def _iso(value):
    if not value:
        return ''
    if isinstance(value, str):
        return value
    return value.isoformat()


def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (position - lower) * (ordered[upper] - ordered[lower])


def select_representative_rows(rows, max_samples=SAMPLE_CAP_PER_SPEC_DUNGEON):
    """Reuse the established M+ overview sampling rule without loading gear/talents."""
    by_level = defaultdict(list)
    for row in rows:
        by_level[row.get('keystone_level') or 0].append(row)

    selected = []
    seen_players = set()
    for level in sorted(by_level, reverse=True):
        level_rows = sorted(by_level[level], key=lambda item: item.get('dps') or 0, reverse=True)
        median_dps = _percentile([item.get('dps') or 0 for item in level_rows], 50)
        for row in level_rows:
            if len(selected) >= max_samples:
                return selected
            dps = row.get('dps') or 0
            if median_dps is not None and dps < median_dps:
                continue
            player_key = (
                str(row.get('region') or '').strip().lower(),
                str(row.get('realm') or '').strip().lower(),
                str(row.get('character_name') or '').strip().lower(),
            )
            if player_key in seen_players:
                continue
            seen_players.add(player_key)
            selected.append(row)
    return selected


def _row_value(row, field):
    if isinstance(row, dict):
        return row.get(field)
    return row[ROW_FIELD_INDEX[field]]


def _select_representative_values(rows, max_samples=SAMPLE_CAP_PER_SPEC_DUNGEON):
    """Compact equivalent of select_representative_rows for production streaming."""
    by_level = defaultdict(list)
    for level, dps, player_key in rows:
        by_level[level].append((dps, player_key))

    selected_values = []
    seen_players = set()
    for level in sorted(by_level, reverse=True):
        level_rows = sorted(by_level[level], key=lambda item: item[0], reverse=True)
        median_dps = _percentile([item[0] for item in level_rows], 50)
        for dps, player_key in level_rows:
            if len(selected_values) >= max_samples:
                return selected_values
            if median_dps is not None and dps < median_dps:
                continue
            if player_key in seen_players:
                continue
            seen_players.add(player_key)
            selected_values.append(dps)
    return selected_values


def _season_value(season, key, default=None):
    if isinstance(season, dict):
        return season.get(key, default)
    return getattr(season, key, default)


def _season_encounter_ids(season):
    encounters = list(_season_value(season, 'mplus_encounters', []) or [])
    try:
        encounter_ids = [int(item['id']) for item in encounters]
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError('当前赛季大秘境副本配置无效') from exc
    if len(encounter_ids) != 8 or len(set(encounter_ids)) != 8:
        raise RuntimeError('当前赛季必须配置 8 个唯一的大秘境副本')
    return encounters, encounter_ids


def _identity_payload(class_name, spec_name):
    return {
        'class_name': class_name,
        'class_name_cn': CLASS_CN.get(class_name, class_name),
        'spec_name': spec_name,
        'spec_name_cn': SPEC_CN.get(spec_name, spec_name),
        'class_color': CLASS_COLOR.get(class_name, '#64748b'),
        'icon_url': SPEC_ICON.get((class_name, spec_name), ''),
        'detail_url': '/portal/spec/{}/{}/dungeons/'.format(
            quote(class_name, safe=''), quote(spec_name, safe='')
        ),
    }


def _rank(items):
    items.sort(key=lambda item: (-item['average_dps'], item['class_name'], item['spec_name']))
    for index, item in enumerate(items, start=1):
        item['rank'] = index
    return items


def build_rankings_payload_from_rows(season, rows, generated_at=None):
    """Pure aggregation entry point used by the hourly publisher and focused tests."""
    encounters, encounter_ids = _season_encounter_ids(season)
    allowed_dungeons = set(encounter_ids)
    grouped = defaultdict(list)
    source_updated_at = None

    for row in rows:
        try:
            dungeon_id = int(_row_value(row, 'dungeon_id'))
        except (TypeError, ValueError):
            continue
        if dungeon_id not in allowed_dungeons:
            continue
        identity = canonical_class_spec(
            _row_value(row, 'class_name'), _row_value(row, 'spec_name')
        )
        if not identity or SPEC_ROLE.get(identity) != 'dps':
            continue
        dps = _row_value(row, 'dps')
        if dps is None:
            continue
        player_key = (
            str(_row_value(row, 'region') or '').strip().lower(),
            str(_row_value(row, 'realm') or '').strip().lower(),
            str(_row_value(row, 'character_name') or '').strip().lower(),
        )
        grouped[(identity[0], identity[1], dungeon_id)].append((
            _row_value(row, 'keystone_level') or 0,
            float(dps),
            player_key,
        ))
        updated_at = _row_value(row, 'last_updated')
        if updated_at and (source_updated_at is None or updated_at > source_updated_at):
            source_updated_at = updated_at

    dungeon_stats = {}
    per_dungeon = {dungeon_id: [] for dungeon_id in encounter_ids}
    for (class_name, spec_name, dungeon_id), group_rows in grouped.items():
        values = _select_representative_values(group_rows)
        if not values:
            continue
        stat = {
            **_identity_payload(class_name, spec_name),
            'dungeon_id': dungeon_id,
            'sample_size': len(values),
            'lower_dps': min(values),
            'average_dps': sum(values) / len(values),
            'highest_dps': max(values),
            'dungeon_count': 1,
            'required_dungeon_count': len(encounter_ids),
        }
        dungeon_stats[(class_name, spec_name, dungeon_id)] = stat
        per_dungeon[dungeon_id].append(dict(stat))

    identities = sorted({(class_name, spec_name) for class_name, spec_name, _ in dungeon_stats})
    overall = []
    for class_name, spec_name in identities:
        stats = [
            dungeon_stats.get((class_name, spec_name, dungeon_id))
            for dungeon_id in encounter_ids
        ]
        if not encounter_ids or any(stat is None for stat in stats):
            continue
        sample_size = sum(stat['sample_size'] for stat in stats)
        if sample_size <= 0:
            continue

        def weighted(field):
            return sum(stat[field] * stat['sample_size'] for stat in stats) / sample_size

        overall.append({
            **_identity_payload(class_name, spec_name),
            'dungeon_id': None,
            'sample_size': sample_size,
            'lower_dps': weighted('lower_dps'),
            'average_dps': weighted('average_dps'),
            'highest_dps': weighted('highest_dps'),
            'dungeon_count': len(stats),
            'required_dungeon_count': len(encounter_ids),
        })

    rankings = {'overall': _rank(overall)}
    scopes = [{'key': 'overall', 'label': '总计', 'dungeon_id': None}]
    for encounter in encounters:
        dungeon_id = int(encounter['id'])
        scope_key = f'dungeon:{dungeon_id}'
        label = encounter.get('short') or _lookup_dungeon_cn(encounter.get('name'))
        full_name = _lookup_dungeon_cn(encounter.get('name'))
        scopes.append({
            'key': scope_key,
            'label': label,
            'name': full_name,
            'dungeon_id': dungeon_id,
        })
        rankings[scope_key] = _rank(per_dungeon[dungeon_id])

    generated_at = generated_at or timezone.now()
    season_id = _season_value(season, 'id')
    season_key = _season_value(season, 'season_key', '')
    season_name = _season_value(season, 'season_name', '')
    return {
        'season': {'id': season_id, 'key': season_key, 'name': season_name},
        'generated_at': _iso(generated_at),
        'source_updated_at': _iso(source_updated_at),
        'scopes': scopes,
        'rankings': rankings,
        'method': {
            'role': 'dps',
            'sample_cap_per_spec_dungeon': SAMPLE_CAP_PER_SPEC_DUNGEON,
            'sample_rule': '按钥石层数从高到低；每层保留不低于该层中位数的记录；角色去重',
            'dungeon_metrics': {'lower': 'min', 'average': 'mean', 'highest': 'max'},
            'overall_weight': '各副本最终样本数',
            'overall_requires_all_dungeons': True,
            'required_dungeon_count': len(encounter_ids),
        },
    }


def active_season():
    return SeasonMeta.objects.filter(is_active=True).order_by('-updated_at', '-id').first()


def iter_season_rows(season):
    _, dungeon_ids = _season_encounter_ids(season)
    queryset = SpecDungeonRanking.objects.filter(
        season_id=season.id,
        dungeon_id__in=dungeon_ids,
        dps__isnull=False,
    ).values_list(*ROW_FIELDS).order_by()
    return queryset.iterator(chunk_size=2000)


def build_current_mplus_dps_rankings_payload(season=None):
    season = season or active_season()
    if not season:
        raise RuntimeError('没有可用的当前赛季')
    return build_rankings_payload_from_rows(season, iter_season_rows(season))


def snapshot_path(season_id):
    media_root = Path(getattr(settings, 'MEDIA_ROOT', '') or 'media')
    return media_root / 'aggregated' / str(season_id) / SNAPSHOT_FILENAME


def atomic_write_payload(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.tmp-mplus-dps-', suffix='.json', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def publish_current_mplus_dps_rankings(season=None):
    season = season or active_season()
    if not season:
        raise RuntimeError('没有可用的当前赛季')
    payload = build_current_mplus_dps_rankings_payload(season=season)
    atomic_write_payload(snapshot_path(season.id), payload)
    return payload


def load_published_mplus_dps_rankings(season=None):
    season = season or active_season()
    if not season:
        return None
    path = snapshot_path(season.id)
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or (payload.get('season') or {}).get('id') != season.id:
        return None
    return payload


def get_current_mplus_dps_rankings_payload():
    season = active_season()
    if not season:
        raise RuntimeError('没有可用的当前赛季')
    payload = load_published_mplus_dps_rankings(season)
    if payload is None:
        raise RuntimeError('大秘境 DPS 榜单快照尚未生成')
    return payload
