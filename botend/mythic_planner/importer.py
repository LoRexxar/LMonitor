import hashlib
import json
import re
from collections import Counter

from django.db import transaction
from django.utils import timezone

from botend.models import (
    MythicDungeon,
    MythicDungeonAbility,
    MythicDungeonDataVersion,
    MythicDungeonEnemy,
    MythicDungeonFloor,
    MythicDungeonPoi,
    MythicDungeonSelectionGroup,
    MythicDungeonSelectionMembership,
    MythicDungeonSpell,
    MythicDungeonSpawn,
    MythicPlannerConfig,
)


KEY_RE = re.compile(r'^[a-z0-9][a-z0-9_-]*$')


def _is_spell_placeholder(value, spell_id):
    text = str(value or '').strip()
    return not text or text in {f'Spell #{spell_id}', f'技能 #{spell_id}'}


def _required_text(data, field, context, *, max_length):
    value = str(data.get(field) or '').strip()
    if not value:
        raise ValueError(f'{context} 缺少必填字段 {field}。')
    if len(value) > max_length:
        raise ValueError(f'{context} 的 {field} 超过 {max_length} 个字符。')
    return value


def _optional_text(data, field, *, max_length, default=''):
    value = str(data.get(field, default) or '').strip()
    if len(value) > max_length:
        raise ValueError(f'字段 {field} 超过 {max_length} 个字符。')
    return value


def _key(data, context):
    value = _required_text(data, 'key', context, max_length=120)
    if not KEY_RE.match(value):
        raise ValueError(f'{context} 的 key 只能包含小写字母、数字、下划线和连字符。')
    return value


def _dict(value, field):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f'字段 {field} 必须是 JSON 对象。')
    return value


def _list(value, field):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f'字段 {field} 必须是 JSON 数组。')
    return value


def _int(value, field, *, minimum=0, default=0, nullable=False):
    if value is None and nullable:
        return None
    if value in (None, ''):
        value = default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'字段 {field} 必须是整数。') from exc
    if number < minimum:
        raise ValueError(f'字段 {field} 不能小于 {minimum}。')
    return number


def _float(value, field, *, minimum=None, maximum=None, default=0.0):
    if value in (None, ''):
        value = default
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'字段 {field} 必须是数字。') from exc
    if minimum is not None and number < minimum:
        raise ValueError(f'字段 {field} 不能小于 {minimum}。')
    if maximum is not None and number > maximum:
        raise ValueError(f'字段 {field} 不能大于 {maximum}。')
    return number


def _bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {'1', 'true', 'yes', 'on'}
    return bool(value)


def _mark(counter, created):
    counter['created' if created else 'updated'] += 1


