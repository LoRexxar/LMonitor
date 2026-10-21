"""仅登录用户可见的团本战斗统计探索页。"""

import re
from urllib.parse import urlencode

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import render
from django.urls import reverse
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
        selection = request.GET.get('scope', '')
        match = re.fullmatch(r'([1-9]\d*)/([A-Za-z]+)/([A-Za-z]+)/([45])/([1-9]\d*)', selection)
        payload, _ = load_published(selection) if match else (None, [])
        back_url = None
        if match:
            _, class_name, spec_name, difficulty, boss_id = match.groups()
            back_url = reverse('spec_detail_raid', kwargs={
                'class_name': class_name, 'spec_name': spec_name,
            }) + '?' + urlencode({'boss_id': boss_id, 'difficulty': difficulty})
        response = render(request, 'portal/raid_burst.html', {
            'raid_burst_payload': payload,
            'raid_burst_back_url': back_url,
            'raid_burst_title': payload['scope']['label'] if payload else (
                f'首领 {boss_id} · {"史诗" if difficulty == "5" else "英雄"} · {class_name} {spec_name}'
                if match else '未指定首领'),
            'raid_burst_updated_at': parse_datetime(payload['generated_at']) if payload else None,
        }, status=200 if match else 400)
        response['X-Robots-Tag'] = 'noindex, nofollow'
        return response
