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
        # 可变元数据始终使用分支当前行，旧链接只用来识别所属分支。
        if not allow_inactive and key not in ('retail', 'ptr', 'beta'):
            old = WowTalentVersion.objects.filter(key=str(key or '')).first()
            if old:
                current = WowTalentVersion.objects.filter(key=old.branch, is_active=True).first()
                if current:
                    return current
        if key in ('retail', 'ptr', 'beta') and not allow_inactive:
            current = WowTalentVersion.objects.filter(key=key, is_active=True, status='active').first()
            if current:
                return current
            current = WowTalentVersion.objects.filter(branch=key, is_active=True, status='active').order_by('-activated_at', '-id').first()
            if current:
                return current
        qs = WowTalentVersion.objects.filter(key=str(key or '').strip())
        if not allow_inactive:
            qs = qs.filter(is_active=True)
        version = qs.first()
        if not version:
            raise ValueError(f'Talent version not found or inactive: {key}')
        return version

    @classmethod
    def list_active(cls):
        current = {}
        for version in WowTalentVersion.objects.filter(is_active=True, branch__in=('retail', 'ptr')).order_by('-activated_at', '-updated_at', '-id'):
            if version.branch not in current or version.key == version.branch:
                current[version.branch] = version
        return sorted(current.values(), key=lambda row: row.branch)

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
