import json
import math

from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required
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
    'is_active', 'is_staff', 'is_superuser', 'password',
}
BOOLEAN_FIELDS = {'is_active', 'is_staff', 'is_superuser'}


def _error(message, *, status=400, errors=None):
    payload = {'status': 'error', 'message': message}
    if errors:
        payload['errors'] = errors
    return JsonResponse(payload, status=status)


def _require_superuser(request):
    if not request.user.is_superuser:
        return _error('仅超级管理员可以管理用户', status=403)
    return None


def _parse_payload(request):
    try:
        payload = json.loads(request.body or b'{}')
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ValidationError('请求 JSON 格式无效')
    if not isinstance(payload, dict):
        raise ValidationError('请求数据必须是对象')
    unknown = sorted(set(payload) - USER_FIELDS)
    if unknown:
        raise ValidationError(f'不允许的字段：{", ".join(unknown)}')
    for field in BOOLEAN_FIELDS & payload.keys():
        if not isinstance(payload[field], bool):
            raise ValidationError({field: ['必须是布尔值']})
    return payload


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
        'date_joined': user.date_joined.isoformat() if user.date_joined else None,
        'last_login': user.last_login.isoformat() if user.last_login else None,
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

        users = User.objects.all().order_by('-id')
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
            payload = _parse_payload(request)
            username = str(payload.get('username', '')).strip()
            password = payload.get('password')
            if not username:
                raise ValidationError({'username': ['用户名不能为空']})
            if not isinstance(password, str) or not password:
                raise ValidationError({'password': ['密码不能为空']})

            with transaction.atomic():
                user = User(
                    username=username,
                    email=str(payload.get('email', '')).strip(),
                    first_name=str(payload.get('first_name', '')).strip(),
                    last_name=str(payload.get('last_name', '')).strip(),
                    is_active=payload.get('is_active', True),
                    is_staff=payload.get('is_staff', False),
                    is_superuser=payload.get('is_superuser', False),
                )
                validate_password(password, user=user)
                user.set_password(password)
                user.full_clean()
                user.save()
            return JsonResponse({'status': 'success', 'data': _serialize_user(user)}, status=201)
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
            payload = _parse_payload(request)
            with transaction.atomic():
                user = get_object_or_404(User.objects.select_for_update(), pk=user_id)
                if user.pk == request.user.pk:
                    forbidden = [
                        field for field in ('is_active', 'is_staff', 'is_superuser')
                        if payload.get(field) is False
                    ]
                    if forbidden:
                        raise ValidationError('不能停用当前账号或移除当前账号的管理员权限')

                for field in USER_FIELDS - {'password'}:
                    if field not in payload:
                        continue
                    value = payload[field]
                    if field in {'username', 'email', 'first_name', 'last_name'}:
                        value = str(value).strip()
                    if field == 'username' and not value:
                        raise ValidationError({'username': ['用户名不能为空']})
                    setattr(user, field, value)

                password = payload.get('password')
                if password is not None:
                    if not isinstance(password, str) or not password:
                        raise ValidationError({'password': ['新密码不能为空']})
                    validate_password(password, user=user)
                    user.set_password(password)

                user.full_clean()
                user.save()
            if user.pk == request.user.pk and password is not None:
                update_session_auth_hash(request, user)
            return JsonResponse({'status': 'success', 'data': _serialize_user(user)})
        except ValidationError as exc:
            return _validation_error_response(exc)
        except IntegrityError:
            return _error('用户名已存在')
