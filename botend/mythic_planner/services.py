import base64
import json
from dataclasses import dataclass

from django.db.models import Prefetch

from botend.models import (
    MythicDungeon,
    MythicDungeonAbility,
    MythicDungeonDataVersion,
    MythicDungeonEnemy,
    MythicDungeonFloor,
    MythicDungeonPoi,
    MythicDungeonRoute,
    MythicDungeonSelectionGroup,
    MythicDungeonSelectionMembership,
    MythicDungeonSpawn,
    MythicPlannerConfig,
)
from botend.mythic_planner.spell_tooltips import spell_snapshot_provenance


SHARE_PREFIX = '!LMDT1!'
MAX_ROUTE_BYTES = 2 * 1024 * 1024
MAX_PULLS = 100
MAX_ANNOTATIONS = 500


@dataclass(frozen=True)
class RouteValidationResult:
    payload: dict
    spawn_uids: set[str]


def display_name(obj):
    return getattr(obj, 'name_zh', '') or getattr(obj, 'name', '') or getattr(obj, 'key', '')


def selection_groups(metadata, *, include_dungeon_indexes=False):
    rows = (metadata or {}).get(
        'dungeon_selection_groups' if include_dungeon_indexes else 'selection_groups',
        [],
    )
    if not isinstance(rows, list):
        return []
    result = []
    for row in rows:
        if not isinstance(row, dict) or not row.get('key'):
            continue
        item = {
            'key': str(row['key']),
            'name': str(row.get('name') or row['key']),
            'name_zh': str(row.get('name_zh') or row.get('name') or row['key']),
            'order': int(row.get('order') or 0),
        }
        if include_dungeon_indexes:
            raw_indexes = row.get('dungeon_indexes', [])
            if not isinstance(raw_indexes, list):
                raw_indexes = []
            item['dungeon_indexes'] = [
                int(value)
                for value in raw_indexes
                if isinstance(value, int)
            ]
        else:
            item['dungeon_order'] = int(row.get('dungeon_order') or 0)
        result.append(item)
    return sorted(result, key=lambda item: (item['order'], item['name_zh']))


def active_data_version():
    return MythicDungeonDataVersion.objects.filter(is_active=True).order_by('-imported_at', '-id').first()


def planner_config_dict():
    config = MythicPlannerConfig.objects.filter(key='default').first()
    if not config:
        return {
            'key': 'default',
            'default_dungeon_key': '',
            'default_dungeon_level': 10,
            'min_dungeon_level': 2,
            'max_dungeon_level': 35,
            'group_selection_default': True,
            'live_sync_enabled': True,
            'allow_public_route_share': True,
            'settings': {},
        }
    return {
        'key': config.key,
        'default_dungeon_key': config.default_dungeon_key,
        'default_dungeon_level': config.default_dungeon_level,
        'min_dungeon_level': config.min_dungeon_level,
        'max_dungeon_level': config.max_dungeon_level,
        'group_selection_default': config.group_selection_default,
        'live_sync_enabled': config.live_sync_enabled,
        'allow_public_route_share': config.allow_public_route_share,
        'settings': config.settings or {},
    }


