import os

from django.conf import settings
from django.views import View
from django.shortcuts import render
from django.http import HttpResponse

from botend.models import WowSkillDiffReport


class PortalHomeView(View):
    def get(self, request):
        return render(request, 'portal/index.html')


class PortalArticleView(View):
    def get(self, request, article_id):
        try:
            article_id = int(article_id)
        except Exception:
            return HttpResponse('Not Found', status=404)
        return render(request, 'portal/article.html', {'article_id': article_id})


class PortalWowSkillDiffReportView(View):
    def get(self, request, report_id):
        try:
            report_id = int(report_id)
        except Exception:
            return HttpResponse('Not Found', status=404)
        row = WowSkillDiffReport.objects.filter(id=report_id).first()
        if not row:
            return HttpResponse('Not Found', status=404)
        branch = (row.branch or '').strip()
        server_title_map = {
            'wow': 'Retail(正式服)',
            'wow_beta': 'Beta(测试服)',
            'wowt': 'PTR(测试服)',
            'wowxptr': 'PTR X(测试服)',
        }
        server_title = server_title_map.get(branch, branch)
        from_build = (row.display_from_build or row.from_build or '').strip()
        to_build = (row.display_to_build or row.to_build or '').strip()
        md = (row.content_md or '').strip()
        html_exists = False
        html_path = (row.content_html_path or '').strip()
        if html_path:
            static_root = os.path.join(str(getattr(settings, 'BASE_DIR', '') or ''), 'static')
            full_path = os.path.abspath(os.path.join(static_root, html_path))
            static_root_abs = os.path.abspath(static_root)
            html_exists = full_path.startswith(static_root_abs + os.sep) and os.path.isfile(full_path)
        summary = ''
        if md:
            for line in md.splitlines():
                line = (line or '').strip()
                if not line:
                    continue
                if line.startswith('#'):
                    summary = line.lstrip('#').strip()
                    break
        if summary and ('职业技能变更报告' not in summary):
            title = f"{server_title}：{summary}（{from_build} → {to_build}）".strip()
        else:
            title = f"{server_title} 职业技能变更报告：{from_build} → {to_build}".strip()
        return render(request, 'portal/wow_skill_diff_report.html', {
            'report': row,
            'page_title': title,
            'server_title': server_title,
            'html_exists': html_exists,
        })
