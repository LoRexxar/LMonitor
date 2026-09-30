"""账号绑定入口与仅超级管理员可用的配置、核验页面。"""

import json
import uuid
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie

from botend.models import BilibiliAccountBinding, BilibiliBindingChallenge
from botend.services.bilibili_binding import (
    BindingError, BilibiliCommentReader, check_challenge, consume_challenge, create_challenge,
    ensure_identity_available, finish_verification, get_config, numeric_id,
    owned_challenge, owner_hash, parse_dynamic_url, require_enabled, require_live,
    serialize_binding, serialize_challenge,
)


def payload(request):
    try:
        data = json.loads(request.body)
    except (ValueError, TypeError):
        raise BindingError('请求 JSON 格式无效。')
    if not isinstance(data, dict):
        raise BindingError('请求数据必须是对象。')
    return data


@method_decorator(never_cache, name='dispatch')
@method_decorator(ensure_csrf_cookie, name='dispatch')
class BindingViewBase(View):
    def dispatch(self, request, *args, **kwargs):
        try:
            return super().dispatch(request, *args, **kwargs)
        except BindingError as exc:
            return JsonResponse({'status': 'error', 'message': str(exc)}, status=exc.status)
        except IntegrityError:
            return JsonResponse({'status': 'error', 'message': '该身份已被关联或申请已处理，请刷新后重试。'}, status=409)
        except ValidationError:
            return JsonResponse({'status': 'error', 'message': '申请编号或数据格式无效。'}, status=400)


@method_decorator(login_required, name='dispatch')
class BilibiliBindingPage(BindingViewBase):
    def get(self, request):
        return render(request, 'dashboard/bilibili_binding.html')


class BilibiliBindingAPI(BindingViewBase):
    def get(self, request):
        config = get_config()
        user_id = request.user.pk if request.user.is_authenticated else None
        challenge = BilibiliBindingChallenge.objects.filter(owner_hash=owner_hash(request), user_id=user_id).first()
        binding = BilibiliAccountBinding.objects.filter(user_id=user_id).first() if user_id else None
        return JsonResponse({'status': 'success', 'config': {
            'enabled': config.enabled, 'required': config.require_for_registration,
            'instructions': config.instructions, 'check_interval_seconds': config.check_interval_seconds,
            'dynamic_url': config.dynamic_url,
        }, 'binding': serialize_binding(binding), 'challenge': serialize_challenge(challenge, config)})

    def post(self, request):
        data = payload(request)
        action = data.get('action')
        if action == 'create':
            challenge = create_challenge(request, data.get('uid'))
        elif action == 'check':
            challenge = check_challenge(request, data.get('challenge_id'))
        elif action in ('manual', 'confirm', 'cancel'):
            with transaction.atomic():
                config = get_config(lock=True)
                require_enabled(config, request)
                challenge = owned_challenge(request, data.get('challenge_id'), lock=True)
                require_live(challenge, config)
                if action == 'manual':
                    if challenge.status == 'pending':
                        challenge.status = 'manual'
                        challenge.expires_at = timezone.now() + timedelta(hours=24)
                        challenge.last_message = '已申请人工核验，请保留评论。申请将在 24 小时后过期。'
                        challenge.save()
                elif action == 'confirm':
                    if not request.user.is_authenticated:
                        raise BindingError('请在注册表单中完成注册，验证结果会随账号一起保存。')
                    consume_challenge(challenge, request.user)
                else:
                    challenge.status = 'cancelled'
                    challenge.save(update_fields=['status'])
        else:
            raise BindingError('未知操作。')
        return JsonResponse({'status': 'success', 'challenge': serialize_challenge(challenge, get_config())})


class BilibiliAdminBase(BindingViewBase):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_superuser:
            return JsonResponse({'status': 'error', 'message': '仅超级管理员可以管理 B 站绑定。'}, status=403)
        return super().dispatch(request, *args, **kwargs)


class BilibiliBindingAdminPage(BilibiliAdminBase):
    def get(self, request):
        return redirect('/dashboard/?section=bilibili-binding')


CONFIG_FIELDS = ('enabled', 'require_for_registration', 'dynamic_url', 'challenge_minutes',
                 'check_interval_seconds', 'max_comment_pages', 'instructions')


