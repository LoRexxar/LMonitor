"""B 站评论验证：固定身份、固定动态、短时验证码和可审计的人工核验。"""

import json
import re
import secrets
import time
from datetime import timedelta
from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac

from botend.models import BilibiliAccountBinding, BilibiliBindingChallenge, BilibiliBindingConfig


class BindingError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class CommentSourceError(BindingError):
    def __init__(self, message='暂时无法读取 B 站评论，请稍后重试或申请人工核验。'):
        super().__init__(message, 503)


def numeric_id(value, label='UID'):
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', value.strip()):
        raise BindingError(f'{label}必须是最多 20 位的正整数，请勿填写昵称。')
    return value.strip()


def parse_dynamic_url(value):
    """仅接受确定的动态地址，不请求用户提供的主机或重定向。"""
    if not isinstance(value, str) or len(value) > 500:
        raise BindingError('请填写 B 站动态的完整链接。')
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port:
            raise ValueError
        if parsed.hostname == 't.bilibili.com':
            match = re.fullmatch(r'/([1-9][0-9]{0,19})/?', parsed.path)
        elif parsed.hostname == 'www.bilibili.com':
            match = re.fullmatch(r'/opus/([1-9][0-9]{0,19})/?', parsed.path)
        else:
            match = None
        if not match:
            raise ValueError
    except ValueError:
        raise BindingError('仅支持 https://t.bilibili.com/数字 或 https://www.bilibili.com/opus/数字，请先展开短链接。')
    return f'https://www.bilibili.com/opus/{match[1]}', match[1]


def get_config(*, lock=False):
    BilibiliBindingConfig.objects.get_or_create(pk=1)
    query = BilibiliBindingConfig.objects
    if lock:
        query = query.select_for_update()
    return query.get(pk=1)


def owner_hash(request):
    if 'bilibili_binding_owner' not in request.session:
        request.session['bilibili_binding_owner'] = secrets.token_urlsafe(32)
    return salted_hmac('bilibili-binding-owner', request.session['bilibili_binding_owner'], algorithm='sha256').hexdigest()


def require_enabled(config, request=None):
    if not config.enabled or not config.dynamic_url:
        raise BindingError('B 站账号绑定尚未开放，请联系管理员。', 403)
    if request is not None and not request.user.is_authenticated and not getattr(settings, 'ALLOW_REGISTRATION', True):
        raise BindingError('注册功能已关闭，请登录已有账号后绑定。', 403)


def ensure_identity_available(uid, user_id):
    existing = BilibiliAccountBinding.objects.filter(uid=uid).first()
    if existing and existing.user_id != user_id:
        raise BindingError('该 B 站账号已关联其他网站账号，请联系管理员。', 409)
    existing = BilibiliAccountBinding.objects.filter(user_id=user_id).first() if user_id else None
    if existing and existing.uid != uid:
        raise BindingError('此网站账号已有关联身份，不能直接更换 UID，请联系管理员。', 409)


def create_challenge(request, uid):
    uid = numeric_id(uid)
    owner = owner_hash(request)
    requester = salted_hmac('bilibili-binding-ip', request.META.get('REMOTE_ADDR', ''), algorithm='sha256').hexdigest()
    user_id = request.user.pk if request.user.is_authenticated else None
    now = timezone.now()
    with transaction.atomic():
        config = get_config(lock=True)
        require_enabled(config, request)
        ensure_identity_available(uid, user_id)
        recent = BilibiliBindingChallenge.objects.filter(owner_hash=owner, created_at__gt=now - timedelta(hours=1))
        if recent.filter(created_at__gt=now - timedelta(seconds=30)).exists() or recent.count() >= 10:
            raise BindingError('申请过于频繁，请稍后再试。', 429)
        if BilibiliBindingChallenge.objects.filter(requester_hash=requester, created_at__gt=now - timedelta(hours=1)).count() >= 60:
            raise BindingError('当前网络申请过于频繁，请稍后再试。', 429)
        BilibiliBindingChallenge.objects.filter(owner_hash=owner, status__in=['pending', 'manual', 'verified']).update(status='cancelled')
        return BilibiliBindingChallenge.objects.create(
            user_id=user_id, owner_hash=owner, requester_hash=requester, uid=uid,
            code='账号绑定 LM-' + secrets.token_hex(10).upper(),
            dynamic_url=config.dynamic_url, config_revision=config.revision,
            expires_at=now + timedelta(minutes=config.challenge_minutes),
        )


