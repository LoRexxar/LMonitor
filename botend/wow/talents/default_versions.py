# -*- coding: utf-8 -*-
"""Default talent metadata version bootstrap helpers."""

from __future__ import annotations

from django.utils import timezone


DEFAULT_TALENT_VERSIONS = [
    {
        'key': 'retail-12.0.7',
        'label': '正式服 12.0.7',
        'branch': 'retail',
        'major_version': '12.0.7',
        'current_build': '',
        'is_active': False,
        'is_default_simulator': False,
        'is_default_player_tree': True,
        'is_default_stats': True,
        'status': 'inactive',
        'source_dir': '.cache/wago_db2_dumps/latest',
        'notes': '正式服 12.0.7 历史天赋元数据版本，已关闭模拟器入口。',
    },
    {
        'key': 'ptr-12.1.0',
        'label': '正式服 12.1.0',
        'branch': 'retail',
        'major_version': '12.1.0',
        'current_build': '',
        'is_active': True,
        'is_default_simulator': True,
        'is_default_player_tree': False,
        'is_default_stats': False,
        'status': 'active',
        'source_dir': '.cache/wago_db2_dumps/ptr',
        'notes': '正式服 12.1.0 天赋模拟器元数据版本。',
    },
]


def ensure_default_talent_versions(model_class, now=None):
    """Create/update the built-in retail/PTR talent versions idempotently."""
    now = now or timezone.now()
    ensured = []
    for item in DEFAULT_TALENT_VERSIONS:
        payload = dict(item)
        key = payload.pop('key')
        defaults = dict(payload)
        defaults['activated_at'] = now if payload.get('is_active') else None
        obj, created = model_class.objects.get_or_create(key=key, defaults=defaults)
        changed = created
        if not created:
            update_fields = []
            for field, value in defaults.items():
                # 不覆盖已有自定义 source_dir/notes/current_build，只补空值；默认标记和状态要保持可修正。
                if field in ('source_dir', 'notes', 'current_build') and getattr(obj, field, ''):
                    continue
                if getattr(obj, field) != value:
                    setattr(obj, field, value)
                    update_fields.append(field)
            if update_fields:
                obj.save(update_fields=update_fields)
                changed = True
        ensured.append((obj, created, changed))
    return ensured
