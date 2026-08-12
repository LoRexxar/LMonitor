import hashlib
import json

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from botend.dashboard.permissions import has_dashboard_permission
from botend.models import (
    MythicDungeon,
    MythicDungeonAbility,
    MythicDungeonDataVersion,
    MythicDungeonEnemy,
    MythicDungeonFloor,
    MythicDungeonPoi,
    MythicDungeonRoute,
    MythicDungeonRouteShare,
    MythicDungeonSelectionGroup,
    MythicDungeonSelectionMembership,
    MythicDungeonSpell,
    MythicDungeonSpawn,
    MythicPlannerConfig,
)
from botend.mythic_planner.spell_tooltips import (
    QUALITY_MANUAL_OVERRIDE,
    SOURCE_MANUAL,
    build_description_metadata,
)
from botend.mythic_planner.services import (
    decode_share_code,
    encode_share_code,
    get_active_dungeon,
    owned_route_queryset,
    serialize_ability,
    serialize_catalog,
    serialize_dungeon,
    serialize_route,
    validate_route_payload,
)
from botend.mythic_planner.importer import import_mythic_dungeon_payload


SHORT_LINK_RATE_LIMIT = 30
SHORT_LINK_RATE_WINDOW_SECONDS = 60 * 60


def success(data=None, **extra):
    payload = {'success': True}
    if data is not None:
        payload['data'] = data
    payload.update(extra)
    return JsonResponse(payload, json_dumps_params={'ensure_ascii': False})


def error(message, status=400, **extra):
    payload = {'success': False, 'message': str(message)}
    payload.update(extra)
    return JsonResponse(payload, status=status, json_dumps_params={'ensure_ascii': False})


def parse_body(request):
    try:
        data = json.loads(request.body.decode('utf-8') or '{}')
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError('请求体必须是 UTF-8 编码的 JSON。') from exc
    if not isinstance(data, dict):
        raise ValueError('请求体必须是 JSON 对象。')
    return data


def _consume_short_link_quota(request):
    remote_address = str(request.META.get('REMOTE_ADDR') or 'unknown')
    client_hash = hashlib.sha256(remote_address.encode('utf-8')).hexdigest()[:24]
    cache_key = f'mythic-planner:short-link:{client_hash}'
    if cache.add(cache_key, 1, timeout=SHORT_LINK_RATE_WINDOW_SECONDS):
        return True
    try:
        count = cache.incr(cache_key)
    except ValueError:
        cache.set(cache_key, 1, timeout=SHORT_LINK_RATE_WINDOW_SECONDS)
        count = 1
    return count <= SHORT_LINK_RATE_LIMIT


def _serialize_route_share(share, request):
    short_path = f'/m/{share.token}'
    return {
        'token': share.token,
        'name': share.name,
        'dungeon_key': share.dungeon.key,
        'dungeon_level': share.dungeon_level,
        'route_data': share.route_data or {},
        'share_code': encode_share_code(share.route_data or {}),
        'short_path': short_path,
        'short_url': request.build_absolute_uri(short_path),
        'created_at': share.created_at.isoformat() if share.created_at else None,
    }


class MythicPlannerCatalogAPIView(View):
    def get(self, request):
        return success(serialize_catalog())


class MythicPlannerDungeonAPIView(View):
    def get(self, request, dungeon_key):
        dungeon = get_active_dungeon(dungeon_key)
        if not dungeon:
            return error('当前数据版本中不存在该地下城。', status=404)
        return success(serialize_dungeon(dungeon))


@method_decorator(csrf_exempt, name='dispatch')
class MythicPlannerShareCodeAPIView(View):
    def post(self, request):
        try:
            body = parse_body(request)
            action = str(body.get('action') or 'decode')
            if action == 'encode':
                payload = body.get('route_data')
                dungeon_key = str((payload or {}).get('dungeon_key') or '')
                dungeon = get_active_dungeon(dungeon_key)
                if not dungeon:
                    return error('路线所属地下城不存在。', status=404)
                validated = validate_route_payload(payload, dungeon)
                return success({
                    'share_code': encode_share_code(validated.payload),
                    'route_data': validated.payload,
                })
            code = body.get('share_code')
            payload = decode_share_code(code)
            dungeon = get_active_dungeon(str(payload.get('dungeon_key') or ''))
            if not dungeon:
                return error('分享路线所属地下城不在当前数据版本中。', status=404)
            validated = validate_route_payload(payload, dungeon)
            return success({
                'route_data': validated.payload,
                'dungeon_key': dungeon.key,
            })
        except ValueError as exc:
            return error(exc)