def owned_challenge(request, challenge_id, *, lock=False):
    query = BilibiliBindingChallenge.objects
    if lock:
        query = query.select_for_update()
    try:
        challenge = query.get(pk=challenge_id, owner_hash=owner_hash(request))
    except (BilibiliBindingChallenge.DoesNotExist, ValidationError, ValueError, TypeError):
        raise BindingError('验证申请不存在或不属于当前会话。', 404)
    expected_user = request.user.pk if request.user.is_authenticated else None
    if challenge.user_id != expected_user:
        raise BindingError('登录身份已变化，请重新发起验证。', 403)
    return challenge


def require_live(challenge, config):
    require_enabled(config)
    if challenge.config_revision != config.revision:
        raise BindingError('绑定设置已更新，请重新获取验证口令。', 409)
    if challenge.expires_at <= timezone.now():
        raise BindingError('验证申请已过期，请重新获取验证口令。', 410)
    if challenge.status not in ('pending', 'manual', 'verified'):
        raise BindingError('此申请已处理，请重新发起验证。', 409)


class BilibiliCommentReader:
    """有界读取公开评论；不接收 Cookie，不自动发布评论，不绕过风控。"""

    def _get(self, path, params):
        try:
            with requests.get(
                'https://api.bilibili.com' + path, params=params,
                headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'},
                timeout=(3, 6), allow_redirects=False, stream=True,
            ) as response:
                if response.status_code != 200:
                    raise CommentSourceError()
                raw = bytearray()
                for chunk in response.iter_content(65536):
                    raw.extend(chunk)
                    if len(raw) > 2 * 1024 * 1024:
                        raise CommentSourceError()
                payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get('code') != 0 or not isinstance(payload.get('data'), dict):
                raise CommentSourceError()
            return payload['data']
        except (requests.RequestException, ValueError, TypeError):
            raise CommentSourceError() from None

    def resolve(self, dynamic_url):
        _, dynamic_id = parse_dynamic_url(dynamic_url)
        data = self._get('/x/polymer/web-dynamic/v1/detail', {'id': dynamic_id})
        item = data.get('item')
        if not isinstance(item, dict) or str(item.get('id_str', '')) != dynamic_id:
            raise CommentSourceError('无法确认动态身份，请管理员检查动态链接。')
        basic = item.get('basic') or {}
        if not isinstance(basic, dict):
            raise CommentSourceError('该动态没有可识别的评论区。')
        try:
            oid = numeric_id(basic.get('comment_id_str', ''), '评论区编号')
            kind = int(basic.get('comment_type', 0))
        except (BindingError, ValueError, TypeError):
            raise CommentSourceError('该动态没有可识别的评论区。') from None
        if kind not in (1, 11, 12, 17):
            raise CommentSourceError('暂不支持该动态的评论类型，请选择普通文字或图文动态。')
        return oid, kind

    def find(self, challenge, max_pages):
        deadline = time.monotonic() + 15
        oid, kind = self.resolve(challenge.dynamic_url)
        for page in range(1, max_pages + 1):
            if time.monotonic() > deadline:
                raise CommentSourceError('本次读取超时，请稍后重试或申请人工核验。')
            data = self._get('/x/v2/reply', {'oid': oid, 'type': kind, 'pn': page, 'ps': 20, 'sort': 0})
            replies = data.get('replies')
            if replies is None:
                if not isinstance(data.get('page'), dict):
                    raise CommentSourceError()
                return None
            if not isinstance(replies, list):
                raise CommentSourceError()
            for reply in replies:
                evidence = match_comment(reply, challenge, oid, kind)
                if evidence:
                    return evidence
            if len(replies) < 20:
                break
        return None


def match_comment(reply, challenge, oid, kind):
    """只认指定评论区内目标 UID 的新一级评论，不接受引用或子回复。"""
    if not isinstance(reply, dict):
        return None
    member = reply.get('member') or {}
    content = reply.get('content') or {}
    if not isinstance(member, dict) or not isinstance(content, dict):
        return None
    try:
        valid = (
            str(reply.get('oid')) == oid and int(reply.get('type', 0)) == kind
            and int(reply.get('root', -1)) == 0 and int(reply.get('parent', -1)) == 0
            and str(member.get('mid')) == challenge.uid
            and content.get('message', '').strip() == challenge.code
            and int(challenge.created_at.timestamp()) <= int(reply.get('ctime', 0)) <= int(challenge.expires_at.timestamp())
        )
        comment_id = numeric_id(str(reply.get('rpid_str') or reply.get('rpid') or ''), '评论编号')
    except (ValueError, TypeError, AttributeError, BindingError):
        return None
    if not valid:
        return None
    return {'comment_id': comment_id, 'nickname': str(member.get('uname') or '')[:100]}