def serialize_catalog():
    version = active_data_version()
    config = planner_config_dict()
    if not version:
        return {'version': None, 'dungeons': [], 'config': config}
    dungeons = list(
        version.dungeons.filter(is_active=True)
        .prefetch_related(
            Prefetch('floors', queryset=MythicDungeonFloor.objects.filter(is_active=True)),
        )
        .order_by('order', 'name_zh', 'name')
    )
    groups = list(
        MythicDungeonSelectionGroup.objects.filter(
            data_version=version,
            is_active=True,
        )
        .prefetch_related(
            Prefetch(
                'memberships',
                queryset=MythicDungeonSelectionMembership.objects.filter(
                    is_active=True,
                    dungeon__is_active=True,
                )
                .select_related('dungeon')
                .order_by('order', 'dungeon__order'),
            ),
        )
        .order_by('order', 'name_zh', 'name')
    )
    dungeon_group_map = {}
    if groups:
        catalog_groups = []
        for group in groups:
            memberships = list(group.memberships.all())
            catalog_groups.append({
                'key': group.key,
                'name': group.name or group.key,
                'name_zh': group.display_name,
                'order': group.order,
                'dungeon_indexes': [
                    membership.dungeon.external_index
                    for membership in memberships
                    if membership.dungeon.external_index is not None
                ],
            })
            for membership in memberships:
                dungeon_group_map.setdefault(
                    membership.dungeon_id,
                    [],
                ).append({
                    'key': group.key,
                    'name': group.name or group.key,
                    'name_zh': group.display_name,
                    'order': group.order,
                    'dungeon_order': membership.order,
                })
    else:
        catalog_groups = selection_groups(
            version.metadata,
            include_dungeon_indexes=True,
        )
    return {
        'version': {
            'key': version.key,
            'label': version.label,
            'game_version': version.game_version,
            'season': version.season,
            'schema_version': version.schema_version,
            'source_name': version.source_name,
            'imported_at': version.imported_at.isoformat() if version.imported_at else None,
        },
        'selection_groups': catalog_groups,
        'dungeons': [
            {
                'id': dungeon.id,
                'key': dungeon.key,
                'name': dungeon.name,
                'name_zh': dungeon.name_zh,
                'display_name': display_name(dungeon),
                'short_name': dungeon.short_name,
                'map_id': dungeon.map_id,
                'total_enemy_forces': dungeon.total_enemy_forces,
                'selection_groups': (
                    sorted(
                        dungeon_group_map.get(dungeon.id, []),
                        key=lambda item: (
                            item['order'],
                            item['dungeon_order'],
                            item['name_zh'],
                        ),
                    )
                    if groups
                    else selection_groups(dungeon.metadata)
                ),
                'floors': [
                    {
                        'id': floor.id,
                        'key': floor.key,
                        'floor_index': floor.floor_index,
                        'name': floor.name,
                        'name_zh': floor.name_zh,
                        'display_name': display_name(floor),
                    }
                    for floor in dungeon.floors.all()
                ],
            }
            for dungeon in dungeons
        ],
        'config': config,
    }


def get_active_dungeon(dungeon_key):
    version = active_data_version()
    if not version:
        return None
    floors = MythicDungeonFloor.objects.filter(is_active=True).prefetch_related(
        Prefetch('pois', queryset=MythicDungeonPoi.objects.filter(is_active=True)),
    )
    abilities = MythicDungeonAbility.objects.filter(is_active=True).select_related(
        'spell_record',
    )
    spawns = MythicDungeonSpawn.objects.filter(
        is_active=True,
        floor__is_active=True,
    ).select_related('floor')
    enemies = MythicDungeonEnemy.objects.filter(is_active=True).prefetch_related(
        Prefetch('abilities', queryset=abilities),
        Prefetch('spawns', queryset=spawns),
    )
    return (
        version.dungeons.filter(key=dungeon_key, is_active=True)
        .prefetch_related(
            Prefetch('floors', queryset=floors),
            Prefetch('enemies', queryset=enemies),
        )
        .first()
    )