@method_decorator(csrf_exempt, name='dispatch')
class MythicPlannerRouteShareAPIView(View):
    """创建和读取与账号无关的只读路线短链接。"""

    def post(self, request, share_token=None):
        if share_token is not None:
            return error('短链接快照不可修改。', status=405)
        if not _consume_short_link_quota(request):
            return error('短链接生成过于频繁，请稍后再试。', status=429)
        try:
            body = parse_body(request)
            payload = body.get('route_data')
            dungeon_key = str((payload or {}).get('dungeon_key') or '')
            dungeon = get_active_dungeon(dungeon_key)
            if not dungeon:
                return error('路线所属地下城不存在。', status=404)
            config = MythicPlannerConfig.objects.filter(key='default').first()
            if config and not config.allow_public_route_share:
                return error('管理员已关闭公开路线分享。', status=403)
            validated = validate_route_payload(payload, dungeon)
            level = int(validated.payload.get('dungeon_level') or 10)
            if level < 2 or level > 99:
                raise ValueError('地下城层数必须在 2–99 之间。')
            name = str(validated.payload.get('name') or '未命名路线').strip()[:160]
            name = name or '未命名路线'
            encode_share_code(validated.payload)
            canonical = json.dumps(
                validated.payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
            ).encode('utf-8')
            content_hash = hashlib.sha256(canonical).hexdigest()
            with transaction.atomic():
                share, _created = MythicDungeonRouteShare.objects.get_or_create(
                    content_hash=content_hash,
                    defaults={
                        'dungeon': dungeon,
                        'name': name,
                        'dungeon_level': level,
                        'route_data': validated.payload,
                    },
                )
                if not share.is_active:
                    share.is_active = True
                    share.save(update_fields=['is_active', 'updated_at'])
            return success(_serialize_route_share(share, request))
        except (TypeError, ValueError) as exc:
            return error(exc)

    def get(self, request, share_token=None):
        if not share_token:
            return error('缺少短链接令牌。')
        share = (
            MythicDungeonRouteShare.objects.filter(
                token=share_token,
                is_active=True,
            )
            .select_related('dungeon', 'dungeon__data_version')
            .first()
        )
        if not share:
            return error('分享链接不存在或已失效。', status=404)
        MythicDungeonRouteShare.objects.filter(pk=share.pk).update(
            view_count=F('view_count') + 1,
            last_accessed_at=timezone.now(),
        )
        return success(_serialize_route_share(share, request))


class MythicPlannerRouteAPIView(View):
    def _require_user(self, request):
        if not request.user.is_authenticated:
            return error('登录后才能保存服务器路线。', status=401)
        return None

    def get(self, request, route_id=None):
        auth_error = self._require_user(request)
        if auth_error:
            return auth_error
        queryset = owned_route_queryset(request.user)
        if route_id is not None:
            route = queryset.filter(id=route_id).first()
            if not route:
                return error('路线不存在。', status=404)
            return success(serialize_route(route))
        return success([serialize_route(route) for route in queryset[:200]])

    def post(self, request, route_id=None):
        auth_error = self._require_user(request)
        if auth_error:
            return auth_error
        try:
            body = parse_body(request)
            dungeon_key = str(body.get('dungeon_key') or '')
            dungeon = get_active_dungeon(dungeon_key)
            if not dungeon:
                return error('当前数据版本中不存在该地下城。', status=404)
            name = str(body.get('name') or '未命名路线').strip()[:160] or '未命名路线'
            level = int(body.get('dungeon_level') or 10)
            if level < 2 or level > 99:
                raise ValueError('地下城层数必须在 2–99 之间。')
            validated = validate_route_payload(body.get('route_data') or {}, dungeon)
            is_public = bool(body.get('is_public', False))
            config = MythicPlannerConfig.objects.filter(key='default').first()
            if is_public and config and not config.allow_public_route_share:
                raise ValueError('管理员已关闭公开路线分享。')

            with transaction.atomic():
                if route_id is None:
                    route = MythicDungeonRoute.objects.create(
                        owner_user_id=request.user.id,
                        dungeon=dungeon,
                        name=name,
                        dungeon_level=level,
                        route_data=validated.payload,
                        is_public=is_public,
                    )
                else:
                    route = owned_route_queryset(request.user).filter(id=route_id).first()
                    if not route:
                        return error('路线不存在。', status=404)
                    route.dungeon = dungeon
                    route.name = name
                    route.dungeon_level = level
                    route.route_data = validated.payload
                    route.is_public = is_public
                    route.revision += 1
                    route.save()
            return success(serialize_route(route))
        except (TypeError, ValueError) as exc:
            return error(exc)

    def delete(self, request, route_id=None):
        auth_error = self._require_user(request)
        if auth_error:
            return auth_error
        if route_id is None:
            return error('缺少路线 ID。')
        route = owned_route_queryset(request.user).filter(id=route_id).first()
        if not route:
            return error('路线不存在。', status=404)
        route.is_active = False
        route.save(update_fields=['is_active', 'updated_at'])
        return success({'id': route.id, 'archived': True})


class MythicPlannerSharedRouteAPIView(View):
    def get(self, request, share_id):
        route = (
            MythicDungeonRoute.objects.filter(
                share_id=share_id,
                is_public=True,
                is_active=True,
            )
            .select_related('dungeon', 'dungeon__data_version')
            .first()
        )
        if not route:
            return error('公开路线不存在或已停止分享。', status=404)
        return success(serialize_route(route))


def _iso(value):
    return value.isoformat() if value else None


def _management_ability(row):
    resolved = serialize_ability(row)
    return {
        'id': row.id,
        'enemy_id': row.enemy_id,
        'enemy_name': row.enemy.display_name,
        'spell_record_id': row.spell_record_id,
        'spell_id': row.spell_id,
        'name': row.name,
        'name_zh': row.name_zh,
        'display_name': resolved['display_name'],
        'description': row.description,
        'description_zh': row.description_zh,
        'resolved_description': resolved['description'],
        'resolved_description_zh': resolved['description_zh'],
        'icon_url': row.icon_url,
        'resolved_icon_url': resolved['icon_url'],
        'interruptible': row.interruptible,
        'dispel_type': row.dispel_type,
        'danger_level': row.danger_level,
        'order': row.order,
        'is_active': row.is_active,
        'metadata': row.metadata or {},
    }


