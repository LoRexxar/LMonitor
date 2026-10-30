# -*- coding: utf-8 -*-
"""Bootstrap server slots, never overwrite their published source identity."""
from __future__ import annotations

from django.db import transaction

BRANCH_LABELS = {'retail': '正式服', 'ptr': 'PTR', 'beta': 'Beta'}
DEFAULT_TALENT_VERSIONS = [
    dict(key=branch, label=label, branch=branch, major_version='', current_build='',
         source_dir='', is_active=False, status='draft',
         is_default_simulator=branch == 'retail',
         is_default_player_tree=branch == 'retail', is_default_stats=branch == 'retail')
    for branch, label in BRANCH_LABELS.items()
]

# This historical key actually contained retail data. Branch, not spelling,
# determines the server; keep its PK and all foreign keys when renaming it.
LEGACY_KEYS_BY_STABLE_KEY = {'retail': ('ptr-12.1.0',)}


@transaction.atomic
def ensure_default_talent_versions(model_class, now=None):
    """Create three inactive slots; existing data/activation is never reset."""
    ensured = []
    for item in DEFAULT_TALENT_VERSIONS:
        defaults = dict(item)
        key = defaults.pop('key')
        obj = model_class.objects.select_for_update().filter(key=key).first()
        migrated = False
        if obj is None:
            obj = model_class.objects.filter(
                key__in=LEGACY_KEYS_BY_STABLE_KEY.get(key, ()), branch=key,
            ).order_by('-id').first()
            if obj is not None:
                obj.key = key
                obj.save(update_fields=['key'])
                migrated = True
        created = obj is None
        if created:
            obj = model_class.objects.create(key=key, **defaults)
        ensured.append((obj, created, created or migrated))
    return ensured