def _source_hash(payload, source_bytes=None):
    raw = source_bytes
    if raw is None:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _sync_selection_groups(
    *,
    version,
    payload,
    version_metadata,
    dungeon_items,
    replace,
    stats,
):
    has_explicit_groups = 'selection_groups' in payload
    has_legacy_groups = 'dungeon_selection_groups' in version_metadata
    if not has_explicit_groups and not has_legacy_groups:
        return 0

    group_items = _list(
        payload.get('selection_groups')
        if has_explicit_groups
        else version_metadata.get('dungeon_selection_groups'),
        'selection_groups',
    )
    dungeons = list(MythicDungeon.objects.filter(data_version=version))
    dungeon_by_key = {row.key: row for row in dungeons}
    dungeon_by_index = {
        row.external_index: row
        for row in dungeons
        if row.external_index is not None
    }
    membership_hints = {}
    for dungeon_data in dungeon_items:
        if not isinstance(dungeon_data, dict):
            continue
        dungeon_key = str(dungeon_data.get('key') or '')
        metadata = dungeon_data.get('metadata')
        rows = (
            metadata.get('selection_groups', [])
            if isinstance(metadata, dict)
            else []
        )
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or not row.get('key'):
                continue
            membership_hints.setdefault(str(row['key']), []).append((
                _int(
                    row.get('dungeon_order'),
                    'dungeon_order',
                    default=0,
                ),
                dungeon_key,
            ))

    seen_group_keys = []
    for group_index, group_data in enumerate(group_items):
        if not isinstance(group_data, dict):
            raise ValueError(
                f'第 {group_index + 1} 个赛季分类必须是 JSON 对象。'
            )
        group_key = _key(group_data, f'第 {group_index + 1} 个赛季分类')
        seen_group_keys.append(group_key)
        group_metadata = _dict(
            group_data.get('metadata'),
            f'{group_key}.metadata',
        )
        group_metadata = {**group_metadata, 'source': 'payload'}
        group, created = MythicDungeonSelectionGroup.objects.update_or_create(
            data_version=version,
            key=group_key,
            defaults={
                'name': _required_text(
                    group_data,
                    'name',
                    f'赛季分类 {group_key}',
                    max_length=160,
                ),
                'name_zh': _optional_text(
                    group_data,
                    'name_zh',
                    max_length=160,
                ),
                'order': _int(
                    group_data.get('order'),
                    'order',
                    default=group_index,
                ),
                'is_active': _bool(group_data.get('is_active'), True),
                'metadata': group_metadata,
            },
        )
        _mark(stats, created)

        member_specs = []
        if 'dungeon_keys' in group_data:
            dungeon_keys = _list(
                group_data.get('dungeon_keys'),
                f'{group_key}.dungeon_keys',
            )
            member_specs = [
                (order, str(dungeon_key))
                for order, dungeon_key in enumerate(dungeon_keys, start=1)
            ]
        elif 'dungeon_indexes' in group_data:
            dungeon_indexes = _list(
                group_data.get('dungeon_indexes'),
                f'{group_key}.dungeon_indexes',
            )
            for order, external_index in enumerate(dungeon_indexes, start=1):
                try:
                    dungeon = dungeon_by_index[int(external_index)]
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(
                        f'赛季分类 {group_key} 引用了不存在的地下城索引 '
                        f'{external_index}。'
                    ) from exc
                member_specs.append((order, dungeon.key))
        else:
            member_specs = membership_hints.get(group_key, [])

        seen_dungeon_ids = []
        for member_order, dungeon_key in member_specs:
            dungeon = dungeon_by_key.get(dungeon_key)
            if not dungeon:
                raise ValueError(
                    f'赛季分类 {group_key} 引用了不存在的地下城 '
                    f'{dungeon_key}。'
                )
            membership, member_created = (
                MythicDungeonSelectionMembership.objects.update_or_create(
                    selection_group=group,
                    dungeon=dungeon,
                    defaults={
                        'order': member_order,
                        'is_active': True,
                        'metadata': {'source': 'payload'},
                    },
                )
            )
            membership.full_clean()
            seen_dungeon_ids.append(dungeon.id)
            _mark(stats, member_created)
        if replace:
            group.memberships.filter(
                metadata__source='payload',
            ).exclude(
                dungeon_id__in=seen_dungeon_ids,
            ).update(is_active=False)

    if replace:
        MythicDungeonSelectionGroup.objects.filter(
            data_version=version,
            metadata__source='payload',
        ).exclude(
            key__in=seen_group_keys,
        ).update(is_active=False)
    return len(seen_group_keys)


