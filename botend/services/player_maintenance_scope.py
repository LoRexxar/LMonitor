"""人物主动维护范围：当前Top20或有效账号绑定，不扩展到普通统计样本。"""
from botend.models import PlayerSpecTopPlayer, UserCharacterBinding
from botend.raiderio_identity import activity_identity_digest


def active_bindings():
    return UserCharacterBinding.objects.filter(is_active=True, user__is_active=True)


def maintained(player):
    if getattr(player, '_top20_snapshot', False) is True:
        return True
    rank = getattr(player, 'rank', None)
    if type(rank) is int and 1 <= rank <= 20:
        return True
    from botend.services.player_raiderio_activity import player_identity
    value = player_identity(player)
    return bool(value and active_bindings().filter(identity_digest=activity_identity_digest(**value)).exists())


def maintained_digests(season):
    from botend.services.player_raiderio_activity import player_identity
    values = set(active_bindings().values_list('identity_digest', flat=True))
    players = PlayerSpecTopPlayer.objects.filter(season_id=season.pk, rank__gte=1, rank__lte=20).only(
        'region', 'realm', 'character_name', 'profile_url')
    values.update(activity_identity_digest(**value) for player in players if (value := player_identity(player)))
    return values
