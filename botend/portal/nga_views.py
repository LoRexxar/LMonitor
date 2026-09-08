from urllib.parse import urlencode

from django.shortcuts import render
from django.views import View
from django.utils.cache import patch_vary_headers
from django.urls import reverse

from botend.services.nga_browse_service import browse_posts, post_detail


class PortalNgaView(View):
    def get(self, request):
        context = browse_posts(request.GET)
        page = context['page']
        filters = {key: context[key] for key in ('q', 'board', 'sort')}
        context['next_batch'] = page.next_page_number() if page.has_next() else 1
        context['at_end'] = not page.has_next()
        context['batch_query'] = urlencode({**filters, 'page': page.number})
        context['batch_url'] = reverse('portal_nga') + '?' + context['batch_query']
        template = 'portal/_nga_batch.html' if request.headers.get('X-NGA-Batch') == '1' else 'portal/nga.html'
        response = render(request, template, context)
        patch_vary_headers(response, ['X-NGA-Batch'])
        return response


class PortalNgaDetailView(View):
    def get(self, request, article_id):
        context = post_detail(article_id)
        filters = {key: request.GET.get(key, '')[:255] for key in ('q', 'board', 'sort', 'page')}
        context['list_url'] = reverse('portal_nga') + '?' + urlencode(filters) + '#nga-batch'
        return render(request, 'portal/nga_detail.html', context)
