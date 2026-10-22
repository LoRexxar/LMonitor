"""Account-scoped character subscriptions and the daily refresh target set."""
import math
from urllib.parse import quote

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from botend.constants.wow import canonical_class_spec
from botend.models import CharacterBindingQuota, UserCharacterBinding, PlayerSpecTopPlayer, SeasonMeta, WowRealmDirectory
from botend.raiderio_identity import activity_identity_digest, normalized_name
from botend.services.player_raiderio_activity import player_identity, validate_profile_identity

DEFAULT_LIMIT = 5
REGIONS = ('cn', 'tw', 'us', 'eu', 'kr')


def quota(user):
    value = CharacterBindingQuota.objects.filter(user=user).values_list('limit', flat=True).first()
    return DEFAULT_LIMIT if value is None else value


def _lock_user(user):
    # Every quota/binding write locks the SAME pre-existing row, even for accounts
    # that have never configured a quota. No race on get_or_create of settings.
    return get_user_model().objects.select_for_update().get(pk=user.pk)


def _text(value, label, limit):
    if not isinstance(value, str):
        raise ValidationError(f'请填写{label}。')
    value = value.strip()
    if (not value or len(value) > limit or value in ('.', '..')
            or any(c in value for c in '/\\') or any(ord(c) < 32 for c in value)):
        raise ValidationError(f'{label}格式不正确。')
    return value


def resolve_character(name, realm, region=''):
    """Use verified local facts first; a new character must pass source identity validation."""
    name, realm = _text(name, '角色名', 50), _text(realm, '服务器名', 100)
    if region not in ('', *REGIONS):
        raise ValidationError('请选择有效区服。')
    season = SeasonMeta.objects.filter(is_active=True).order_by('pk').first()
    if not season:
        raise ValidationError('当前没有开放的赛季，请稍后再试。')
    directories = WowRealmDirectory.objects.filter(
        Q(slug__iexact=realm) | Q(name__iexact=realm) | Q(localized_name__iexact=realm))
    if region:
        directories = directories.filter(region=region)
    realms = {(row.region, row.slug) for row in directories
              if normalized_name(realm) in {normalized_name(v) for v in (row.slug, row.name, row.localized_name)}}
    candidates = PlayerSpecTopPlayer.objects.filter(season_id=season.pk, character_name__iexact=name)
    if region:
        candidates = candidates.filter(region=region)
    matches = {}
    for player in candidates.order_by('-last_updated', 'pk'):
        if normalized_name(player.character_name) != normalized_name(name):
            continue  # MySQL collations can ignore accents; character identity cannot.
        identity = player_identity(player)
        if not identity or not canonical_class_spec(player.class_name, player.spec_name):
            continue
        if ((identity['region'], identity['realm_slug']) not in realms
                and normalized_name(realm) not in (normalized_name(player.realm), normalized_name(identity['realm_slug']))):
            continue
        matches.setdefault(activity_identity_digest(**identity), player)
    if len(matches) > 1:
        raise ValidationError('该名字和服务器对应多个区服，请选择区服后重试。')
    if matches:
        return next(iter(matches.values()))
    if len(realms) > 1:
        raise ValidationError('服务器名对应多个区服，请选择区服后重试。')
    if realms:
        region, realm = next(iter(realms))
    elif not region:
        raise ValidationError('未找到本地角色，请选择区服后验证；服务器也可填写 Raider.IO 的英文标识。')
    identity = {'region': region, 'realm_slug': realm.lower(), 'character_name': normalized_name(name)}
    cfg = getattr(settings, 'REQUEST_CONFIG', {})
    try:
        response = requests.get('https://raider.io/api/v1/characters/profile',
            params={'region': region, 'realm': realm, 'name': name, 'fields': 'gear,talents'},
            timeout=15, proxies=cfg.get('proxies') if cfg.get('enable_proxy') else None)
        if response.status_code in (400, 404):
            raise ValidationError('未找到该角色，请核对角色名、服务器名和区服。')
        response.raise_for_status()
        data = response.json()
        # Localized realms may be accepted by the provider. Its canonical slug
        # must still resolve through our directory or equal the supplied slug.
        validate_profile_identity(data, identity)
        scope = canonical_class_spec(data.get('class'), data.get('active_spec_name'))
        if not scope:
            raise ValueError('unsupported specialization')
    except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError):
        raise ValidationError('角色数据暂时无法验证，请核对服务器或稍后重试。') from None
    # Unsaved until the account lock has rechecked capacity; failed binding
    # attempts must not create untracked profiles.
    player = PlayerSpecTopPlayer(season_id=season.pk, region=region, realm=identity['realm_slug'],
        character_name=data['name'], class_name=scope[0], spec_name=scope[1], profile_url=data['profile_url'])
    from botend.controller.plugins.portal.SpecDetailPlayerMonitor import SpecDetailPlayerMonitor
    SpecDetailPlayerMonitor(None, None)._apply_raiderio_profile(player, data)
    player.last_updated = timezone.now()
    return player


