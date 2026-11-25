"""Request-local, account-scoped links for the shared account menus."""
from django import template
from django.urls import reverse

from botend.dashboard.permissions import effective_dashboard_permissions, has_dashboard_permission
from botend.constants.wow import SPEC_ICON, canonical_class_spec
from botend.models import UserCharacterBinding
from botend.services.character_bindings import detail_url

register = template.Library()
MENU_LIMIT = 20


@register.simple_tag(takes_context=True)
def can_enter_dashboard(context):
    return bool(effective_dashboard_permissions(getattr(context.get('request'), 'user', None)))


@register.simple_tag(takes_context=True)
def simulation_management_url(context):
    user = getattr(context.get('request'), 'user', None)
    if has_dashboard_permission(user, 'frontend.simulations'):
        return reverse('simc_reference_entry')
    return ''


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
                'player__id', 'player__class_name', 'player__spec_name', 'player__realm')
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
                'icon': SPEC_ICON.get(scope, ''),
            })
        request._character_menu_context = {'shortcuts': shortcuts, 'authenticated': True,
                                            'has_more': len(bindings) > MENU_LIMIT}
    return request._character_menu_context
