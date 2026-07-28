from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import render
from django.utils.decorators import method_decorator
from django.views import View

from botend.dashboard.dashboard import DashboardView


class PortalMythicPlannerView(View):
    """未挂入 Portal 导航的路线规划器直达测试页。"""

    def get(self, request, share_token=''):
        return render(
            request,
            'portal/mythic_planner.html',
            {
                'planner_share_token': str(share_token or ''),
            },
        )


@method_decorator(login_required, name='dispatch')
class DashboardMythicPlannerView(View):
    """仅管理员可访问的大秘境数据维护页。"""

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not (request.user.is_staff or request.user.is_superuser):
            return HttpResponseForbidden('仅管理员可以维护大秘境规划器数据。')
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        context = DashboardView().get_context_data(
            title='MDT 数据与配置',
            page_name='mythic-planner',
            include_stats=False,
        )
        return render(request, 'dashboard/mythic_planner.html', context)


class DashboardMythicPlannerRoutesView(DashboardMythicPlannerView):
    """账号保存路线与 MDT 分享字符串的独立管理页。"""

    def get(self, request):
        context = DashboardView().get_context_data(
            title='账号路线 / MDT 字符串',
            page_name='mythic-planner-routes',
            include_stats=False,
        )
        return render(request, 'dashboard/mythic_planner_routes.html', context)