def _management_route(row, owner=None, include_payload=False):
    route_data = row.route_data if isinstance(row.route_data, dict) else {}
    pulls = route_data.get('pulls')
    pulls = pulls if isinstance(pulls, list) else []
    annotations = route_data.get('annotations')
    annotations = annotations if isinstance(annotations, list) else []
    spawn_count = sum(
        len(pull.get('spawn_uids') or [])
        for pull in pulls
        if isinstance(pull, dict) and isinstance(pull.get('spawn_uids'), list)
    )
    owner_username = owner.get_username() if owner else ''
    owner_display_name = ''
    if owner:
        get_full_name = getattr(owner, 'get_full_name', None)
        if callable(get_full_name):
            owner_display_name = str(get_full_name() or '').strip()
    result = {
        'id': row.id,
        'share_id': str(row.share_id),
        'owner_user_id': row.owner_user_id,
        'owner_username': owner_username,
        'owner_display_name': owner_display_name or owner_username,
        'owner_email': str(getattr(owner, 'email', '') or '') if owner else '',
        'owner_exists': owner is not None,
        'dungeon_id': row.dungeon_id,
        'dungeon_name': row.dungeon.display_name,
        'data_version_id': row.dungeon.data_version_id,
        'version_label': row.dungeon.data_version.label,
        'name': row.name,
        'dungeon_level': row.dungeon_level,
        'pull_count': len(pulls),
        'spawn_count': spawn_count,
        'annotation_count': len(annotations),
        'revision': row.revision,
        'is_public': row.is_public,
        'is_active': row.is_active,
        'created_at': _iso(row.created_at),
        'updated_at': _iso(row.updated_at),
    }
    if include_payload:
        result.update({
            'route_data': route_data,
            'share_code': encode_share_code(route_data),
        })
    return result


