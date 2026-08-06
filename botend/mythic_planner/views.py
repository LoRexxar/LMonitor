from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.utils.decorators import method_decorator
from django.views import View

from botend.dashboard.permissions import SECTION_PERMISSION_CODES, has_dashboard_permission


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
    """兼容旧管理页 URL，并转入 Dashboard 单页内容区。"""

    dashboard_section = 'mythic-planner-config'

    def dispatch(self, request, *args, **kwargs):
        if not has_dashboard_permission(request.user, SECTION_PERMISSION_CODES[self.dashboard_section]):
            return HttpResponseForbidden('无权访问该 Dashboard 页面。')
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        return redirect(f'/dashboard/?section={self.dashboard_section}')


class DashboardMythicPlannerRoutesView(DashboardMythicPlannerView):
    """兼容账号路线管理页旧 URL。"""

    dashboard_section = 'mythic-planner-routes'


class DashboardMythicPlannerPositionsView(DashboardMythicPlannerView):
    """兼容地图点位管理页旧 URL。"""

    dashboard_section = 'mythic-planner-positions'
