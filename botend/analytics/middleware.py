import re

from django.templatetags.static import static
from django.utils.html import format_html


class SiteAnalyticsMiddleware:
    """覆盖普通 HTML 页面；下载、流式文件与沙箱报告保留原有响应策略。"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if (request.method != 'GET' or response.streaming
                or not 200 <= response.status_code < 300
                or response.get('Content-Type', '').split(';')[0].strip() != 'text/html'
                or response.get('Content-Encoding')
                or 'attachment' in response.get('Content-Disposition', '').lower()
                or 'sandbox' in response.get('Content-Security-Policy', '').lower()):
            return response
        body = response.content
        if b'data-site-analytics' in body:
            return response
        script = str(format_html(
            '<script defer src="{}" data-site-analytics="1"></script>',
            static('analytics/collector.js') + '?v=20260928',
        )).encode(response.charset)
        match = re.search(br'</body\s*>', body, re.I)
        if match:
            response.content = body[:match.start()] + script + body[match.start():]
        elif re.search(br'<html[\s>]', body, re.I):
            response.content = body + script
        else:
            return response
        if response.has_header('Content-Length'):
            response['Content-Length'] = str(len(response.content))
        for header in ('ETag', 'Content-MD5'):
            if response.has_header(header):
                del response[header]
        return response