def serialize_dungeon(dungeon):
    floors = list(dungeon.floors.all())
    enemies = list(dungeon.enemies.all())
    return {
        'id': dungeon.id,
        'key': dungeon.key,
        'name': dungeon.name,
        'name_zh': dungeon.name_zh,
        'display_name': display_name(dungeon),
        'short_name': dungeon.short_name,
        'map_id': dungeon.map_id,
        'total_enemy_forces': dungeon.total_enemy_forces,
        'metadata': dungeon.metadata or {},
        'data_version': {
            'key': dungeon.data_version.key,
            'label': dungeon.data_version.label,
            'game_version': dungeon.data_version.game_version,
            'season': dungeon.data_version.season,
        },
        'floors': [
            {
                'id': floor.id,
                'key': floor.key,
                'floor_index': floor.floor_index,
                'name': floor.name,
                'name_zh': floor.name_zh,
                'display_name': display_name(floor),
                'background_url': floor.background_url,
                'background_color': floor.background_color,
                'map_width': floor.map_width,
                'map_height': floor.map_height,
                'metadata': floor.metadata or {},
                'pois': [
                    {
                        'id': poi.id,
                        'key': poi.key,
                        'type': poi.poi_type,
                        'x': poi.x,
                        'y': poi.y,
                        'label': poi.label,
                        'icon_url': poi.icon_url,
                        'target_floor_key': poi.target_floor_key,
                        'metadata': poi.metadata or {},
                    }
                    for poi in floor.pois.all()
                ],
            }
            for floor in floors
        ],
        'enemies': [
            {
                'id': enemy.id,
                'key': enemy.key,
                'npc_id': enemy.npc_id,
                'name': enemy.name,
                'name_zh': enemy.name_zh,
                'display_name': display_name(enemy),
                'enemy_forces': enemy.enemy_forces,
                'base_health': enemy.base_health,
                'level': enemy.level,
                'creature_type': enemy.creature_type,
                'icon_url': enemy.icon_url,
                'marker_color': enemy.marker_color,
                'is_boss': enemy.is_boss,
                'traits': enemy.traits or {},
                'metadata': enemy.metadata or {},
                'abilities': [
                    serialize_ability(ability)
                    for ability in enemy.abilities.all()
                ],
                'spawns': [
                    {
                        'id': spawn.id,
                        'uid': f'{enemy.key}:{spawn.key}',
                        'key': spawn.key,
                        'floor_id': spawn.floor_id,
                        'floor_key': spawn.floor.key,
                        'x': spawn.x,
                        'y': spawn.y,
                        'group_key': spawn.group_key,
                        'scale': spawn.scale,
                        'patrol': spawn.patrol or [],
                        'metadata': spawn.metadata or {},
                    }
                    for spawn in enemy.spawns.all()
                ],
            }
            for enemy in enemies
        ],
        'config': planner_config_dict(),
    }


def _is_spell_placeholder(value, spell_id):
    text = str(value or '').strip()
    return not text or text in {f'Spell #{spell_id}', f'技能 #{spell_id}'}


def serialize_ability(ability):
    spell = ability.spell_record
    metadata = dict(ability.metadata or {})
    override_fields = set(metadata.get('manual_override_fields') or [])

    def overridden(field):
        return field in override_fields and bool(str(getattr(ability, field) or '').strip())

    name = (
        ability.name if overridden('name') else (spell.name if spell else '')
    )
    name_zh = (
        ability.name_zh if overridden('name_zh') else (spell.name_zh if spell else '')
    )
    name = (
        name
        or (
            ability.name
            if not _is_spell_placeholder(ability.name, ability.spell_id)
            else ''
        )
        or f'Spell #{ability.spell_id}'
    )
    name_zh = (
        name_zh
        or (
            ability.name_zh
            if not _is_spell_placeholder(ability.name_zh, ability.spell_id)
            else ''
        )
        or f'技能 #{ability.spell_id}'
    )
    description = (
        (ability.description if overridden('description') else '')
        or (spell.description if spell else '')
        or (spell.aura_description if spell else '')
        or ability.description
    )
    description_zh = (
        (ability.description_zh if overridden('description_zh') else '')
        or (spell.description_zh if spell else '')
        or (spell.aura_description_zh if spell else '')
        or ability.description_zh
    )
    if spell:
        metadata['spell_snapshot'] = {
            'id': spell.id,
            'source_branch': spell.source_branch,
            'source_locale': spell.source_locale,
            'snapshot_build': spell.snapshot_build,
            'icon_file_data_id': spell.icon_file_data_id,
            **spell_snapshot_provenance(spell.metadata),
        }
    return {
        'id': ability.id,
        'spell_id': ability.spell_id,
        'spell_record_id': ability.spell_record_id,
        'name': name,
        'name_zh': name_zh,
        'display_name': name_zh or name,
        'description': description,
        'description_zh': description_zh,
        'icon_url': (
            ability.icon_url
            if overridden('icon_url')
            else ((spell.icon_url if spell else '') or ability.icon_url)
        ),
        'interruptible': ability.interruptible,
        'dispel_type': ability.dispel_type,
        'danger_level': ability.danger_level,
        'metadata': metadata,
    }


