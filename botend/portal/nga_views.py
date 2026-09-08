from urllib.parse import urlencode

from django.shortcuts import render
from django.views import View

from botend.services.nga_browse_service import browse_posts, post_detail


class PortalNgaView(View):
    def get(self, request):
        context = browse_posts(request.GET)
        page = context['page']
        filters = {key: context[key] for key in ('q', 'board', 'sort')}
        if page.has_previous():
            context['previous_url'] = '?' + urlencode({**filters, 'page': page.previous_page_number()})
        if page.has_next():
            context['next_url'] = '?' + urlencode({**filters, 'page': page.next_page_number()})
        return render(request, 'portal/nga.html', context)


class PortalNgaDetailView(View):
    def get(self, request, article_id):
        return render(request, 'portal/nga_detail.html', post_detail(article_id))
