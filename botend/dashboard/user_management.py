import json
import math
import secrets
import string

from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Group, Permission
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views import View


User = get_user_model()
USER_FIELDS = {
    'username', 'email', 'first_name', 'last_name',
    'is_active', 'is_staff', 'is_superuser', 'password', 'group_ids',
}
CREATE_FIELDS = USER_FIELDS | {'quick_create'}
DETAIL_FIELDS = USER_FIELDS | {'reset_password'}
BOOLEAN_FIELDS = {'is_active', 'is_staff', 'is_superuser'}
QUICK_CREATE_FIELDS = {'username', 'quick_create'}
GROUP_FIELDS = {'name', 'permission_ids'}
GROUP_PERMISSION_APP_LABEL = 'botend'
QUICK_PASSWORD_ALPHABET = string.ascii_letters + string.digits


def _generate_password(length=16):
    required = [
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
    ]
    password = required + [
        secrets.choice(QUICK_PASSWORD_ALPHABET)
        for _ in range(length - len(required))
    ]
    secrets.SystemRandom().shuffle(password)
    return ''.join(password)


def _error(message, *, status=400, errors=None):
    payload = {'status': 'error', 'message': message}
    if errors:
        payload['errors'] = errors
    return JsonResponse(payload, status=status)


def _require_superuser(request):
    if not request.user.is_superuser:
        return _error('仅超级管理员可以管理用户', status=403)
    return None


def _parse_payload(request, *, allowed_fields=USER_FIELDS):
    try:
        payload = json.loads(request.body or b'{}')
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ValidationError('请求 JSON 格式无效')
    if not isinstance(payload, dict):
        raise ValidationError('请求数据必须是对象')
    unknown = sorted(set(payload) - allowed_fields)
    if unknown:
        raise ValidationError(f'不允许的字段：{", ".join(unknown)}')
    for field in BOOLEAN_FIELDS & payload.keys():
        if not isinstance(payload[field], bool):
            raise ValidationError({field: ['必须是布尔值']})
    return payload


def _parse_id_list(payload, field):
    values = payload.get(field, [])
    if not isinstance(values, list) or any(
        isinstance(value, bool) or not isinstance(value, int) for value in values
    ):
        raise ValidationError({field: ['必须是整数 ID 数组']})
    if len(values) != len(set(values)):
        raise ValidationError({field: ['不能包含重复 ID']})
    return values


def _objects_for_ids(model, ids, field):
    objects = list(model.objects.filter(pk__in=ids).order_by('pk'))
    if len(objects) != len(ids):
        found = {obj.pk for obj in objects}
        missing = sorted(set(ids) - found)
        raise ValidationError({field: [f'不存在的 ID：{", ".join(map(str, missing))}']})
    return objects


def _group_permissions_for_ids(ids):
    permissions = list(Permission.objects.filter(
        pk__in=ids,
        content_type__app_label=GROUP_PERMISSION_APP_LABEL,
    ).select_related('content_type').order_by('pk'))
    if len(permissions) != len(ids):
        found = {permission.pk for permission in permissions}
        rejected = sorted(set(ids) - found)
        raise ValidationError({
            'permission_ids': [
                f'不可授予或不存在的权限 ID：{", ".join(map(str, rejected))}'
            ]
        })
    return permissions


def _serialize_user(user):
    return {
        'id': user.pk,
        'username': user.username,
        'email': user.email,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'is_active': user.is_active,
        'is_staff': user.is_staff,
        'is_superuser': user.is_superuser,
        'groups': [
            {'id': group.pk, 'name': group.name}
            for group in user.groups.all().order_by('name', 'pk')
        ],
        'date_joined': user.date_joined.isoformat() if user.date_joined else None,
        'last_login': user.last_login.isoformat() if user.last_login else None,
    }


def _serialize_permission(permission):
    return {
        'id': permission.pk,
        'name': permission.name,
        'codename': permission.codename,
        'app_label': permission.content_type.app_label,
        'model': permission.content_type.model,
    }


