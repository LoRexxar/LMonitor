"""Request-local, account-scoped links for the shared account menus."""
from django import template

from botend.constants.wow import SPEC_ICON, canonical_class_spec
from botend.models import UserCharacterBinding
from botend.services.character_bindings import detail_url

register = template.Library()
MENU_LIMIT = 20


@register.inclusion_tag('shared/_character_shortcuts.html', takes_context=True)
def character_shortcuts(context):
    request = context.get('request')
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return {'shortcuts': [], 'authenticated': False}
    if not hasattr(request, '_character_menu_context'):
        bindings = list(UserCharacterBinding.objects.filter(user=user)
            .select_related('player').only(
                'id', 'user_id', 'character_name', 'realm_slug', 'region', 'is_active', 'player_id',
                'player__id', 'player__class_name', 'player__spec_name', 'player__realm', 'player__avatar_url')
            .order_by('-is_active', 'created_at', 'pk')[:MENU_LIMIT + 1])
        shortcuts = []
        for binding in bindings[:MENU_LIMIT]:
            player = binding.player
            scope = canonical_class_spec(player.class_name, player.spec_name) if player else None
            shortcuts.append({
                'name': binding.character_name,
                'realm': (player.realm if player else '') or binding.realm_slug,
                'region': binding.region.upper(),
                'active': binding.is_active,
                'url': detail_url(binding),
                'icon': (player.avatar_url if player else '') or SPEC_ICON.get(scope, ''),
            })
        request._character_menu_context = {'shortcuts': shortcuts, 'authenticated': True,
                                            'has_more': len(bindings) > MENU_LIMIT}
    return request._character_menu_context