def bind_character(user, name, realm, region=''):
    if user.character_bindings.count() >= quota(user):
        raise ValidationError('绑定数量已达到上限，请先解绑角色或联系管理员调整上限。')
    player = resolve_character(name, realm, region)
    identity = player_identity(player)
    digest = activity_identity_digest(**identity)
    with transaction.atomic():
        _lock_user(user)
        if user.character_bindings.filter(identity_digest=digest).exists():
            raise ValidationError('该角色已经绑定，请在列表中管理激活状态。')
        if user.character_bindings.count() >= quota(user):
            raise ValidationError('绑定数量已达到上限，请先解绑角色。')
        if player.pk is None:
            player, _ = PlayerSpecTopPlayer.objects.get_or_create(
                season_id=player.season_id, region=player.region, realm=player.realm,
                character_name=player.character_name, spec_name=player.spec_name,
                defaults={field.name: getattr(player, field.name) for field in player._meta.fields
                          if not field.primary_key and field.name not in
                          ('season_id', 'region', 'realm', 'character_name', 'spec_name')})
            if player_identity(player) != identity:
                raise ValidationError('角色名与现有资料存在大小写或重音冲突，请联系管理员核对。')
        return UserCharacterBinding.objects.create(user=user, player=player,
            identity_digest=digest, region=identity['region'], realm_slug=identity['realm_slug'],
            character_name=player.character_name)


def update_binding(user, binding_id, action):
    with transaction.atomic():
        _lock_user(user)
        binding = UserCharacterBinding.objects.get(pk=binding_id, user=user)
        if action == 'unbind':
            binding.delete()
            return
        if action not in ('activate', 'deactivate'):
            raise ValidationError('未知操作。')
        active = action == 'activate'
        if active and not binding.is_active and user.character_bindings.filter(is_active=True).count() >= quota(user):
            raise ValidationError('激活角色数量已达到上限。')
        binding.is_active = active
        binding.save(update_fields=['is_active', 'updated_at'])


def set_quota(user, limit):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 32767:
        raise ValidationError('角色上限必须是 0 到 32767 的整数。')
    with transaction.atomic():
        _lock_user(user)
        if user.character_bindings.count() > limit:
            raise ValidationError('新上限不能少于已绑定数量，请先解绑多余角色。')
        CharacterBindingQuota.objects.update_or_create(user=user, defaults={'limit': limit})


def detail_url(binding):
    player = binding.player
    scope = canonical_class_spec(player.class_name, player.spec_name) if player else None
    if not scope:
        return ''
    return reverse('spec_detail_player_detail', kwargs={
        'class_name': scope[0], 'spec_name': scope[1], 'player_id': player.pk})


def active_targets():
    # Disabled login accounts also stop adding collection targets. Shared
    # characters are refreshed once, even when several accounts bind them.
    return (UserCharacterBinding.objects.filter(is_active=True, user__is_active=True)
            .select_related('player').order_by('identity_digest', 'pk'))


def refresh_bound_characters(monitor, season, refreshed_identities=()):
    seen = set()
    already_refreshed = set(refreshed_identities)
    refreshed = 0
    from utils.log import logger
    for binding in active_targets().iterator():
        if binding.identity_digest in seen:
            continue
        seen.add(binding.identity_digest)
        try:
            identity = {'region': binding.region, 'realm_slug': binding.realm_slug,
                        'character_name': normalized_name(binding.character_name)}
            # Reuse an existing current-season/spec profile without changing rank.
            profiles = PlayerSpecTopPlayer.objects.filter(season_id=season.pk,
                region=binding.region, character_name__iexact=binding.character_name).order_by('-last_updated', 'pk')
            matches = [p for p in profiles if player_identity(p) == identity]
            player = matches[0] if matches else None
            if player is None:
                source = binding.player
                if source is None:
                    continue
                player = PlayerSpecTopPlayer.objects.create(season_id=season.pk,
                    region=binding.region, realm=binding.realm_slug, character_name=binding.character_name,
                    class_name=source.class_name, spec_name=source.spec_name,
                    profile_url=f'https://raider.io/characters/{binding.region}/{quote(binding.realm_slug, safe="")}/{quote(binding.character_name, safe="")}')
            UserCharacterBinding.objects.filter(identity_digest=binding.identity_digest).update(player=player)
            if binding.identity_digest in already_refreshed:
                continue
            data = monitor._fetch_profile_with_activity(player, season)
            if data:
                # Bindings follow the character's active spec. Never repurpose
                # an existing ranking row when the character switches specs.
                scope = canonical_class_spec(data.get('class'), data.get('active_spec_name'))
                if scope and scope != (player.class_name, player.spec_name):
                    target = next((p for p in matches if (p.class_name, p.spec_name) == scope), None)
                    if target is None:
                        target, _ = PlayerSpecTopPlayer.objects.get_or_create(season_id=season.pk,
                            region=binding.region, realm=binding.realm_slug,
                            character_name=binding.character_name, spec_name=scope[1],
                            defaults={'class_name': scope[0], 'profile_url': player.profile_url})
                    player = target
                    UserCharacterBinding.objects.filter(identity_digest=binding.identity_digest).update(player=player)
                monitor._apply_raiderio_profile(player, data)
                guild = data.get('guild')
                if isinstance(guild, dict):
                    player.guild_name = guild.get('name') or player.guild_name
                for scores in data.get('mythic_plus_scores_by_season', []):
                    if scores.get('season') == season.rio_season:
                        score = (scores.get('scores') or {}).get('all')
                        if (player.rank is None and isinstance(score, (int, float))
                                and not isinstance(score, bool) and math.isfinite(score) and score >= 0):
                            player.score = score
                player.last_updated = timezone.now()
                player.save(update_fields=['gear_json', 'item_level', 'achievement_points', 'profile_url',
                    'avatar_url', 'race', 'faction', 'talent_build_code', 'talents_json',
                    'guild_name', 'score', 'last_updated'])
                refreshed += 1
            # Stats have their own success state; one source failing must not
            # discard last-good facts from the other source.
            monitor._crawl_battlenet_stats_for_profile(player)
        except Exception:
            logger.exception('[CharacterBinding] 绑定角色更新失败 id=%s', binding.pk)
    return refreshed