def _serialize_group(group):
    return {
        'id': group.pk,
        'name': group.name,
        'permissions': [
            _serialize_permission(permission)
            for permission in group.permissions.all().order_by(
                'content_type__app_label', 'content_type__model', 'codename'
            )
        ],
        'user_count': group.user_set.count(),
    }


def _validation_error_response(exc):
    if hasattr(exc, 'message_dict'):
        return _error('数据校验失败', errors=exc.message_dict)
    return _error('; '.join(exc.messages))


@method_decorator(login_required, name='dispatch')
class DashboardUserListAPIView(View):
    def get(self, request):
        denied = _require_superuser(request)
        if denied:
            return denied

        search = request.GET.get('search', '').strip()
        try:
            page = max(1, int(request.GET.get('page', 1)))
            page_size = min(100, max(10, int(request.GET.get('page_size', 25))))
        except (TypeError, ValueError):
            return _error('分页参数无效')

        users = User.objects.prefetch_related('groups').order_by('-id')
        if search:
            users = users.filter(
                Q(username__icontains=search)
                | Q(email__icontains=search)
                | Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
            )
        total_count = users.count()
        total_pages = max(1, math.ceil(total_count / page_size))
        page = min(page, total_pages)
        start = (page - 1) * page_size
        data = [_serialize_user(user) for user in users[start:start + page_size]]
        return JsonResponse({
            'status': 'success',
            'data': data,
            'total_count': total_count,
            'page': page,
            'page_size': page_size,
            'total_pages': total_pages,
        })

    def post(self, request):
        denied = _require_superuser(request)
        if denied:
            return denied
        try:
            payload = _parse_payload(request, allowed_fields=CREATE_FIELDS)
            quick_create = payload.get('quick_create', False)
            if not isinstance(quick_create, bool):
                raise ValidationError({'quick_create': ['必须是布尔值']})
            if quick_create and set(payload) != QUICK_CREATE_FIELDS:
                raise ValidationError('快捷创建只允许提交用户名')

            username = str(payload.get('username', '')).strip()
            password = _generate_password() if quick_create else payload.get('password')
            if not username:
                raise ValidationError({'username': ['用户名不能为空']})
            if not isinstance(password, str) or not password:
                raise ValidationError({'password': ['密码不能为空']})

            groups = _objects_for_ids(
                Group,
                _parse_id_list(payload, 'group_ids'),
                'group_ids',
            ) if not quick_create else []
            with transaction.atomic():
                user = User(
                    username=username,
                    email='' if quick_create else str(payload.get('email', '')).strip(),
                    first_name='' if quick_create else str(payload.get('first_name', '')).strip(),
                    last_name='' if quick_create else str(payload.get('last_name', '')).strip(),
                    is_active=True if quick_create else payload.get('is_active', True),
                    is_staff=False if quick_create else payload.get('is_staff', False),
                    is_superuser=False if quick_create else payload.get('is_superuser', False),
                )
                validate_password(password, user=user)
                user.set_password(password)
                user.full_clean()
                user.save()
                user.groups.set(groups)
            data = _serialize_user(user)
            if quick_create:
                data['generated_password'] = password
            return JsonResponse({'status': 'success', 'data': data}, status=201)
        except ValidationError as exc:
            return _validation_error_response(exc)
        except IntegrityError:
            return _error('用户名已存在')


