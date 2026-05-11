from django.views import View
from django.shortcuts import render
from django.http import HttpResponse

from botend.models import WowSkillDiffReport


class PortalHomeView(View):
    def get(self, request):
        return render(request, 'portal/index.html')


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
        from_build = (row.from_build or '').strip()
        to_build = (row.to_build or '').strip()
        title = f"{server_title} 职业技能变更报告：{from_build} → {to_build}".strip()
        return render(request, 'portal/wow_skill_diff_report.html', {'report': row, 'page_title': title, 'server_title': server_title})
