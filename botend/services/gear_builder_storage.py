"""职业配装器线上字符串与短链接存储服务。"""

from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import io
import json
import secrets

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from botend.models import GearBuilderShareLink, GearBuilderUserLoadout


MAX_ENCODED_STATE_LENGTH = 16000
MAX_DECODED_STATE_LENGTH = 512 * 1024
MAX_USER_LOADOUTS = 50
SHARE_FORMAT_VERSION = 4


class GearBuilderStorageError(ValueError):
    pass


def _decode_state(code):
    code = str(code or '').strip()
    if not code or len(code) > MAX_ENCODED_STATE_LENGTH or code[0] not in ('z', 'j'):
        raise GearBuilderStorageError('配装字符串无效或过长')
    encoded = code[1:]
    try:
        compressed = base64.urlsafe_b64decode(encoded + '=' * ((4 - len(encoded) % 4) % 4))
        if code[0] == 'z':
            with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode='rb') as stream:
                decoded = stream.read(MAX_DECODED_STATE_LENGTH + 1)
        else:
            decoded = compressed
        if len(decoded) > MAX_DECODED_STATE_LENGTH:
            raise GearBuilderStorageError('配装字符串解压后过大')
        payload = json.loads(decoded.decode('utf-8'))
    except GearBuilderStorageError:
        raise
    except (OSError, EOFError, UnicodeDecodeError, ValueError, binascii.Error):
        raise GearBuilderStorageError('配装字符串无法解析')
    try:
        version = int(payload.get('v') or 0) if isinstance(payload, dict) else 0
    except (TypeError, ValueError):
        version = 0
    if not isinstance(payload, dict) or version != SHARE_FORMAT_VERSION:
        raise GearBuilderStorageError('配装字符串版本无效')
    entries = payload.get('e')
    if not isinstance(entries, list) or not entries or len(entries) > 16:
        raise GearBuilderStorageError('配装字符串中的装备槽位数量无效')
    class_name = str(payload.get('c') or '').strip()
    spec_name = str(payload.get('s') or '').strip()
    batch_key = str(payload.get('b') or '').strip()
    if not class_name or len(class_name) > 32 or not spec_name or len(spec_name) > 64:
        raise GearBuilderStorageError('配装字符串缺少有效职业专精')
    if not batch_key or len(batch_key) > 160:
        raise GearBuilderStorageError('配装字符串缺少有效装备批次')
    return code, payload, {
        'class_name': class_name,
        'spec_name': spec_name,
        'batch_key': batch_key,
        'state_hash': hashlib.sha256(code.encode('utf-8')).hexdigest(),
    }


def _loadout_payload(row, include_code=False):
    payload = {
        'id': row.id,
        'name': row.name,
        'class_name': row.class_name,
        'spec_name': row.spec_name,
        'batch_key': row.batch_key,
        'created_at': row.created_at.isoformat(),
        'updated_at': row.updated_at.isoformat(),
    }
    if include_code:
        payload['code'] = row.encoded_state
    return payload


def list_user_loadouts(user):
    return [_loadout_payload(row) for row in GearBuilderUserLoadout.objects.filter(user=user)[:MAX_USER_LOADOUTS]]


def save_user_loadout(user, *, name, code, loadout_id=None):
    name = ' '.join(str(name or '').strip().split())[:80]
    if not name:
        raise GearBuilderStorageError('请输入线上配装名称')
    code, _, metadata = _decode_state(code)
    queryset = GearBuilderUserLoadout.objects.filter(user=user)
    row = None
    if loadout_id:
        try:
            row = queryset.get(id=int(loadout_id))
        except (TypeError, ValueError, GearBuilderUserLoadout.DoesNotExist):
            raise GearBuilderStorageError('线上配装不存在或无权修改')
    if row is None:
        row = queryset.filter(
            class_name=metadata['class_name'], spec_name=metadata['spec_name'], name=name,
        ).first()
    if row is None and queryset.count() >= MAX_USER_LOADOUTS:
        raise GearBuilderStorageError(f'每个账号最多保存 {MAX_USER_LOADOUTS} 套线上配装')
    row = row or GearBuilderUserLoadout(user=user)
    row.name = name
    row.encoded_state = code
    row.state_hash = metadata['state_hash']
    row.class_name = metadata['class_name']
    row.spec_name = metadata['spec_name']
    row.batch_key = metadata['batch_key']
    try:
        with transaction.atomic():
            row.save()
    except IntegrityError:
        raise GearBuilderStorageError('同一职业专精下已存在同名线上配装')
    return _loadout_payload(row, include_code=True)


def get_user_loadout(user, loadout_id):
    try:
        row = GearBuilderUserLoadout.objects.get(user=user, id=int(loadout_id))
    except (TypeError, ValueError, GearBuilderUserLoadout.DoesNotExist):
        raise GearBuilderStorageError('线上配装不存在或无权访问')
    return _loadout_payload(row, include_code=True)


def delete_user_loadout(user, loadout_id):
    deleted, _ = GearBuilderUserLoadout.objects.filter(user=user, id=loadout_id).delete()
    if not deleted:
        raise GearBuilderStorageError('线上配装不存在或无权删除')


def _new_share_token():
    return secrets.token_urlsafe(9)[:12]


def create_short_link(user, code):
    code, _, metadata = _decode_state(code)
    existing = GearBuilderShareLink.objects.filter(
        user=user,
        state_hash=metadata['state_hash'],
        encoded_state=code,
        is_active=True,
    ).first()
    if existing:
        return existing
    for _ in range(8):
        try:
            with transaction.atomic():
                return GearBuilderShareLink.objects.create(
                    user=user,
                    token=_new_share_token(),
                    encoded_state=code,
                    state_hash=metadata['state_hash'],
                    class_name=metadata['class_name'],
                    spec_name=metadata['spec_name'],
                    batch_key=metadata['batch_key'],
                )
        except IntegrityError:
            continue
    raise GearBuilderStorageError('短链接生成失败，请稍后重试')


def resolve_short_link(token):
    token = str(token or '').strip()
    if not token or len(token) > 16:
        raise GearBuilderStorageError('短链接不存在或已失效')
    try:
        row = GearBuilderShareLink.objects.get(token=token, is_active=True)
    except GearBuilderShareLink.DoesNotExist:
        raise GearBuilderStorageError('短链接不存在或已失效')
    now = timezone.now()
    GearBuilderShareLink.objects.filter(id=row.id).update(
        access_count=F('access_count') + 1,
        last_accessed_at=now,
    )
    return {
        'token': row.token,
        'code': row.encoded_state,
        'class_name': row.class_name,
        'spec_name': row.spec_name,
        'created_at': row.created_at.isoformat(),
    }
