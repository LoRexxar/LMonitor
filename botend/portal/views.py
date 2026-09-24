import json
import os
import re
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.views import View
from django.shortcuts import render
from django.http import HttpResponse, JsonResponse

from botend.models import WowSkillDiffReport, WowHotfixReport
from botend.controller.plugins.wow.wago_regions import wago_region_name
from botend.services.wago_report_html import build_wow_skill_diff_fallback_html
from botend.services.wow_skill_report_metadata import build_report_spell_metadata, build_hotfix_report_spell_metadata


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


class PortalSimcBenchmarkResultsView(View):
    """Standalone Portal shell for published SimC benchmark results."""

    def get(self, request, panel_id=None):
        return render(request, 'portal/simc_benchmark_results.html', {
            'benchmark_panel_id': panel_id,
        })


class PortalNewsView(View):
    def get(self, request):
        return render(request, 'portal/news.html')


class PortalSpecsView(View):
    def get(self, request):
        return render(request, 'portal/specs.html')


class PortalMplusDpsRankingsView(View):
    def get(self, request):
        return render(request, 'portal/mplus_dps_rankings.html')


class PortalWowUpdatesView(View):
    def get(self, request):
        return render(request, 'portal/wow_updates.html')


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
            content = full_path.read_text(encoding='utf-8')
            if full_path.name.startswith(('wow_hotfix_full_', 'wow_hotfix_fallback_')):
                hotfix = WowHotfixReport.objects.filter(
                    content_html_path__endswith='/' + full_path.name,
                ).first()
                if not hotfix or not hotfix.collection_complete:
                    content = _legacy_hotfix_warning_html(content)
            if full_path.name.startswith('wow_skill_diff_'):
                is_hotfix = bool(re.search(r'_hotfix_r\d+_p\d+\.html$', full_path.name))
                report = (
                    WowHotfixReport.objects.filter(
                        class_content_html_path__endswith='/' + full_path.name, collection_complete=True,
                    ).first()
                    if is_hotfix else
                    WowSkillDiffReport.objects.filter(content_html_path__endswith='/' + full_path.name).first()
                )
                if is_hotfix and (not report or not report.collection_complete or not report.class_spell_count):
                    return HttpResponse('Not Found', status=404)
                if report:
                    branch = report.branch if report.branch in ('wow', 'wowt', 'wowxptr', 'wow_beta') else 'wow'
                    metadata_url = (
                        f'/portal/api/wow-hotfix-class/{report.id}/metadata/' if is_hotfix else
                        f'/portal/api/wow-skill-diff/{report.id}/metadata/'
                    )
                    enhancement = (
                        f'<div data-report-branch="{branch}" data-skill-report-metadata="{metadata_url}"></div>'
                        '<link rel="stylesheet" href="/static/portal/css/wow-skill-report-metadata.css?v=20260924_hotfix_source">'
                        '<script src="/static/portal/js/wow-skill-report-metadata.js?v=20260924_hotfix_direct_effect"></script>'
                    )
                    content = content.replace('</body>', enhancement + '</body>', 1) if '</body>' in content else content + enhancement
            return HttpResponse(content, content_type='text/html; charset=utf-8')
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
            content = full_path.read_text(encoding='utf-8')
            if not row.collection_complete:
                content = _legacy_hotfix_warning_html(content)
            return HttpResponse(content, content_type='text/html; charset=utf-8')
        except Exception:
            return HttpResponse('Not Found', status=404)


def _legacy_hotfix_warning_html(content):
    warning = (
        '<section role="alert" style="position:relative;z-index:1000;padding:16px;'
        'background:#fff1db;color:#713f12;border:2px solid #eab308;font:600 16px/1.6 sans-serif">'
        '历史数值未核实：这份旧 Hotfix 报告未保存同区域热修前后 payload；'
        '其中的 DB2 基表记录不能当作热修前态、生效值或加强/削弱依据。'
        '</section>'
    )
    return re.sub(r'<body\b[^>]*>', lambda match: match.group(0) + warning,
                  content, count=1, flags=re.I) if re.search(r'<body\b', content, re.I) else warning + content

class PortalWowHotfixClassReportView(View):
    """Verified class projection of a Hotfix; a push is not a build."""

    def get(self, request, report_id):
        row = WowHotfixReport.objects.filter(id=report_id).first()
        if not row or not row.collection_complete or not row.class_spell_count:
            return HttpResponse('Not Found', status=404)
        path = _resolve_portal_report_html_path(row.class_content_html_path)
        if not path:
            return HttpResponse('Not Found', status=404)
        try:
            embedded_html = _extract_portal_report_embedded_html(path.read_text(encoding='utf-8'))
        except OSError:
            return HttpResponse('Not Found', status=404)
        server_title = wago_region_name(row.region_id) or f'region {row.region_id}'
        title = f'{server_title} Hotfix 职业更新：push {row.from_push} → {row.to_push}'
        return render(request, 'portal/wow_skill_diff_report.html', {
            'report': row, 'is_hotfix': True, 'page_title': title,
            'server_title': server_title, 'html_exists': True,
            'embedded_html': embedded_html, 'fallback_html': '',
            'report_file_url': portal_report_url(row.class_content_html_path),
            'metadata_url': f'/portal/api/wow-hotfix-class/{row.id}/metadata/',
        })


class PortalWowHotfixClassMetadataAPIView(View):
    def get(self, request, report_id):
        row = WowHotfixReport.objects.filter(id=report_id).first()
        if not row or not row.collection_complete or not row.class_spell_count:
            return JsonResponse({'error': '报告不存在'}, status=404)
        path = _resolve_portal_report_html_path(row.class_content_html_path)
        if not path:
            return JsonResponse({'error': '报告文件不存在'}, status=404)
        try:
            content = path.read_text(encoding='utf-8')
        except OSError:
            return JsonResponse({'error': '报告文件不可读'}, status=404)
        try:
            facts = json.loads(row.source_facts_json)
            if not isinstance(facts, list) or not facts:
                raise ValueError('Hotfix source facts missing')
            spells = build_hotfix_report_spell_metadata(content, row.branch, facts)
        except (TypeError, ValueError):
            return JsonResponse({'error': '热修元数据来源不可验证'}, status=503)
        return JsonResponse({'spells': spells, 'branch': row.branch,
                             'unresolved_count': row.class_unresolved_count})


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
            'metadata_url': f'/portal/api/wow-skill-diff/{row.id}/metadata/',
        })


class PortalWowSkillDiffMetadataAPIView(View):
    """旧报告和新报告共用的展示补全，仅接受数据库中已有报告的技能 ID。"""

    def get(self, request, report_id):
        report = WowSkillDiffReport.objects.filter(id=report_id).first()
        if not report:
            return JsonResponse({'error': '报告不存在'}, status=404)
        path = _resolve_portal_report_html_path(report.content_html_path)
        if not path:
            return JsonResponse({'spells': {}, 'branch': report.branch})
        try:
            content = path.read_text(encoding='utf-8')
        except OSError:
            return JsonResponse({'spells': {}, 'branch': report.branch})
        return JsonResponse({'spells': build_report_spell_metadata(content, report.branch, report.to_build), 'branch': report.branch})