@transaction.atomic
def import_mythic_dungeon_payload(payload, *, activate=False, replace=False, source_bytes=None):
    if not isinstance(payload, dict):
        raise ValueError('数据包根节点必须是 JSON 对象。')
    schema_version = _int(payload.get('schema_version'), 'schema_version', minimum=1, default=1)
    if schema_version != 1:
        raise ValueError('当前仅支持 schema_version=1。')

    version_data = _dict(payload.get('data_version'), 'data_version')
    version_key = _key(version_data, '数据版本')
    version_defaults = {
        'label': _required_text(version_data, 'label', '数据版本', max_length=160),
        'game_version': _optional_text(version_data, 'game_version', max_length=40),
        'season': _optional_text(version_data, 'season', max_length=80),
        'schema_version': schema_version,
        'source_name': _optional_text(version_data, 'source_name', max_length=160),
        'source_reference': _optional_text(version_data, 'source_reference', max_length=500),
        'source_hash': _source_hash(payload, source_bytes),
        'notes': str(version_data.get('notes') or ''),
        'metadata': _dict(version_data.get('metadata'), 'data_version.metadata'),
        'imported_at': timezone.now(),
    }
    version, version_created = MythicDungeonDataVersion.objects.update_or_create(
        key=version_key,
        defaults=version_defaults,
    )
    stats = Counter()
    _mark(stats, version_created)

    dungeon_items = _list(payload.get('dungeons'), 'dungeons')
    if not dungeon_items:
        raise ValueError('数据包至少需要包含一个地下城。')

    seen_dungeons = []
    first_dungeon_key = ''
    for dungeon_order, dungeon_data in enumerate(dungeon_items):
        if not isinstance(dungeon_data, dict):
            raise ValueError(f'第 {dungeon_order + 1} 个地下城必须是 JSON 对象。')
        dungeon_key = _key(dungeon_data, f'第 {dungeon_order + 1} 个地下城')
        first_dungeon_key = first_dungeon_key or dungeon_key
        seen_dungeons.append(dungeon_key)
        dungeon_defaults = {
            'external_index': _int(dungeon_data.get('external_index'), 'external_index', nullable=True),
            'name': _required_text(dungeon_data, 'name', f'地下城 {dungeon_key}', max_length=160),
            'name_zh': _optional_text(dungeon_data, 'name_zh', max_length=160),
            'short_name': _optional_text(dungeon_data, 'short_name', max_length=32),
            'map_id': _int(dungeon_data.get('map_id'), 'map_id', nullable=True),
            'total_enemy_forces': _int(dungeon_data.get('total_enemy_forces'), 'total_enemy_forces'),
            'order': _int(dungeon_data.get('order'), 'order', default=dungeon_order),
            'is_active': _bool(dungeon_data.get('is_active'), True),
            'metadata': _dict(dungeon_data.get('metadata'), f'{dungeon_key}.metadata'),
        }
        dungeon, created = MythicDungeon.objects.update_or_create(
            data_version=version,
            key=dungeon_key,
            defaults=dungeon_defaults,
        )
        _mark(stats, created)

        floor_map = {}
        seen_floors = []
        seen_pois_by_floor = {}
        floor_items = _list(dungeon_data.get('floors'), f'{dungeon_key}.floors')
        if not floor_items:
            raise ValueError(f'地下城 {dungeon_key} 至少需要一个楼层。')
        for floor_order, floor_data in enumerate(floor_items):
            if not isinstance(floor_data, dict):
                raise ValueError(f'{dungeon_key} 的第 {floor_order + 1} 个楼层必须是 JSON 对象。')
            floor_key = _key(floor_data, f'{dungeon_key} 楼层')
            seen_floors.append(floor_key)
            floor_defaults = {
                'floor_index': _int(floor_data.get('floor_index'), 'floor_index', minimum=1, default=floor_order + 1),
                'name': _required_text(floor_data, 'name', f'楼层 {floor_key}', max_length=160),
                'name_zh': _optional_text(floor_data, 'name_zh', max_length=160),
                'background_url': _optional_text(floor_data, 'background_url', max_length=1000),
                'background_color': _optional_text(floor_data, 'background_color', max_length=32, default='#66533f'),
                'map_width': _int(floor_data.get('map_width'), 'map_width', minimum=100, default=1000),
                'map_height': _int(floor_data.get('map_height'), 'map_height', minimum=100, default=700),
                'order': _int(floor_data.get('order'), 'order', default=floor_order),
                'is_active': _bool(floor_data.get('is_active'), True),
                'metadata': _dict(floor_data.get('metadata'), f'{floor_key}.metadata'),
            }
            floor, created = MythicDungeonFloor.objects.update_or_create(
                dungeon=dungeon,
                key=floor_key,
                defaults=floor_defaults,
            )
            floor_map[floor_key] = floor
            _mark(stats, created)

            seen_pois = []
            for poi_index, poi_data in enumerate(_list(floor_data.get('pois'), f'{floor_key}.pois')):
                if not isinstance(poi_data, dict):
                    raise ValueError(f'{floor_key} 的第 {poi_index + 1} 个兴趣点必须是 JSON 对象。')
                poi_key = _key(poi_data, f'{floor_key} 兴趣点')
                seen_pois.append(poi_key)
                poi_defaults = {
                    'poi_type': _optional_text(poi_data, 'type', max_length=60, default='note'),
                    'x': _float(poi_data.get('x'), 'x', minimum=0, maximum=100, default=50),
                    'y': _float(poi_data.get('y'), 'y', minimum=0, maximum=100, default=50),
                    'label': _optional_text(poi_data, 'label', max_length=160),
                    'icon_url': _optional_text(poi_data, 'icon_url', max_length=1000),
                    'target_floor_key': _optional_text(poi_data, 'target_floor_key', max_length=100),
                    'is_active': _bool(poi_data.get('is_active'), True),
                    'metadata': _dict(poi_data.get('metadata'), f'{poi_key}.metadata'),
                }
                _, created = MythicDungeonPoi.objects.update_or_create(
                    floor=floor,
                    key=poi_key,
                    defaults=poi_defaults,
                )
                _mark(stats, created)
            seen_pois_by_floor[floor.id] = seen_pois

        seen_enemies = []
        for enemy_index, enemy_data in enumerate(_list(dungeon_data.get('enemies'), f'{dungeon_key}.enemies')):
            if not isinstance(enemy_data, dict):
                raise ValueError(f'{dungeon_key} 的第 {enemy_index + 1} 个怪物必须是 JSON 对象。')
            enemy_key = _key(enemy_data, f'{dungeon_key} 怪物')
            seen_enemies.append(enemy_key)
            enemy_defaults = {
                'npc_id': _int(enemy_data.get('npc_id'), 'npc_id', nullable=True),
                'name': _required_text(enemy_data, 'name', f'怪物 {enemy_key}', max_length=160),
                'name_zh': _optional_text(enemy_data, 'name_zh', max_length=160),
                'enemy_forces': _int(enemy_data.get('enemy_forces'), 'enemy_forces'),
                'base_health': _int(enemy_data.get('base_health'), 'base_health'),
                'level': _int(enemy_data.get('level'), 'level'),
                'creature_type': _optional_text(enemy_data, 'creature_type', max_length=80),
                'icon_url': _optional_text(enemy_data, 'icon_url', max_length=1000),
                'marker_color': _optional_text(enemy_data, 'marker_color', max_length=32, default='#94a3b8'),
                'is_boss': _bool(enemy_data.get('is_boss'), False),
                'is_active': _bool(enemy_data.get('is_active'), True),
                'traits': _dict(enemy_data.get('traits'), f'{enemy_key}.traits'),
                'metadata': _dict(enemy_data.get('metadata'), f'{enemy_key}.metadata'),
            }
            enemy, created = MythicDungeonEnemy.objects.update_or_create(
                dungeon=dungeon,
                key=enemy_key,
                defaults=enemy_defaults,
            )
            _mark(stats, created)

            seen_spell_ids = []
            for ability_order, ability_data in enumerate(_list(enemy_data.get('abilities'), f'{enemy_key}.abilities')):
                if not isinstance(ability_data, dict):
                    raise ValueError(f'{enemy_key} 的第 {ability_order + 1} 个技能必须是 JSON 对象。')
                spell_id = _int(ability_data.get('spell_id'), 'spell_id', minimum=1)
                seen_spell_ids.append(spell_id)
                ability_name = _required_text(
                    ability_data,
                    'name',
                    f'技能 {spell_id}',
                    max_length=160,
                )
                ability_name_zh = _optional_text(
                    ability_data,
                    'name_zh',
                    max_length=160,
                )
                ability_description = str(ability_data.get('description') or '')
                ability_description_zh = str(ability_data.get('description_zh') or '')
                ability_icon_url = _optional_text(
                    ability_data,
                    'icon_url',
                    max_length=1000,
                )
                spell_defaults = {
                    'name': '' if _is_spell_placeholder(ability_name, spell_id) else ability_name,
                    'name_zh': (
                        ''
                        if _is_spell_placeholder(ability_name_zh, spell_id)
                        else ability_name_zh
                    ),
                    'description': ability_description,
                    'description_zh': ability_description_zh,
                    'icon_url': ability_icon_url,
                    'is_active': True,
                    'metadata': {
                        'source': (
                            ability_data.get('metadata', {}).get('source', '')
                            if isinstance(ability_data.get('metadata'), dict)
                            else ''
                        ),
                        'import_status': 'payload',
                    },
                }
                spell_record, spell_created = MythicDungeonSpell.objects.get_or_create(
                    data_version=version,
                    spell_id=spell_id,
                    defaults=spell_defaults,
                )
                if not spell_created:
                    spell_updates = {'is_active': True}
                    for field in (
                        'name',
                        'name_zh',
                        'description',
                        'description_zh',
                        'icon_url',
                    ):
                        incoming = spell_defaults[field]
                        if incoming:
                            spell_updates[field] = incoming
                    for field, value in spell_updates.items():
                        setattr(spell_record, field, value)
                    spell_record.save(
                        update_fields=[*spell_updates.keys(), 'updated_at'],
                    )
                _mark(stats, spell_created)
                ability_defaults = {
                    'spell_record': spell_record,
                    'name': ability_name,
                    'name_zh': ability_name_zh,
                    'description': ability_description,
                    'description_zh': ability_description_zh,
                    'icon_url': ability_icon_url,
                    'interruptible': _bool(ability_data.get('interruptible'), False),
                    'dispel_type': _optional_text(ability_data, 'dispel_type', max_length=40),
                    'danger_level': _int(ability_data.get('danger_level'), 'danger_level', minimum=1, default=1),
                    'order': _int(ability_data.get('order'), 'order', default=ability_order),
                    'is_active': _bool(ability_data.get('is_active'), True),
                    'metadata': _dict(ability_data.get('metadata'), f'{spell_id}.metadata'),
                }
                _, created = MythicDungeonAbility.objects.update_or_create(
                    enemy=enemy,
                    spell_id=spell_id,
                    defaults=ability_defaults,
                )
                _mark(stats, created)

            seen_spawns = []
            for spawn_index, spawn_data in enumerate(_list(enemy_data.get('spawns'), f'{enemy_key}.spawns')):
                if not isinstance(spawn_data, dict):
                    raise ValueError(f'{enemy_key} 的第 {spawn_index + 1} 个刷新点必须是 JSON 对象。')
                spawn_key = _key(spawn_data, f'{enemy_key} 刷新点')
                seen_spawns.append(spawn_key)
                floor_key = _required_text(spawn_data, 'floor_key', f'刷新点 {spawn_key}', max_length=100)
                floor = floor_map.get(floor_key)
                if not floor:
                    raise ValueError(f'刷新点 {enemy_key}:{spawn_key} 引用了不存在的楼层 {floor_key}。')
                spawn_defaults = {
                    'floor': floor,
                    'x': _float(spawn_data.get('x'), 'x', minimum=0, maximum=100, default=50),
                    'y': _float(spawn_data.get('y'), 'y', minimum=0, maximum=100, default=50),
                    'group_key': _optional_text(spawn_data, 'group_key', max_length=100),
                    'scale': _float(spawn_data.get('scale'), 'scale', minimum=0.25, maximum=5, default=1),
                    'patrol': _list(spawn_data.get('patrol'), f'{spawn_key}.patrol'),
                    'is_active': _bool(spawn_data.get('is_active'), True),
                    'metadata': _dict(spawn_data.get('metadata'), f'{spawn_key}.metadata'),
                }
                _, created = MythicDungeonSpawn.objects.update_or_create(
                    enemy=enemy,
                    key=spawn_key,
                    defaults=spawn_defaults,
                )
                _mark(stats, created)

            if replace:
                MythicDungeonAbility.objects.filter(enemy=enemy).exclude(spell_id__in=seen_spell_ids).update(is_active=False)
                MythicDungeonSpawn.objects.filter(enemy=enemy).exclude(key__in=seen_spawns).update(is_active=False)

        if replace:
            MythicDungeonFloor.objects.filter(dungeon=dungeon).exclude(key__in=seen_floors).update(is_active=False)
            MythicDungeonEnemy.objects.filter(dungeon=dungeon).exclude(key__in=seen_enemies).update(is_active=False)
            for floor_id, poi_keys in seen_pois_by_floor.items():
                MythicDungeonPoi.objects.filter(floor_id=floor_id).exclude(key__in=poi_keys).update(is_active=False)

    selection_group_count = _sync_selection_groups(
        version=version,
        payload=payload,
        version_metadata=version_defaults['metadata'],
        dungeon_items=dungeon_items,
        replace=replace,
        stats=stats,
    )

    if replace:
        MythicDungeon.objects.filter(data_version=version).exclude(key__in=seen_dungeons).update(is_active=False)
        active_spell_ids = MythicDungeonAbility.objects.filter(
            enemy__dungeon__data_version=version,
            is_active=True,
        ).values_list('spell_id', flat=True)
        MythicDungeonSpell.objects.filter(data_version=version).exclude(
            spell_id__in=active_spell_ids,
        ).update(is_active=False)

    if activate:
        MythicDungeonDataVersion.objects.exclude(pk=version.pk).filter(is_active=True).update(is_active=False)
        version.is_active = True
        version.save(update_fields=['is_active', 'updated_at'])

    config, config_created = MythicPlannerConfig.objects.get_or_create(
        key='default',
        defaults={'default_dungeon_key': first_dungeon_key},
    )
    if (
        not config.default_dungeon_key
        or (activate and config.default_dungeon_key not in seen_dungeons)
    ):
        config.default_dungeon_key = first_dungeon_key
        config.save(update_fields=['default_dungeon_key', 'updated_at'])
    _mark(stats, config_created)

    return {
        'version_key': version.key,
        'active': version.is_active,
        'created': stats['created'],
        'updated': stats['updated'],
        'dungeons': len(seen_dungeons),
        'selection_groups': selection_group_count,
        'replace': bool(replace),
    }