def management_snapshot(resources=None, dungeon_id=None):
    if resources is None:
        requested_resources = None
    elif isinstance(resources, str):
        requested_resources = {
            item.strip()
            for item in resources.split(',')
            if item.strip()
        }
    else:
        requested_resources = {
            str(item).strip()
            for item in resources
            if str(item).strip()
        }

    def wants(resource):
        return requested_resources is None or resource in requested_resources

    try:
        dungeon_filter_id = int(dungeon_id) if dungeon_id not in (None, '') else None
    except (TypeError, ValueError):
        dungeon_filter_id = None

    versions = (
        list(MythicDungeonDataVersion.objects.all())
        if wants('versions')
        else []
    )
    dungeons = (
        list(MythicDungeon.objects.select_related('data_version').all())
        if wants('dungeons')
        else []
    )
    selection_groups = list(
        MythicDungeonSelectionGroup.objects.select_related('data_version').all()
    ) if wants('selection_groups') else []
    selection_memberships = list(
        MythicDungeonSelectionMembership.objects.select_related(
            'selection_group',
            'dungeon',
        ).all()
    ) if wants('selection_memberships') else []
    floor_query = MythicDungeonFloor.objects.select_related('dungeon')
    enemy_query = MythicDungeonEnemy.objects.select_related('dungeon')
    if dungeon_filter_id:
        floor_query = floor_query.filter(dungeon_id=dungeon_filter_id)
        enemy_query = enemy_query.filter(dungeon_id=dungeon_filter_id)
    floors = list(floor_query.all()) if wants('floors') else []
    enemies = list(enemy_query.all()) if wants('enemies') else []
    spells = (
        list(MythicDungeonSpell.objects.select_related('data_version').all())
        if wants('spells')
        else []
    )
    abilities = list(
        MythicDungeonAbility.objects.select_related('enemy', 'spell_record').all()
    ) if wants('abilities') else []
    spawn_query = MythicDungeonSpawn.objects.select_related('enemy', 'floor')
    poi_query = MythicDungeonPoi.objects.select_related('floor')
    if dungeon_filter_id:
        spawn_query = spawn_query.filter(enemy__dungeon_id=dungeon_filter_id)
        poi_query = poi_query.filter(floor__dungeon_id=dungeon_filter_id)
    spawns = list(spawn_query.all()) if wants('spawns') else []
    pois = list(poi_query.all()) if wants('pois') else []
    routes = list(
        MythicDungeonRoute.objects.select_related(
            'dungeon',
            'dungeon__data_version',
        ).all()
    ) if wants('routes') else []
    owner_ids = {
        row.owner_user_id
        for row in routes
        if row.owner_user_id is not None
    }
    owners = get_user_model().objects.in_bulk(owner_ids)
    configs = (
        list(MythicPlannerConfig.objects.all())
        if wants('configs')
        else []
    )

    count_models = {
        'versions': MythicDungeonDataVersion,
        'dungeons': MythicDungeon,
        'selection_groups': MythicDungeonSelectionGroup,
        'selection_memberships': MythicDungeonSelectionMembership,
        'floors': MythicDungeonFloor,
        'enemies': MythicDungeonEnemy,
        'spells': MythicDungeonSpell,
        'abilities': MythicDungeonAbility,
        'spawns': MythicDungeonSpawn,
        'pois': MythicDungeonPoi,
        'routes': MythicDungeonRoute,
        'configs': MythicPlannerConfig,
    }
    loaded_rows = {
        'versions': versions,
        'dungeons': dungeons,
        'selection_groups': selection_groups,
        'selection_memberships': selection_memberships,
        'floors': floors,
        'enemies': enemies,
        'spells': spells,
        'abilities': abilities,
        'spawns': spawns,
        'pois': pois,
        'routes': routes,
        'configs': configs,
    }

    def resource_count(resource):
        if wants(resource):
            return len(loaded_rows[resource])
        if wants('counts'):
            return count_models[resource].objects.count()
        return 0

    return {
        'versions': [
            {
                'id': row.id,
                'key': row.key,
                'label': row.label,
                'game_version': row.game_version,
                'season': row.season,
                'schema_version': row.schema_version,
                'source_name': row.source_name,
                'source_reference': row.source_reference,
                'source_hash': row.source_hash,
                'is_active': row.is_active,
                'notes': row.notes,
                'metadata': row.metadata or {},
                'imported_at': _iso(row.imported_at),
                'updated_at': _iso(row.updated_at),
            }
            for row in versions
        ],
        'dungeons': [
            {
                'id': row.id,
                'data_version_id': row.data_version_id,
                'key': row.key,
                'external_index': row.external_index,
                'name': row.name,
                'name_zh': row.name_zh,
                'short_name': row.short_name,
                'map_id': row.map_id,
                'total_enemy_forces': row.total_enemy_forces,
                'order': row.order,
                'is_active': row.is_active,
                'metadata': row.metadata or {},
            }
            for row in dungeons
        ],
        'selection_groups': [
            {
                'id': row.id,
                'data_version_id': row.data_version_id,
                'version_label': row.data_version.label,
                'key': row.key,
                'name': row.name,
                'name_zh': row.name_zh,
                'display_name': row.display_name,
                'order': row.order,
                'is_active': row.is_active,
                'metadata': row.metadata or {},
            }
            for row in selection_groups
        ],
        'selection_memberships': [
            {
                'id': row.id,
                'data_version_id': row.selection_group.data_version_id,
                'selection_group_id': row.selection_group_id,
                'selection_group_name': row.selection_group.display_name,
                'dungeon_id': row.dungeon_id,
                'dungeon_name': row.dungeon.display_name,
                'order': row.order,
                'is_active': row.is_active,
                'metadata': row.metadata or {},
            }
            for row in selection_memberships
        ],
        'floors': [
            {
                'id': row.id,
                'dungeon_id': row.dungeon_id,
                'key': row.key,
                'floor_index': row.floor_index,
                'name': row.name,
                'name_zh': row.name_zh,
                'background_url': row.background_url,
                'background_color': row.background_color,
                'map_width': row.map_width,
                'map_height': row.map_height,
                'order': row.order,
                'is_active': row.is_active,
                'metadata': row.metadata or {},
            }
            for row in floors
        ],
        'enemies': [
            {
                'id': row.id,
                'dungeon_id': row.dungeon_id,
                'key': row.key,
                'npc_id': row.npc_id,
                'name': row.name,
                'name_zh': row.name_zh,
                'enemy_forces': row.enemy_forces,
                'base_health': row.base_health,
                'level': row.level,
                'creature_type': row.creature_type,
                'icon_url': row.icon_url,
                'marker_color': row.marker_color,
                'is_boss': row.is_boss,
                'is_active': row.is_active,
                'traits': row.traits or {},
                'metadata': row.metadata or {},
            }
            for row in enemies
        ],
        'spells': [
            {
                'id': row.id,
                'data_version_id': row.data_version_id,
                'version_label': row.data_version.label,
                'spell_id': row.spell_id,
                'source_branch': row.source_branch,
                'source_locale': row.source_locale,
                'snapshot_build': row.snapshot_build,
                'description_source': (row.metadata or {}).get(
                    'description_source',
                    '',
                ),
                'description_quality': (row.metadata or {}).get(
                    'description_quality',
                    '',
                ),
                'name': row.name,
                'name_zh': row.name_zh,
                'display_name': row.display_name,
                'description': row.description,
                'description_zh': row.description_zh,
                'aura_description': row.aura_description,
                'aura_description_zh': row.aura_description_zh,
                'icon_file_data_id': row.icon_file_data_id,
                'icon_name': row.icon_name,
                'icon_url': row.icon_url,
                'is_active': row.is_active,
                'metadata': row.metadata or {},
            }
            for row in spells
        ],
        'abilities': [_management_ability(row) for row in abilities],
        'spawns': [
            {
                'id': row.id,
                'enemy_id': row.enemy_id,
                'floor_id': row.floor_id,
                'key': row.key,
                'x': row.x,
                'y': row.y,
                'group_key': row.group_key,
                'scale': row.scale,
                'patrol': row.patrol or [],
                'is_active': row.is_active,
                'metadata': row.metadata or {},
                'is_position_manual': bool(
                    (row.metadata or {}).get('manual_position_override')
                ),
                'is_group_manual': bool(
                    (row.metadata or {}).get('manual_group_override')
                ),
                'imported_position': (
                    (row.metadata or {}).get('imported_position')
                    if isinstance(
                        (row.metadata or {}).get('imported_position'),
                        dict,
                    )
                    else None
                ),
                'imported_group_key': (
                    str((row.metadata or {}).get('imported_group_key') or '')
                    if (row.metadata or {}).get('manual_group_override')
                    else None
                ),
            }
            for row in spawns
        ],
        'pois': [
            {
                'id': row.id,
                'floor_id': row.floor_id,
                'key': row.key,
                'poi_type': row.poi_type,
                'x': row.x,
                'y': row.y,
                'label': row.label,
                'icon_url': row.icon_url,
                'target_floor_key': row.target_floor_key,
                'is_active': row.is_active,
                'metadata': row.metadata or {},
            }
            for row in pois
        ],
        'routes': [
            _management_route(row, owners.get(row.owner_user_id))
            for row in routes
        ],
        'configs': [
            {
                'id': row.id,
                'key': row.key,
                'default_dungeon_key': row.default_dungeon_key,
                'default_dungeon_level': row.default_dungeon_level,
                'min_dungeon_level': row.min_dungeon_level,
                'max_dungeon_level': row.max_dungeon_level,
                'group_selection_default': row.group_selection_default,
                'live_sync_enabled': row.live_sync_enabled,
                'allow_public_route_share': row.allow_public_route_share,
                'settings': row.settings or {},
                'updated_by_user_id': row.updated_by_user_id,
                'updated_at': _iso(row.updated_at),
            }
            for row in configs
        ],
        'counts': {
            resource: resource_count(resource)
            for resource in count_models
        },
    }