class BilibiliBindingAdminAPI(BilibiliAdminBase):
    def get(self, request):
        config = get_config()
        search = request.GET.get('search', '').strip()[:150]
        challenges = BilibiliBindingChallenge.objects.select_related('user', 'reviewed_by')
        bindings = BilibiliAccountBinding.objects.select_related('user').order_by('-verified_at')
        if search:
            challenges = challenges.filter(Q(uid=search) | Q(user__username__icontains=search))
            bindings = bindings.filter(Q(uid=search) | Q(user__username__icontains=search))
        entries = []
        for item in challenges[:100]:
            row = serialize_challenge(item, config)
            row.update({'username': item.user.username if item.user else '待注册用户',
                        'created_at': item.created_at.isoformat(), 'review_note': item.review_note,
                        'reviewer': item.reviewed_by.username if item.reviewed_by else '',
                        'method': item.verification_method})
            entries.append(row)
        return JsonResponse({'status': 'success', 'config': {field: getattr(config, field) for field in CONFIG_FIELDS},
                             'challenges': entries, 'bindings': [dict(serialize_binding(item), id=item.pk,
                             username=item.user.username, evidence_url=item.evidence_url,
                             revoke_reason=item.revoke_reason) for item in bindings[:100]]})

    def post(self, request):
        data = payload(request)
        action = data.get('action')
        if action == 'save':
            fields = data.get('config')
            if not isinstance(fields, dict) or set(fields) != set(CONFIG_FIELDS):
                raise BindingError('请提交完整的绑定设置。')
            for key in ('enabled', 'require_for_registration'):
                if not isinstance(fields[key], bool):
                    raise BindingError('开关值必须为布尔值。')
            for key, minimum, maximum in [('challenge_minutes', 5, 60), ('check_interval_seconds', 15, 300), ('max_comment_pages', 1, 10)]:
                value = fields[key]
                if type(value) is not int or not minimum <= value <= maximum:
                    raise BindingError(f'{key} 必须在 {minimum} 至 {maximum} 之间。')
            if not isinstance(fields['instructions'], str) or len(fields['instructions']) > 500:
                raise BindingError('操作说明不能超过 500 字。')
            if fields['dynamic_url']:
                fields['dynamic_url'], _ = parse_dynamic_url(fields['dynamic_url'])
            elif fields['dynamic_url'] != '':
                raise BindingError('动态链接格式无效。')
            if fields['enabled'] and not fields['dynamic_url']:
                raise BindingError('开启绑定前请填写动态链接。')
            if fields['require_for_registration'] and not fields['enabled']:
                raise BindingError('要求注册验证时，必须同时开启 B 站绑定。')
            with transaction.atomic():
                config = get_config(lock=True)
                changed = any(getattr(config, key) != value for key, value in fields.items())
                for key, value in fields.items():
                    setattr(config, key, value)
                if changed:
                    config.revision = uuid.uuid4()
                config.save()
            return JsonResponse({'status': 'success', 'message': '设置已保存；发生变更时，进行中的申请需重新发起。'})
        if action == 'probe':
            dynamic_url, _ = parse_dynamic_url(data.get('dynamic_url'))
            reader = BilibiliCommentReader()
            oid, kind = reader.resolve(dynamic_url)
            reader._get('/x/v2/reply', {'oid': oid, 'type': kind, 'pn': 1, 'ps': 1, 'sort': 0})
            return JsonResponse({'status': 'success', 'message': '本次动态与评论接口读取成功。实际用户验证仍以实时读取结果为准。'})
        if action in ('approve', 'reject'):
            note = data.get('note')
            if not isinstance(note, str) or not note.strip() or len(note) > 500:
                raise BindingError('请填写 1 至 500 字核验说明。')
            with transaction.atomic():
                config = get_config(lock=True)
                try:
                    challenge = BilibiliBindingChallenge.objects.select_for_update().get(pk=data.get('challenge_id'))
                except BilibiliBindingChallenge.DoesNotExist:
                    raise BindingError('申请不存在。', 404)
                require_live(challenge, config)
                if challenge.status != 'manual':
                    raise BindingError('只能处理已申请人工核验的记录。', 409)
                if action == 'approve':
                    if data.get('checked') is not True:
                        raise BindingError('请先核对真实评论作者 UID、评论位置及完整口令。')
                    comment_id = numeric_id(data.get('comment_id'), '评论编号')
                    ensure_identity_available(challenge.uid, challenge.user_id)
                    finish_verification(challenge, {'comment_id': comment_id}, 'manual', request.user, note.strip())
                else:
                    challenge.status = 'rejected'
                    challenge.reviewed_by = request.user
                    challenge.review_note = note.strip()
                    challenge.last_message = '人工核验未通过，请确认 UID 和评论内容后重新申请。'
                    challenge.save()
            return JsonResponse({'status': 'success', 'message': '核验结果已保存。'})
        if action == 'revoke':
            binding_id = data.get('binding_id')
            if type(binding_id) is not int or binding_id <= 0:
                raise BindingError('绑定记录编号无效。')
            note = data.get('note')
            if not isinstance(note, str) or not note.strip() or len(note) > 500:
                raise BindingError('请填写撤销原因（最多 500 字）。')
            with transaction.atomic():
                get_config(lock=True)
                binding = BilibiliAccountBinding.objects.select_for_update().filter(pk=binding_id).first()
                if not binding:
                    raise BindingError('绑定记录不存在。', 404)
                binding.revoked_at = timezone.now()
                binding.revoked_by = request.user
                binding.revoke_reason = note.strip()
                binding.save()
                BilibiliBindingChallenge.objects.filter(user=binding.user, status__in=['pending', 'manual', 'verified']).update(status='cancelled')
            return JsonResponse({'status': 'success', 'message': '绑定已撤销，历史身份保留；原账号可重新验证同一 UID。'})
        raise BindingError('未知操作。')
