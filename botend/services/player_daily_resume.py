"""人物日更只保留失败身份引用，恢复与调度共用来源的退避时钟。"""
from types import SimpleNamespace

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from botend.models import RaiderIOProfileCache
from botend.raiderio_identity import activity_identity_digest
from botend.services.player_raiderio_activity import player_identity


def reference(player):
    result = {key: player.get(key) for key in ('region', 'realm', 'name', 'profile_url')}
    if not all(isinstance(result[key], str) and result[key].strip()
               for key in ('region', 'realm', 'name')):
        return None
    if player.get('_profile_required_after'):
        result['required_after'] = player['_profile_required_after']
    return result


def digest(ref):
    identity = player_identity(SimpleNamespace(region=ref['region'], realm=ref['realm'],
        character_name=ref['name'], profile_url=ref.get('profile_url')))
    return activity_identity_digest(**identity) if identity else None


def deadlines(season, refs, *, binding_digests=()):
    """只读租约、失败时钟等元数据，不加载缓存中的装备和天赋。"""
    from botend.services.character_enrichment import source_backoff
    keys = [digest(ref) for ref in refs] + list(binding_digests)
    rows = {row['identity_digest']: row for row in RaiderIOProfileCache.objects.filter(
        season=season, identity_digest__in={key for key in keys if key}).values(
            'identity_digest', 'lease_expires_at', 'next_attempt_at')}
    now, source_until = timezone.now(), source_backoff()
    result = []
    for key in keys:
        row = rows.get(key, {})
        clocks = [row.get('lease_expires_at'), row.get('next_attempt_at')]
        # 来源失败项遵守全局冷却；纯本地写入失败仍允许复用有效缓存。
        if row.get('next_attempt_at') or not row:
            clocks.append(source_until)
        result.append(max([now, *(clock for clock in clocks if clock)]))
    return result


def restore(monitor, season, class_name, spec_name, refs):
    due_times = deadlines(season, refs)
    now = timezone.now()
    players, waiting = [], []
    for ref, due in zip(refs, due_times):
        if due > now:
            waiting.append(ref)
            continue
        player = {key: ref.get(key) for key in ('region', 'realm', 'name', 'profile_url')}
        player.update(_class_name=class_name, _spec_name=spec_name)
        if ref.get('required_after'):
            player['_profile_required_after'] = ref['required_after']
        players.append(player)
    monitor._activity_season = season
    if players:
        monitor._reuse_saved_profiles(players, class_name, spec_name)
        monitor._enrich_talents(players)
    return players, waiting


def required_after(value):
    if not value:
        return None
    parsed = parse_datetime(value)
    if parsed is None or timezone.is_naive(parsed):
        raise ValueError('人物恢复时间无效')
    return parsed