RESOURCE_SPECS = {
    'routes': {
        'model': MythicDungeonRoute,
        'fields': {'is_public', 'is_active'},
        'bool': {'is_public', 'is_active'},
    },
    'versions': {
        'model': MythicDungeonDataVersion,
        'fields': {
            'key', 'label', 'game_version', 'season', 'schema_version',
            'source_name', 'source_reference', 'notes', 'metadata', 'is_active',
        },
        'json': {'metadata'},
        'bool': {'is_active'},
        'int': {'schema_version'},
    },
    'dungeons': {
        'model': MythicDungeon,
        'fields': {
            'data_version_id', 'key', 'external_index', 'name', 'name_zh',
            'short_name', 'map_id', 'total_enemy_forces', 'order',
            'is_active', 'metadata',
        },
        'json': {'metadata'},
        'bool': {'is_active'},
        'int': {'data_version_id', 'external_index', 'map_id', 'total_enemy_forces', 'order'},
    },
    'selection_groups': {
        'model': MythicDungeonSelectionGroup,
        'fields': {
            'data_version_id', 'key', 'name', 'name_zh', 'order',
            'is_active', 'metadata',
        },
        'json': {'metadata'},
        'bool': {'is_active'},
        'int': {'data_version_id', 'order'},
    },
    'selection_memberships': {
        'model': MythicDungeonSelectionMembership,
        'fields': {
            'selection_group_id', 'dungeon_id', 'order', 'is_active',
            'metadata',
        },
        'json': {'metadata'},
        'bool': {'is_active'},
        'int': {'selection_group_id', 'dungeon_id', 'order'},
    },
    'floors': {
        'model': MythicDungeonFloor,
        'fields': {
            'dungeon_id', 'key', 'floor_index', 'name', 'name_zh',
            'background_url', 'background_color', 'map_width', 'map_height',
            'order', 'is_active', 'metadata',
        },
        'json': {'metadata'},
        'bool': {'is_active'},
        'int': {'dungeon_id', 'floor_index', 'map_width', 'map_height', 'order'},
    },
    'enemies': {
        'model': MythicDungeonEnemy,
        'fields': {
            'dungeon_id', 'key', 'npc_id', 'name', 'name_zh', 'enemy_forces',
            'base_health', 'level', 'creature_type', 'icon_url', 'marker_color',
            'is_boss', 'is_active', 'traits', 'metadata',
        },
        'json': {'traits', 'metadata'},
        'bool': {'is_boss', 'is_active'},
        'int': {'dungeon_id', 'npc_id', 'enemy_forces', 'base_health', 'level'},
    },
    'spells': {
        'model': MythicDungeonSpell,
        'fields': {
            'data_version_id', 'spell_id', 'source_branch', 'source_locale',
            'snapshot_build', 'name', 'name_zh', 'description',
            'description_zh', 'aura_description', 'aura_description_zh',
            'icon_file_data_id', 'icon_name', 'icon_url', 'is_active',
            'metadata',
        },
        'json': {'metadata'},
        'bool': {'is_active'},
        'int': {'data_version_id', 'spell_id', 'icon_file_data_id'},
    },
    'abilities': {
        'model': MythicDungeonAbility,
        'fields': {
            'enemy_id', 'spell_id', 'name', 'name_zh', 'description',
            'description_zh', 'icon_url', 'interruptible', 'dispel_type',
            'danger_level', 'order', 'is_active', 'metadata',
        },
        'json': {'metadata'},
        'bool': {'interruptible', 'is_active'},
        'int': {'enemy_id', 'spell_id', 'danger_level', 'order'},
    },
    'spawns': {
        'model': MythicDungeonSpawn,
        'fields': {
            'enemy_id', 'floor_id', 'key', 'x', 'y', 'group_key',
            'scale', 'patrol', 'is_active', 'metadata',
        },
        'json': {'patrol', 'metadata'},
        'bool': {'is_active'},
        'int': {'enemy_id', 'floor_id'},
        'float': {'x', 'y', 'scale'},
    },
    'pois': {
        'model': MythicDungeonPoi,
        'fields': {
            'floor_id', 'key', 'poi_type', 'x', 'y', 'label', 'icon_url',
            'target_floor_key', 'is_active', 'metadata',
        },
        'json': {'metadata'},
        'bool': {'is_active'},
        'int': {'floor_id'},
        'float': {'x', 'y'},
    },
    'configs': {
        'model': MythicPlannerConfig,
        'fields': {
            'key', 'default_dungeon_key', 'default_dungeon_level',
            'min_dungeon_level', 'max_dungeon_level',
            'group_selection_default', 'live_sync_enabled',
            'allow_public_route_share', 'settings',
        },
        'json': {'settings'},
        'bool': {
            'group_selection_default', 'live_sync_enabled',
            'allow_public_route_share',
        },
        'int': {'default_dungeon_level', 'min_dungeon_level', 'max_dungeon_level'},
    },
}


def _coerce_resource_data(spec, data):
    clean = {}
    for field in spec['fields']:
        if field not in data:
            continue
        value = data[field]
        if field in spec.get('json', set()) and isinstance(value, str):
            try:
                value = json.loads(value or '{}')
            except json.JSONDecodeError as exc:
                raise ValueError(f'字段 {field} 不是有效 JSON。') from exc
        if field in spec.get('bool', set()):
            value = value if isinstance(value, bool) else str(value).lower() in {'1', 'true', 'yes', 'on'}
        if field in spec.get('int', set()) and value not in (None, ''):
            value = int(value)
        if field in spec.get('float', set()) and value not in (None, ''):
            value = float(value)
        if value == '' and field in {'external_index', 'map_id', 'npc_id'}:
            value = None
        clean[field] = value
    return clean


