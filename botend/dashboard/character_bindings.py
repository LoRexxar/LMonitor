"""Server-rendered account and administrator binding management."""
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from botend.constants.wow import CLASS_CN, SPEC_CN, SPEC_ICON, canonical_class_spec
from botend.models import UserCharacterBinding
from botend.services import character_bindings as service


def _rows(queryset):
    rows = list(queryset.select_related('player', 'user'))
    for row in rows:
        row.detail_url = service.detail_url(row)
        player = row.player
        scope = canonical_class_spec(player.class_name, player.spec_name) if player else None
        row.spec_icon = SPEC_ICON.get(scope, '')
        row.spec_label = f'{SPEC_CN.get(scope[1], scope[1])} · {CLASS_CN.get(scope[0], scope[0])}' if scope else '专精待更新'
        row.icon_url = (player.avatar_url if player else '') or row.spec_icon
        row.icon_fallback = row.spec_icon if row.icon_url != row.spec_icon else ''
    return rows


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
def my_characters(request):
    error = ''
    if request.method == 'POST':
        try:
            action = request.POST.get('action')
            if action == 'bind':
                service.bind_character(request.user, request.POST.get('character_name'),
                    request.POST.get('realm'), request.POST.get('region', ''))
            else:
                service.update_binding(request.user, int(request.POST.get('binding_id', '')), action)
            return redirect('my_character_bindings')
        except UserCharacterBinding.DoesNotExist:
            raise Http404 from None
        except (ValueError, TypeError):
            error = '请求参数无效，请刷新页面后重试。'
        except ValidationError as exc:
            error = '；'.join(exc.messages)
    rows = _rows(request.user.character_bindings.all())
    return render(request, 'dashboard/character_bindings.html', {
        'bindings': rows, 'limit': service.quota(request.user),
        'active_count': sum(row.is_active for row in rows), 'error': error,
        'form': request.POST if error else {},
    }, status=400 if error else 200)


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
def binding_admin(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden('仅超级管理员可以管理角色绑定。')
    # Keep old bookmarks working, but always enter the shared Dashboard shell.
    params = {key: request.GET.get(key, '') for key in ('q', 'status', 'page', 'users_page')}
    params['section'] = 'character-bindings'
    dashboard_url = reverse('dashboard') + '?' + urlencode(params)
    if request.method == 'GET' and request.path != reverse('dashboard'):
        return redirect(dashboard_url)
    error = ''
    if request.method == 'POST':
        try:
            user = get_object_or_404(get_user_model(), pk=int(request.POST.get('user_id', '')))
            action = request.POST.get('action')
            if action == 'quota':
                service.set_quota(user, int(request.POST.get('limit', '')))
            else:
                service.update_binding(user, int(request.POST.get('binding_id', '')), action)
            return redirect(dashboard_url + '&saved=1')
        except UserCharacterBinding.DoesNotExist:
            raise Http404 from None
        except (ValueError, TypeError):
            error = '请输入有效的整数。'
        except ValidationError as exc:
            error = '；'.join(exc.messages)
    query = request.GET.get('q', '').strip()[:150]
    state = request.GET.get('status', '')
    users = get_user_model().objects.select_related('character_binding_quota').order_by('pk')
    bindings = UserCharacterBinding.objects.all().order_by('-created_at', '-pk')
    if query:
        users = users.filter(Q(username__icontains=query) | Q(email__icontains=query))
        bindings = bindings.filter(Q(user__username__icontains=query) | Q(character_name__icontains=query)
                                   | Q(realm_slug__icontains=query) | Q(player__realm__icontains=query))
    if state in ('active', 'inactive'):
        bindings = bindings.filter(is_active=state == 'active')
    users = users.annotate(binding_count=Count('character_bindings'),
                           active_count=Count('character_bindings', filter=Q(character_bindings__is_active=True)))
    user_page = Paginator(users, 20).get_page(request.GET.get('users_page'))
    for user in user_page:
        try:
            user.binding_limit = user.character_binding_quota.limit
        except service.CharacterBindingQuota.DoesNotExist:
            user.binding_limit = service.DEFAULT_LIMIT
    binding_page = Paginator(bindings, 30).get_page(request.GET.get('page'))
    binding_page.object_list = _rows(binding_page.object_list)
    from botend.dashboard.dashboard import DashboardView
    from botend.dashboard.permissions import effective_dashboard_permissions, permission_catalog

    dashboard = DashboardView()
    dashboard.setup(request)
    context = dashboard.get_context_data(title='角色绑定管理', include_stats=False)
    context.update({
        'dashboard_permissions': sorted(effective_dashboard_permissions(request.user)),
        'dashboard_permission_catalog': permission_catalog(),
        'dashboard_default_section': 'character-bindings',
        'character_bindings_loaded': True,
        'users': user_page, 'bindings': binding_page, 'q': query, 'state': state,
        'search_params': urlencode({'section': 'character-bindings', 'q': query, 'status': state}),
        'binding_action_url': dashboard_url,
        'error': error,
        'saved': request.GET.get('saved') == '1',
    })
    response = render(request, 'dashboard/index.html', context, status=400 if error else 200)
    response['X-Robots-Tag'] = 'noindex, nofollow'
    return response
