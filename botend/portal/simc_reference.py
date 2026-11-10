"""不加入公开导航的 SimC 专精资料内测页。"""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.http import Http404
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.vary import vary_on_cookie

from botend.services.simc_reference import reference_context


@method_decorator(never_cache, name='dispatch')
@method_decorator(vary_on_cookie, name='dispatch')
class PortalSimcReferenceView(LoginRequiredMixin, View):
    login_url = '/auth/login/'
    http_method_names = ['get', 'head', 'options']

    def get(self, request):
        context = reference_context(request.GET)
        results = context['simulation_results']
        context.update(module_section='reference', module_reference_url=request.get_full_path(),
                       module_rankings_url=reverse('portal_simc_reference_rankings', args=[results[0]['config_id'], results[0]['signature'], 'overall'])
                       if results else reverse('portal_simc_reference_rankings_entry'))
        response = render(request, 'portal/simc_reference.html', context)
        response['X-Robots-Tag'] = 'noindex, nofollow'
        return response


@method_decorator(never_cache, name='dispatch')
@method_decorator(vary_on_cookie, name='dispatch')
class PortalSimcReferenceRankingsView(LoginRequiredMixin, View):
    login_url = '/auth/login/'
    http_method_names = ['get', 'head', 'options']

    def get(self, request, config_id=None, signature=None, scope='overall'):
        from botend.models import SimcReferenceSimulationConfig
        from botend.services.simc_reference_rankings import ranking_page_context, ranking_entry
        if config_id is None:
            entry = ranking_entry(request.GET.get('config'))
            if entry:
                return redirect('portal_simc_reference_rankings', *entry)
            board = {'empty_ranking': True, 'scope_label': '全职业', 'module_section': 'rankings',
                     'module_reference_url': reverse('portal_simc_reference'),
                     'module_rankings_url': reverse('portal_simc_reference_rankings_entry')}
        else:
            config = get_object_or_404(SimcReferenceSimulationConfig.objects.select_related('backend', 'template'), pk=config_id)
            board = ranking_page_context(config, signature, scope)
            if board is None:
                raise Http404('当前配置已更新或不再公开，请刷新页面查看排名')
        response = render(request, 'portal/simc_reference_rankings.html', board)
        response['X-Robots-Tag'] = 'noindex, nofollow'
        return response