def _spawn_group_ids(value):
    if not isinstance(value, list) or not value:
        raise ValueError('请选择至少一个怪物点位。')
    if len(value) > 500:
        raise ValueError('一次最多处理 500 个怪物点位。')
    try:
        spawn_ids = list(dict.fromkeys(int(item) for item in value))
    except (TypeError, ValueError) as exc:
        raise ValueError('怪物点位 ID 格式无效。') from exc
    if any(spawn_id <= 0 for spawn_id in spawn_ids):
        raise ValueError('怪物点位 ID 格式无效。')
    return spawn_ids


def _next_manual_spawn_group_key(floor):
    largest = 0
    for group_key in MythicDungeonSpawn.objects.filter(
        floor=floor,
        group_key__startswith='manual-group-',
    ).values_list('group_key', flat=True):
        suffix = str(group_key).removeprefix('manual-group-')
        if suffix.isdigit():
            largest = max(largest, int(suffix))
    return f'manual-group-{largest + 1}'


def _manage_spawn_groups(request, data):
    if not isinstance(data, dict):
        raise ValueError('data 必须是 JSON 对象。')
    action = str(data.get('action') or '').strip()
    if action not in {'create', 'assign', 'remove', 'restore'}:
        raise ValueError('不支持的怪群操作。')
    spawn_ids = _spawn_group_ids(data.get('spawn_ids'))
    now = timezone.now()
    with transaction.atomic():
        spawns = list(
            MythicDungeonSpawn.objects.select_for_update()
            .select_related('floor', 'enemy__dungeon')
            .filter(id__in=spawn_ids)
            .order_by('id')
        )
        if len(spawns) != len(spawn_ids):
            raise ValueError('部分怪物点位不存在或已经删除。')
        floor_ids = {spawn.floor_id for spawn in spawns}
        dungeon_ids = {spawn.enemy.dungeon_id for spawn in spawns}
        if len(floor_ids) != 1 or len(dungeon_ids) != 1:
            raise ValueError('一次只能设置同一个楼层内的怪群。')
        floor = MythicDungeonFloor.objects.select_for_update().get(
            id=spawns[0].floor_id,
        )
        target_group_key = ''
        if action == 'create':
            target_group_key = _next_manual_spawn_group_key(floor)
        elif action == 'assign':
            target_group_key = str(data.get('group_key') or '').strip()
            if not target_group_key:
                raise ValueError('请选择要加入的已有怪群。')
            if not MythicDungeonSpawn.objects.filter(
                floor=floor,
                group_key=target_group_key,
            ).exists():
                raise ValueError('目标怪群不存在于当前楼层。')

        changed = []
        for spawn in spawns:
            metadata = dict(spawn.metadata or {})
            previous_group_key = spawn.group_key
            previous_metadata = dict(metadata)
            if action == 'restore':
                if not metadata.get('manual_group_override'):
                    continue
                spawn.group_key = str(metadata.get('imported_group_key') or '')
                metadata.pop('imported_group_key', None)
                for key in list(metadata):
                    if key.startswith('manual_group_'):
                        metadata.pop(key, None)
            else:
                if not metadata.get('manual_group_override'):
                    metadata['imported_group_key'] = spawn.group_key
                spawn.group_key = target_group_key
                metadata.update({
                    'manual_group_override': True,
                    'manual_group_updated_at': now.isoformat(),
                    'manual_group_updated_by_user_id': request.user.id,
                })
            if (
                spawn.group_key == previous_group_key
                and metadata == previous_metadata
            ):
                continue
            spawn.metadata = metadata
            spawn.updated_at = now
            changed.append(spawn)
        if changed:
            MythicDungeonSpawn.objects.bulk_update(
                changed,
                ['group_key', 'metadata', 'updated_at'],
            )
    return {
        'resource': 'spawn_groups',
        'action': action,
        'group_key': target_group_key,
        'updated': len(changed),
        'spawn_ids': [spawn.id for spawn in spawns],
        'floor_id': floor.id,
    }


