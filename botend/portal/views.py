import os
import re
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.views import View
from django.shortcuts import render
from django.http import HttpResponse

from botend.models import WowSkillDiffReport, WowHotfixReport
from botend.services.wago_report_html import build_wow_skill_diff_fallback_html


def _resolve_portal_report_html_path(report_path):
    """
    Resolve a generated portal report under BASE_DIR/static/portal/reports.

    Runtime Wago reports are stored with content_html_path like
    "portal/reports/foo.html".  This helper intentionally exposes only that
    generated report directory and only .html files; it rejects absolute paths,
    parent-directory segments, backslashes, and paths that resolve outside the
    report root.
    """
    raw_path = str(report_path or '').strip()
    if not raw_path:
        return None
    raw_path = raw_path.lstrip('/')
    if raw_path.startswith('static/'):
        raw_path = raw_path[len('static/'):]
    if raw_path.startswith('portal/reports/'):
        raw_path = raw_path[len('portal/reports/'):]

    if not raw_path or '\\' in raw_path:
        return None
    pure_path = PurePosixPath(raw_path)
    if pure_path.is_absolute() or any(part in ('', '.', '..') for part in pure_path.parts):
        return None
    if pure_path.suffix.lower() != '.html':
        return None

    base_dir = str(getattr(settings, 'BASE_DIR', '') or '')
    static_root = Path(base_dir or os.getcwd()) / 'static' / 'portal' / 'reports'
    report_root = static_root.resolve()
    full_path = (report_root / Path(*pure_path.parts)).resolve()
    try:
        full_path.relative_to(report_root)
    except ValueError:
        return None
    if not full_path.is_file():
        return None
    return full_path


def portal_report_url(content_html_path):
    raw_path = str(content_html_path or '').strip().lstrip('/')
    if raw_path.startswith('static/'):
        raw_path = raw_path[len('static/'):]
    if raw_path.startswith('portal/reports/'):
        raw_path = raw_path[len('portal/reports/'):]
    if not raw_path:
        return ''
    return f'/portal/reports/{raw_path}'


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


class PortalReportFileView(View):
    def get(self, request, report_path):
        full_path = _resolve_portal_report_html_path(report_path)
        if not full_path:
            return HttpResponse('Not Found', status=404)
        try:
            return HttpResponse(full_path.read_bytes(), content_type='text/html; charset=utf-8')
        except Exception:
            return HttpResponse('Not Found', status=404)


class PortalWowHotfixReportView(View):
    def get(self, request, report_id):
        try:
            report_id = int(report_id)
        except Exception:
            return HttpResponse('Not Found', status=404)
        row = WowHotfixReport.objects.filter(id=report_id).first()
        if not row:
            return HttpResponse('Not Found', status=404)
        full_path = _resolve_portal_report_html_path(row.content_html_path)
        if not full_path:
            return HttpResponse('Not Found', status=404)
        try:
            return HttpResponse(full_path.read_bytes(), content_type='text/html; charset=utf-8')
        except Exception:
            return HttpResponse('Not Found', status=404)

def _extract_portal_report_embedded_html(html_text):
    """Extract style/body content from a generated standalone report for safe inline display."""
    text = str(html_text or '')
    styles = '\n'.join(re.findall(r'<style\b[^>]*>.*?</style>', text, flags=re.I | re.S))
    body_match = re.search(r'<body\b[^>]*>(.*?)</body>', text, flags=re.I | re.S)
    body = body_match.group(1) if body_match else text
    return f"{styles}\n{body}".strip()


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
            html_exists = bool(_resolve_portal_report_html_path(html_path))
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
        embedded_html = ''
        if html_exists:
            try:
                full_html_path = _resolve_portal_report_html_path(html_path)
                embedded_html = _extract_portal_report_embedded_html(full_html_path.read_text(encoding='utf-8')) if full_html_path else ''
            except Exception:
                embedded_html = ''
                html_exists = False
        fallback_html = ''
        if not html_exists:
            fallback_html = build_wow_skill_diff_fallback_html(row, page_title=title, server_title=server_title)
        return render(request, 'portal/wow_skill_diff_report.html', {
            'report': row,
            'page_title': title,
            'server_title': server_title,
            'html_exists': html_exists,
            'embedded_html': embedded_html,
            'fallback_html': fallback_html,
            'report_file_url': portal_report_url(html_path),
        })
