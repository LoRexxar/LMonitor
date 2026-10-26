"""攻略公开页面只读取后台发布的私有快照。"""
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.utils.html import format_html
from django.views import View

from botend.dashboard.permissions import has_dashboard_permission
from botend.services.class_guide_snapshot import read_guide_page


class PortalGuidePageMixin:
    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        response['Cache-Control'] = 'private, no-store'
        response['X-Robots-Tag'] = 'noindex, nofollow'
        return response

    def snapshot_response(self, request, guide_id=None):
        try:
            page = read_guide_page(guide_id)
        except (OSError, ValueError, KeyError, TypeError):
            response = render(request, 'portal/class_guide_pending.html', {
                'title': '职业攻略', 'can_manage_guides': has_dashboard_permission(request.user, 'content.class-guides'),
            }, status=503)
            response['Retry-After'] = '15'
            return response
        if page is None:
            raise Http404
        body, (start, end, offset) = page
        if request.user.is_authenticated:
            header = render_to_string('portal/_header.html', request=request).encode('utf-8')
            management = b''
            if has_dashboard_permission(request.user, 'content.class-guides'):
                url = '/dashboard/?section=class-guides' + (f'&guide={guide_id}' if guide_id is not None else '')
                management = format_html('<a href="{}">{}</a>', url,
                                         '编辑攻略' if guide_id is not None else '管理攻略').encode('utf-8')
            body = body[:start] + header + body[end:offset] + management + body[offset:]
        return HttpResponse(body, content_type='text/html; charset=utf-8')


class PortalClassGuideCatalogView(PortalGuidePageMixin, View):
    def get(self, request):
        return self.snapshot_response(request)


class PortalClassGuideArticleView(PortalGuidePageMixin, View):
    def get(self, request, guide_id):
        return self.snapshot_response(request, guide_id)