@method_decorator(csrf_exempt, name='dispatch')
class DashboardMythicPlannerAPIView(View):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return error('请先登录。', status=401)
        if not has_dashboard_permission(request.user, 'mythic.config'):
            return error('无权访问该 Dashboard 页面。', status=403)
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, object_id=None):
        if object_id is not None:
            resource = str(request.GET.get('resource') or '')
            if resource != 'routes':
                return error('不支持读取该资源详情。', status=404)
            route = (
                MythicDungeonRoute.objects.select_related(
                    'dungeon',
                    'dungeon__data_version',
                )
                .filter(id=object_id)
                .first()
            )
            if not route:
                return error('路线不存在。', status=404)
            owner = None
            if route.owner_user_id is not None:
                owner = get_user_model().objects.filter(
                    pk=route.owner_user_id,
                ).first()
            return success(_management_route(route, owner, include_payload=True))
        return success(management_snapshot(
            request.GET.get('resources'),
            request.GET.get('dungeon_id'),
        ))

    def post(self, request):
        return self._save(request, object_id=None)

    def patch(self, request, object_id=None):
        return self._save(request, object_id=object_id)

    def _save(self, request, object_id=None):
        try:
            body = parse_body(request)
            resource = str(body.get('resource') or '')
            snapshot_resources = body.get('snapshot_resources')
            snapshot_dungeon_id = body.get('snapshot_dungeon_id')
            if resource == 'import':
                import_data = body.get('data')
                if not isinstance(import_data, dict):
                    raise ValueError('导入参数必须是 JSON 对象。')
                payload = import_data.get('payload')
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except json.JSONDecodeError as exc:
                        raise ValueError('数据包内容不是有效 JSON。') from exc
                result = import_mythic_dungeon_payload(
                    payload,
                    activate=bool(import_data.get('activate', False)),
                    replace=bool(import_data.get('replace', False)),
                )
                return success(
                    result,
                    snapshot=management_snapshot(
                        snapshot_resources,
                        snapshot_dungeon_id,
                    ),
                )
            if resource == 'routes':
                if not object_id:
                    raise ValueError('用户路线不能通过后台新建。')
                data = body.get('data')
                if not isinstance(data, dict):
                    raise ValueError('data 必须是 JSON 对象。')
                allowed_fields = {'is_public', 'is_active'}
                requested_fields = allowed_fields.intersection(data)
                if not requested_fields:
                    raise ValueError('没有可更新的路线管理字段。')
                route = MythicDungeonRoute.objects.filter(id=object_id).first()
                if not route:
                    return error('路线不存在。', status=404)
                clean = _coerce_resource_data(
                    RESOURCE_SPECS['routes'],
                    {field: data[field] for field in requested_fields},
                )
                if clean.get('is_public'):
                    config = MythicPlannerConfig.objects.filter(key='default').first()
                    if config and not config.allow_public_route_share:
                        raise ValueError('全局配置已关闭公开路线分享。')
                changed_fields = []
                for field, value in clean.items():
                    if getattr(route, field) == value:
                        continue
                    setattr(route, field, value)
                    changed_fields.append(field)
                if 'is_public' in changed_fields:
                    route.revision += 1
                    changed_fields.append('revision')
                if changed_fields:
                    route.save(update_fields=[*changed_fields, 'updated_at'])
                return success(
                    {'id': route.id, 'resource': resource},
                    snapshot=management_snapshot(
                        snapshot_resources,
                        snapshot_dungeon_id,
                    ),
                )
            if resource == 'spawn_position_reset':
                if not object_id:
                    raise ValueError('缺少刷新点 ID。')
                with transaction.atomic():
                    spawn = get_object_or_404(
                        MythicDungeonSpawn.objects.select_related(
                            'enemy__dungeon',
                            'floor',
                        ),
                        id=object_id,
                    )
                    metadata = dict(spawn.metadata or {})
                    imported_position = metadata.get('imported_position')
                    if not isinstance(imported_position, dict):
                        raise ValueError('该刷新点没有可恢复的上游坐标。')
                    floor_key = str(
                        imported_position.get('floor_key') or ''
                    ).strip()
                    target_floor = (
                        MythicDungeonFloor.objects.filter(
                            dungeon=spawn.enemy.dungeon,
                            key=floor_key,
                        ).first()
                    )
                    if not target_floor:
                        raise ValueError('上游坐标引用的楼层已经不存在。')
                    try:
                        source_x = float(imported_position.get('x'))
                        source_y = float(imported_position.get('y'))
                    except (TypeError, ValueError) as exc:
                        raise ValueError('上游坐标格式无效。') from exc
                    if not 0 <= source_x <= 100 or not 0 <= source_y <= 100:
                        raise ValueError('上游坐标超出地图范围。')
                    spawn.floor = target_floor
                    spawn.x = source_x
                    spawn.y = source_y
                    for key in list(metadata):
                        if key.startswith('manual_position_'):
                            metadata.pop(key, None)
                    spawn.metadata = metadata
                    spawn.full_clean()
                    spawn.save(update_fields=[
                        'floor',
                        'x',
                        'y',
                        'metadata',
                        'updated_at',
                    ])
                return success(
                    {'id': spawn.id, 'resource': 'spawns', 'restored': True},
                    snapshot=management_snapshot(
                        snapshot_resources,
                        snapshot_dungeon_id,
                    ),
                )
            if resource == 'spawn_groups':
                result = _manage_spawn_groups(request, body.get('data'))
                return success(
                    result,
                    snapshot=management_snapshot(
                        snapshot_resources,
                        snapshot_dungeon_id,
                    ),
                )
            spec = RESOURCE_SPECS.get(resource)
            if not spec:
                raise ValueError('不支持的资源类型。')
            data = body.get('data')
            if not isinstance(data, dict):
                raise ValueError('data 必须是 JSON 对象。')
            if object_id is None:
                object_id = body.get('id')
            clean = _coerce_resource_data(spec, data)
            if resource == 'spawns':
                for field in ('x', 'y'):
                    if field in clean and not 0 <= clean[field] <= 100:
                        raise ValueError(f'{field} 必须在 0 到 100 之间。')
            model = spec['model']
            with transaction.atomic():
                if resource == 'abilities':
                    enemy_id = clean.get('enemy_id')
                    if not enemy_id and object_id:
                        enemy_id = model.objects.filter(id=object_id).values_list(
                            'enemy_id',
                            flat=True,
                        ).first()
                    spell_id = clean.get('spell_id')
                    if not spell_id and object_id:
                        spell_id = model.objects.filter(id=object_id).values_list(
                            'spell_id',
                            flat=True,
                        ).first()
                    enemy = get_object_or_404(
                        MythicDungeonEnemy.objects.select_related(
                            'dungeon__data_version',
                        ),
                        id=enemy_id,
                    )
                    spell_record, _ = MythicDungeonSpell.objects.get_or_create(
                        data_version=enemy.dungeon.data_version,
                        spell_id=spell_id,
                    )
                    clean['spell_record_id'] = spell_record.id
                    existing_metadata = {}
                    if object_id:
                        existing_metadata = (
                            model.objects.filter(id=object_id).values_list(
                                'metadata',
                                flat=True,
                            ).first()
                            or {}
                        )
                    metadata = dict(existing_metadata)
                    if isinstance(clean.get('metadata'), dict):
                        metadata.update(clean['metadata'])
                    override_fields = set(
                        metadata.get('manual_override_fields') or []
                    )
                    override_fields.update({
                        field
                        for field in (
                            'name',
                            'name_zh',
                            'description',
                            'description_zh',
                            'icon_url',
                        )
                        if field in data
                    })
                    metadata['manual_override_fields'] = sorted(override_fields)
                    clean['metadata'] = metadata
                if resource == 'spells':
                    existing_metadata = {}
                    if object_id:
                        existing_metadata = (
                            model.objects.filter(id=object_id).values_list(
                                'metadata',
                                flat=True,
                            ).first()
                            or {}
                        )
                    metadata = dict(existing_metadata)
                    if isinstance(clean.get('metadata'), dict):
                        metadata.update(clean['metadata'])
                    if {
                        'description',
                        'description_zh',
                        'aura_description',
                        'aura_description_zh',
                    }.intersection(data):
                        metadata.update(build_description_metadata(
                            source=SOURCE_MANUAL,
                            quality=QUALITY_MANUAL_OVERRIDE,
                            tooltip_imported_at=timezone.now().isoformat(),
                        ))
                        metadata['manual_description_updated_by_user_id'] = (
                            request.user.id
                        )
                    clean['metadata'] = metadata
                if resource == 'spawns':
                    existing_spawn = None
                    if object_id:
                        existing_spawn = model.objects.filter(
                            id=object_id,
                        ).first()
                    enemy_id = clean.get(
                        'enemy_id',
                        getattr(existing_spawn, 'enemy_id', None),
                    )
                    floor_id = clean.get(
                        'floor_id',
                        getattr(existing_spawn, 'floor_id', None),
                    )
                    enemy = get_object_or_404(
                        MythicDungeonEnemy,
                        id=enemy_id,
                    )
                    floor = get_object_or_404(
                        MythicDungeonFloor,
                        id=floor_id,
                    )
                    if enemy.dungeon_id != floor.dungeon_id:
                        raise ValueError('关联怪物和楼层必须属于同一个地下城。')
                if object_id:
                    row = get_object_or_404(model, id=object_id)
                    if resource == 'spawns' and {
                        'x',
                        'y',
                        'floor_id',
                    }.intersection(data):
                        metadata = dict(row.metadata or {})
                        if isinstance(clean.get('metadata'), dict):
                            metadata.update(clean['metadata'])
                        if not isinstance(
                            metadata.get('imported_position'),
                            dict,
                        ):
                            metadata['imported_position'] = {
                                'floor_key': row.floor.key,
                                'x': row.x,
                                'y': row.y,
                            }
                        metadata.update({
                            'manual_position_override': True,
                            'manual_position_updated_at': (
                                timezone.now().isoformat()
                            ),
                            'manual_position_updated_by_user_id': (
                                request.user.id
                            ),
                        })
                        clean['metadata'] = metadata
                    for field, value in clean.items():
                        setattr(row, field, value)
                else:
                    row = model(**clean)
                    if isinstance(row, MythicDungeonSpawn):
                        metadata = dict(row.metadata or {})
                        metadata.update({
                            'manual_position_override': True,
                            'manual_position_updated_at': (
                                timezone.now().isoformat()
                            ),
                            'manual_position_updated_by_user_id': (
                                request.user.id
                            ),
                        })
                        row.metadata = metadata
                if isinstance(row, MythicPlannerConfig):
                    row.updated_by_user_id = request.user.id
                row.full_clean()
                row.save()
                if isinstance(row, MythicDungeonDataVersion) and row.is_active:
                    MythicDungeonDataVersion.objects.exclude(id=row.id).filter(is_active=True).update(is_active=False)
            return success(
                {'id': row.id, 'resource': resource},
                snapshot=management_snapshot(
                    snapshot_resources,
                    snapshot_dungeon_id,
                ),
            )
        except (TypeError, ValueError, ValidationError) as exc:
            if isinstance(exc, ValidationError):
                message = '；'.join(
                    f"{field}: {'、'.join(messages)}"
                    for field, messages in exc.message_dict.items()
                ) if hasattr(exc, 'message_dict') else '；'.join(exc.messages)
            else:
                message = str(exc)
            return error(message)

    def delete(self, request, object_id=None):
        try:
            body = parse_body(request)
            resource = str(body.get('resource') or '')
            snapshot_resources = body.get('snapshot_resources')
            snapshot_dungeon_id = body.get('snapshot_dungeon_id')
            spec = RESOURCE_SPECS.get(resource)
            if not spec or resource == 'configs':
                raise ValueError('该资源不能停用。')
            object_id = object_id or body.get('id')
            if not object_id:
                raise ValueError('缺少资源 ID。')
            row = get_object_or_404(spec['model'], id=object_id)
            if not hasattr(row, 'is_active'):
                raise ValueError('该资源不支持停用。')
            row.is_active = False
            if isinstance(row, MythicDungeonDataVersion):
                row.imported_at = row.imported_at or timezone.now()
            row.save(update_fields=['is_active', 'updated_at'])
            return success(
                {'id': row.id, 'resource': resource, 'archived': True},
                snapshot=management_snapshot(
                    snapshot_resources,
                    snapshot_dungeon_id,
                ),
            )
        except (TypeError, ValueError) as exc:
            return error(exc)
