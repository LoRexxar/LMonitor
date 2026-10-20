"""仅登录用户可见的团本战斗统计探索页。"""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import render
from django.utils.decorators import method_decorator
from django.utils.dateparse import parse_datetime
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.vary import vary_on_cookie


from botend.services.raid_burst_snapshot import load_published


@method_decorator(never_cache, name='dispatch')
@method_decorator(vary_on_cookie, name='dispatch')
class PortalRaidBurstView(LoginRequiredMixin, View):
    login_url = '/auth/login/'

    def get(self, request):
        payload, scopes = load_published(request.GET.get('scope'))
        response = render(request, 'portal/raid_burst.html', {
            'raid_burst_payload': payload, 'raid_burst_scopes': scopes,
            'raid_burst_updated_at': parse_datetime(payload['generated_at']) if payload else None,
        })
        response['X-Robots-Tag'] = 'noindex, nofollow'
        return response