def consume_challenge(challenge, user):
    """调用者持有配置和申请行锁，与注册创建处于同一事务。"""
    if challenge.status != 'verified' or challenge.consumed_at:
        raise BindingError('请先完成 B 站评论验证。', 409)
    ensure_identity_available(challenge.uid, user.pk)
    binding, _ = BilibiliAccountBinding.objects.update_or_create(user=user, defaults={
        'uid': challenge.uid, 'nickname': challenge.nickname,
        'verified_at': timezone.now(), 'verification_method': challenge.verification_method,
        'comment_id': challenge.comment_id,
        'evidence_url': f'{challenge.dynamic_url}#reply{challenge.comment_id}',
        'revoked_at': None, 'revoked_by': None, 'revoke_reason': '',
    })
    challenge.status = 'consumed'
    challenge.user = user
    challenge.consumed_at = timezone.now()
    challenge.save()
    return binding


def finish_verification(challenge, evidence, method='comment', reviewer=None, note=''):
    challenge.status = 'verified'
    challenge.comment_id = evidence['comment_id']
    challenge.nickname = evidence.get('nickname', '')[:100]
    challenge.verified_at = timezone.now()
    challenge.verification_method = method
    challenge.reviewed_by = reviewer
    challenge.review_note = note
    challenge.last_message = '验证通过，请确认并完成绑定。'
    challenge.save()


def check_challenge(request, challenge_id):
    with transaction.atomic():
        config = get_config(lock=True)
        require_enabled(config, request)
        challenge = owned_challenge(request, challenge_id, lock=True)
        require_live(challenge, config)
        if challenge.status == 'verified':
            return challenge
        now = timezone.now()
        if challenge.last_checked_at and (now - challenge.last_checked_at).total_seconds() < config.check_interval_seconds:
            raise BindingError(f'请间隔 {config.check_interval_seconds} 秒后再检查。', 429)
        if challenge.check_count >= 20:
            raise BindingError('本次申请检查次数已达上限，请申请人工核验或重新发起。', 429)
        challenge.last_checked_at = now
        challenge.check_count += 1
        challenge.save(update_fields=['last_checked_at', 'check_count'])
        max_pages = config.max_comment_pages
    # 网络读取不持有数据库锁；返回后重新检查期限、归属和配置，防止并发换码。
    source_error = None
    try:
        evidence = BilibiliCommentReader().find(challenge, max_pages)
    except CommentSourceError as exc:
        evidence = None
        source_error = exc
    with transaction.atomic():
        config = get_config(lock=True)
        challenge = owned_challenge(request, challenge_id, lock=True)
        require_live(challenge, config)
        if challenge.status == 'verified':
            return challenge
        if evidence:
            ensure_identity_available(challenge.uid, challenge.user_id)
            finish_verification(challenge, evidence)
        else:
            challenge.last_message = str(source_error) if source_error else '尚未找到匹配的一级评论，可能正在审核或不在最近扫描范围，请稍后重试或申请人工核验。'
            challenge.save(update_fields=['last_message'])
    if source_error:
        raise source_error
    return challenge


def serialize_binding(binding):
    if not binding:
        return None
    return {'uid': binding.uid, 'nickname': binding.nickname, 'verified_at': binding.verified_at.isoformat(),
            'active': binding.revoked_at is None, 'method': binding.verification_method,
            'profile_url': f'https://space.bilibili.com/{binding.uid}'}


def serialize_challenge(challenge, config):
    if not challenge:
        return None
    status = challenge.status
    if status in ('pending', 'manual', 'verified'):
        if challenge.config_revision != config.revision or not config.enabled:
            status = 'cancelled'
        elif challenge.expires_at <= timezone.now():
            status = 'expired'
    return {'id': str(challenge.pk), 'uid': challenge.uid, 'code': challenge.code,
            'dynamic_url': challenge.dynamic_url, 'status': status,
            'expires_at': challenge.expires_at.isoformat(), 'message': challenge.last_message,
            'nickname': challenge.nickname, 'comment_id': challenge.comment_id}
