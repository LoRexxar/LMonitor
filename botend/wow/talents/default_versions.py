# -*- coding: utf-8 -*-
"""Default talent metadata version bootstrap helpers."""

from __future__ import annotations

from django.utils import timezone


DEFAULT_TALENT_VERSIONS = [
    {
        'key': 'retail',
        'label': '正式服 12.1.0',
        'branch': 'retail',
        'major_version': '12.1.0',
        'current_build': '',
        'is_active': True,
        'is_default_simulator': True,
        'is_default_player_tree': True,
        'is_default_stats': True,
        'status': 'active',
        'source_dir': '.cache/wago_db2_dumps/12.1.0.69283',
        'notes': '正式服当前天赋元数据；稳定 key 与版本号解耦。',
    },
    {
        'key': 'ptr',
        'label': '测试服 12.1.5（待同步）',
        'branch': 'ptr',
        'major_version': '12.1.5',
        'current_build': '',
        'is_active': False,
        'is_default_simulator': False,
        'is_default_player_tree': False,
        'is_default_stats': False,
        'status': 'draft',
        'source_dir': '.cache/wago_db2_dumps/ptr',
        'notes': 'PTR 独立版本槽位；同步真实 12.1.5 DB2 后再启用。',
    },
]


LEGACY_KEYS_BY_STABLE_KEY = {
    # 该数据桶虽曾错误命名为 PTR，实际 branch 和内容都已用于正式服 12.1.0。
    'retail': ('ptr-12.1.0',),
}


def ensure_default_talent_versions(model_class, now=None):
    """Create/update stable retail/PTR slots without coupling keys to versions."""
    now = now or timezone.now()
    ensured = []
    for item in DEFAULT_TALENT_VERSIONS:
        payload = dict(item)
        key = payload.pop('key')
        defaults = dict(payload)
        defaults['activated_at'] = now if payload.get('is_active') else None
        obj = model_class.objects.filter(key=key).first()
        migrated = False
        if obj is None:
            obj = model_class.objects.filter(
                key__in=LEGACY_KEYS_BY_STABLE_KEY.get(key, ()),
                branch=payload['branch'],
            ).order_by('-id').first()
            if obj is not None:
                obj.key = key
                obj.save(update_fields=['key'])
                migrated = True
        created = obj is None
        if created:
            obj = model_class.objects.create(key=key, **defaults)
        changed = created or migrated
        if not created:
            update_fields = []
            for field, value in defaults.items():
                # 保留已同步的真实 build；普通幂等启动不覆盖人工维护的来源备注。
                preserve_existing = field == 'current_build' or (
                    not migrated and field in ('source_dir', 'notes')
                )
                if preserve_existing and getattr(obj, field, ''):
                    continue
                if field == 'activated_at' and getattr(obj, field, None) and value:
                    continue
                if getattr(obj, field) != value:
                    setattr(obj, field, value)
                    update_fields.append(field)
            if update_fields:
                obj.save(update_fields=update_fields)
                changed = True
        ensured.append((obj, created, changed))
    return ensured