@method_decorator(login_required, name='dispatch')
class DashboardUserDetailAPIView(View):
    def patch(self, request, user_id):
        denied = _require_superuser(request)
        if denied:
            return denied
        try:
            payload = _parse_payload(request, allowed_fields=DETAIL_FIELDS)
            reset_password = payload.get('reset_password')
            if 'reset_password' in payload:
                if reset_password is not True:
                    raise ValidationError({'reset_password': ['必须为 true']})
                if set(payload) != {'reset_password'}:
                    raise ValidationError('生成密码重置不能同时修改其他字段')
            with transaction.atomic():
                user = get_object_or_404(User.objects.select_for_update(), pk=user_id)
                groups = None
                if 'group_ids' in payload:
                    groups = _objects_for_ids(
                        Group,
                        _parse_id_list(payload, 'group_ids'),
                        'group_ids',
                    )
                if user.pk == request.user.pk:
                    forbidden = [
                        field for field in ('is_active', 'is_staff', 'is_superuser')
                        if payload.get(field) is False
                    ]
                    if forbidden:
                        raise ValidationError('不能停用当前账号或移除当前账号的管理员权限')

                for field in USER_FIELDS - {'password', 'group_ids'}:
                    if field not in payload:
                        continue
                    value = payload[field]
                    if field in {'username', 'email', 'first_name', 'last_name'}:
                        value = str(value).strip()
                    if field == 'username' and not value:
                        raise ValidationError({'username': ['用户名不能为空']})
                    setattr(user, field, value)

                password = _generate_password() if reset_password else payload.get('password')
                if password is not None:
                    if not isinstance(password, str) or not password:
                        raise ValidationError({'password': ['新密码不能为空']})
                    validate_password(password, user=user)
                    user.set_password(password)

                user.full_clean()
                user.save()
                if groups is not None:
                    user.groups.set(groups)
            if user.pk == request.user.pk and password is not None:
                update_session_auth_hash(request, user)
            data = _serialize_user(user)
            if reset_password:
                data['generated_password'] = password
            response = JsonResponse({'status': 'success', 'data': data})
            if reset_password:
                response['Cache-Control'] = 'no-store'
                response['Pragma'] = 'no-cache'
            return response
        except ValidationError as exc:
            return _validation_error_response(exc)
        except IntegrityError:
            return _error('用户名已存在')


@method_decorator(login_required, name='dispatch')
class DashboardUserGroupListAPIView(View):
    def get(self, request):
        denied = _require_superuser(request)
        if denied:
            return denied
        groups = Group.objects.prefetch_related('permissions__content_type').order_by('name', 'pk')
        permissions = Permission.objects.filter(
            content_type__app_label=GROUP_PERMISSION_APP_LABEL,
        ).select_related('content_type').order_by(
            'content_type__app_label', 'content_type__model', 'codename'
        )
        return JsonResponse({
            'status': 'success',
            'data': [_serialize_group(group) for group in groups],
            'permissions': [_serialize_permission(permission) for permission in permissions],
        })

    def post(self, request):
        denied = _require_superuser(request)
        if denied:
            return denied
        try:
            payload = _parse_payload(request, allowed_fields=GROUP_FIELDS)
            name = str(payload.get('name', '')).strip()
            if not name:
                raise ValidationError({'name': ['用户组名称不能为空']})
            permissions = _group_permissions_for_ids(
                _parse_id_list(payload, 'permission_ids'),
            )
            with transaction.atomic():
                group = Group(name=name)
                group.full_clean()
                group.save()
                group.permissions.set(permissions)
            return JsonResponse({'status': 'success', 'data': _serialize_group(group)}, status=201)
        except ValidationError as exc:
            return _validation_error_response(exc)
        except IntegrityError:
            return _error('用户组名称已存在')


@method_decorator(login_required, name='dispatch')
class DashboardUserGroupDetailAPIView(View):
    def patch(self, request, group_id):
        denied = _require_superuser(request)
        if denied:
            return denied
        try:
            payload = _parse_payload(request, allowed_fields=GROUP_FIELDS)
            with transaction.atomic():
                group = get_object_or_404(Group.objects.select_for_update(), pk=group_id)
                if 'name' in payload:
                    name = str(payload['name']).strip()
                    if not name:
                        raise ValidationError({'name': ['用户组名称不能为空']})
                    group.name = name
                permissions = None
                if 'permission_ids' in payload:
                    permissions = _group_permissions_for_ids(
                        _parse_id_list(payload, 'permission_ids'),
                    )
                group.full_clean()
                group.save()
                if permissions is not None:
                    group.permissions.set(permissions)
            return JsonResponse({'status': 'success', 'data': _serialize_group(group)})
        except ValidationError as exc:
            return _validation_error_response(exc)
        except IntegrityError:
            return _error('用户组名称已存在')