def _route_json(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def encode_share_code(payload):
    raw = _route_json(payload).encode('utf-8')
    if len(raw) > MAX_ROUTE_BYTES:
        raise ValueError('路线数据超过 2 MB，无法导出。')
    encoded = base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')
    return f'{SHARE_PREFIX}{encoded}'


def decode_share_code(code):
    text = str(code or '').strip()
    if not text.startswith(SHARE_PREFIX):
        raise ValueError('分享字符串格式不正确。')
    encoded = text[len(SHARE_PREFIX):]
    try:
        padding = '=' * (-len(encoded) % 4)
        raw = base64.urlsafe_b64decode((encoded + padding).encode('ascii'))
        if len(raw) > MAX_ROUTE_BYTES:
            raise ValueError('路线数据超过 2 MB，无法导入。')
        payload = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith('路线数据超过'):
            raise
        raise ValueError('分享字符串无法解码。') from exc
    except Exception as exc:
        raise ValueError('分享字符串无法解码。') from exc
    return payload


def validate_route_payload(payload, dungeon, *, check_spawns=True):
    if not isinstance(payload, dict):
        raise ValueError('路线内容必须是 JSON 对象。')
    raw_size = len(_route_json(payload).encode('utf-8'))
    if raw_size > MAX_ROUTE_BYTES:
        raise ValueError('路线数据超过 2 MB。')
    version = payload.get('version', 1)
    if version != 1:
        raise ValueError('仅支持版本 1 的路线数据。')
    dungeon_key = str(payload.get('dungeon_key') or '')
    if dungeon_key and dungeon_key != dungeon.key:
        raise ValueError('路线所属地下城与当前地下城不一致。')
    pulls = payload.get('pulls', [])
    annotations = payload.get('annotations', [])
    if not isinstance(pulls, list) or len(pulls) > MAX_PULLS:
        raise ValueError('拉怪组必须是列表且不能超过 100 组。')
    if not isinstance(annotations, list) or len(annotations) > MAX_ANNOTATIONS:
        raise ValueError('地图标注必须是列表且不能超过 500 个。')

    spawn_uids = set()
    for index, pull in enumerate(pulls, start=1):
        if not isinstance(pull, dict):
            raise ValueError(f'第 {index} 个拉怪组格式不正确。')
        pull_spawns = pull.get('spawn_uids', [])
        if not isinstance(pull_spawns, list) or len(pull_spawns) > 1000:
            raise ValueError(f'第 {index} 个拉怪组的怪物列表不正确。')
        for uid in pull_spawns:
            uid = str(uid)
            if uid in spawn_uids:
                raise ValueError(f'怪物刷新点 {uid} 被重复加入多个拉怪组。')
            spawn_uids.add(uid)

    if check_spawns and spawn_uids:
        valid_uids = {
            f'{enemy_key}:{spawn_key}'
            for enemy_key, spawn_key in MythicDungeonSpawn.objects.filter(
                enemy__dungeon=dungeon,
                enemy__is_active=True,
                floor__is_active=True,
                is_active=True,
            ).values_list('enemy__key', 'key')
        }
        unknown = sorted(spawn_uids - valid_uids)
        if unknown:
            preview = '、'.join(unknown[:5])
            raise ValueError(f'路线包含不存在的怪物刷新点：{preview}')

    normalized = dict(payload)
    normalized['version'] = 1
    normalized['dungeon_key'] = dungeon.key
    normalized.setdefault('data_version_key', dungeon.data_version.key)
    normalized.setdefault('pulls', pulls)
    normalized.setdefault('annotations', annotations)
    return RouteValidationResult(payload=normalized, spawn_uids=spawn_uids)


def serialize_route(route):
    return {
        'id': route.id,
        'share_id': str(route.share_id),
        'name': route.name,
        'dungeon_key': route.dungeon.key,
        'dungeon_name': display_name(route.dungeon),
        'dungeon_level': route.dungeon_level,
        'route_data': route.route_data or {},
        'share_code': route.share_code or encode_share_code(route.route_data or {}),
        'revision': route.revision,
        'is_public': route.is_public,
        'created_at': route.created_at.isoformat() if route.created_at else None,
        'updated_at': route.updated_at.isoformat() if route.updated_at else None,
    }


def owned_route_queryset(user):
    if not getattr(user, 'is_authenticated', False):
        return MythicDungeonRoute.objects.none()
    return MythicDungeonRoute.objects.filter(
        owner_user_id=user.id,
        is_active=True,
    ).select_related('dungeon', 'dungeon__data_version')
