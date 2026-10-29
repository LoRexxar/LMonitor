# -*- coding: utf-8 -*-
"""Talent metadata version resolution helpers."""

from __future__ import annotations

from botend.models import WowTalentVersion


class TalentVersionResolver:
    """Resolve the talent metadata version used by render/encode paths."""

    USAGE_SIMULATOR = 'simulator'
    USAGE_PLAYER_TREE = 'player_tree'
    USAGE_STATS = 'stats'

    DEFAULT_FLAG_BY_USAGE = {
        USAGE_SIMULATOR: 'is_default_simulator',
        USAGE_PLAYER_TREE: 'is_default_player_tree',
        USAGE_STATS: 'is_default_stats',
    }

    @classmethod
    def resolve(cls, version_key='', usage=USAGE_SIMULATOR, allow_inactive=False):
        version_key = str(version_key or '').strip()
        if version_key:
            return cls.get_active_by_key(version_key, allow_inactive=allow_inactive)
        return cls.get_default(usage)

    @classmethod
    def get_default(cls, usage=USAGE_SIMULATOR):
        # 使用场景不能改变数据分支；测试服必须由调用者明确指定。
        try:
            return cls.get_active_by_key('retail')
        except ValueError:
            return None

    @classmethod
    def get_active_by_key(cls, key, allow_inactive=False):
        from .default_versions import BRANCH_LABELS
        key = str(key or '').strip()
        # Importers explicitly access isolated staging rows. Public old links
        # identify a server, never resurrect a retired tree or cross servers.
        if allow_inactive:
            version = WowTalentVersion.objects.filter(key=key).first()
        else:
            if key not in BRANCH_LABELS:
                old = WowTalentVersion.objects.filter(key=key).first()
                if old:
                    key = old.branch
                else:
                    import re
                    from .default_versions import LEGACY_KEYS_BY_STABLE_KEY
                    alias = next((branch for branch, keys in LEGACY_KEYS_BY_STABLE_KEY.items()
                                  if key in keys), '')
                    legacy = re.fullmatch(r'(retail|ptr|beta)-\d+(?:\.\d+)*', key)
                    key = alias or (legacy.group(1) if legacy else '')
            version = WowTalentVersion.objects.filter(
                key=key, branch=key, is_active=True, status='active',
            ).first() if key in BRANCH_LABELS else None
        if not version:
            raise ValueError(f'Talent version not found or inactive: {key}')
        return version

    @classmethod
    def list_active(cls):
        from .default_versions import BRANCH_LABELS
        versions = WowTalentVersion.objects.filter(
            key__in=BRANCH_LABELS, is_active=True, status='active')
        current = {v.key: v for v in versions if v.key == v.branch}
        return [current[key] for key in BRANCH_LABELS if key in current]

    @classmethod
    def serialize(cls, version):
        if not version:
            return None
        return {
            'key': getattr(version, 'key', '') or '',
            'label': getattr(version, 'label', '') or '',
            'branch': getattr(version, 'branch', '') or '',
            'major_version': getattr(version, 'major_version', '') or '',
            'current_build': getattr(version, 'current_build', '') or '',
            'status': getattr(version, 'status', '') or '',
            'is_active': bool(getattr(version, 'is_active', False)),
        }

    @classmethod
    def resolve_for_player_record(cls, player):
        return cls.get_default(cls.USAGE_PLAYER_TREE)

    @classmethod
    def resolve_for_ranking_record(cls, record):
        return cls.get_default(cls.USAGE_STATS)
