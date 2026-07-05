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
        flag_name = cls.DEFAULT_FLAG_BY_USAGE.get(usage) or cls.DEFAULT_FLAG_BY_USAGE[cls.USAGE_SIMULATOR]
        version = (
            WowTalentVersion.objects
            .filter(is_active=True, **{flag_name: True})
            .order_by('-updated_at', '-id')
            .first()
        )
        if version:
            return version
        return (
            WowTalentVersion.objects
            .filter(branch='retail', is_active=True, status='active')
            .order_by('-updated_at', '-id')
            .first()
        )

    @classmethod
    def get_active_by_key(cls, key, allow_inactive=False):
        qs = WowTalentVersion.objects.filter(key=str(key or '').strip())
        if not allow_inactive:
            qs = qs.filter(is_active=True)
        version = qs.first()
        if not version:
            raise ValueError(f'Talent version not found or inactive: {key}')
        return version

    @classmethod
    def list_active(cls):
        return list(
            WowTalentVersion.objects
            .filter(is_active=True)
            .order_by('branch', 'major_version', 'key')
        )

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
