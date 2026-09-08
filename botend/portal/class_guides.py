"""攻略目录及文章阅读的 Portal 界面，测试阶段沿用后台攻略权限。"""
import re
from urllib.parse import urlencode

from django.db.models import F, Prefetch
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.utils.html import strip_tags
from django.utils.dateparse import parse_date
from django.views import View

from botend.constants.wow import CLASS_CN, CLASS_COLOR, SPEC_ROLE, specialization_catalog
from botend.dashboard.permissions import DashboardPermissionRequiredMixin
from botend.guide_models import ClassGuide, ClassGuideRevision
from botend.services.class_guide_content import safe_url, walk_blocks
from botend.services.class_guide_markdown import compile_markdown
from botend.services.class_guide_render import render_blocks, selected_revision
from botend.services.class_guide_service import audit_revision
from botend.services.class_guide_tools import gear_tool_url
from botend.services.class_guide_authors import author_profile_for
from botend.services.class_guide_tags import guide_disclaimers


class GuidePreviewAccess(DashboardPermissionRequiredMixin):
    dashboard_permission = 'content.class-guides'

    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        response['Cache-Control'] = 'private, no-store'
        response['X-Robots-Tag'] = 'noindex, nofollow'
        return response


def article_updated(guide, revision):
    """来源文章沿用原文日期；站内原创使用本地修订时间。"""
    if guide.source_url or revision.source_modified:
        try:
            return parse_date(revision.source_modified[:10])
        except ValueError:
            return None
    return revision.created_at


def catalog_rows(query):
    latest = ClassGuideRevision.objects.order_by('-number').only(
        'id', 'guide_id', 'title', 'created_at', 'source_modified').annotate(
        cover=F('source_payload__featuredImage'), manual_conflict=F('audit__manual_conflict'))
    query = query.prefetch_related('tags',
        Prefetch('revisions', queryset=latest[:1], to_attr='latest_entries'),
        Prefetch('revisions', queryset=latest.filter(origin='manual')[:1], to_attr='manual_entries'))
    specs = {s['spec_id']: s for s in specialization_catalog()}
    rows = []
    for guide in query:
        if not guide.latest_entries:
            continue
        revision = guide.latest_entries[0]
        if revision.manual_conflict and guide.manual_entries:
            revision = guide.manual_entries[0]
        rows.append({'guide': guide, 'title': revision.title, 'spec': specs[guide.spec_id],
                     'author_name': author_profile_for(guide)['name'],
                     'tags': list(guide.tags.all()), 'cover': safe_url(revision.cover or ''),
                     'updated': article_updated(guide, revision), 'role': SPEC_ROLE[(guide.class_name, guide.spec_name)]})
    return rows


class PortalClassGuideCatalogView(GuidePreviewAccess, View):
    def get(self, request):
        rows = catalog_rows(ClassGuide.objects.filter(archived=False).order_by('class_name', 'spec_name', 'title'))
        classes = [{'key': key, 'label': label, 'color': CLASS_COLOR[key],
                    'icon': f'https://render.worldofwarcraft.com/us/icons/56/classicon_{key.lower()}.jpg',
                    'count': sum(row['guide'].class_name == key for row in rows)} for key, label in CLASS_CN.items()]
        return render(request, 'portal/class_guides.html', {
            'rows': rows, 'classes': classes, 'specializations': specialization_catalog(),
            'tags': sorted({tag.name for row in rows for tag in row['tags']}),
            'roles': [('tank', '坦克'), ('healer', '治疗'), ('dps', '输出')],
            'title': '职业攻略',
        })


class PortalClassGuideArticleView(GuidePreviewAccess, View):
    def get(self, request, guide_id):
        guide = get_object_or_404(ClassGuide.objects.prefetch_related('tags'), pk=guide_id, archived=False)
        revision_id = request.GET.get('revision')
        if revision_id:
            if not revision_id.isascii() or not revision_id.isdigit():
                raise Http404
            revision = get_object_or_404(guide.revisions, pk=revision_id)
        else:
            revision = selected_revision(guide)
        if not revision:
            raise Http404
        # 正文与目录均从该篇 Markdown 生成，目录不是单独维护的内容源。
        revision.blocks = compile_markdown(revision.content_markdown)
        audit = audit_revision(revision)
        blocks = render_blocks(revision.blocks, audit['references'], guide)
        toc = [{'id': b['id'], 'title': strip_tags(b['title']), 'level': b['data']['level']}
               for b in walk_blocks(blocks) if b['type'] == 'heading']
        spec = next(s for s in specialization_catalog() if s['spec_id'] == guide.spec_id)
        query = urlencode({'class': guide.class_name, 'spec': guide.spec_name, 'version': guide.game_version})
        gear_url = next((b['data']['tool_url'] for b in walk_blocks(blocks)
                         if b['type'] == 'gear' and b.get('data', {}).get('tool_url')), gear_tool_url({}, guide, {}))
        return render(request, 'portal/class_guide_article.html', {
            'title': revision.title, 'guide': guide, 'revision': revision, 'spec': spec,
            'author_profile': author_profile_for(guide),
            'disclaimers': guide_disclaimers(guide),
            'updated': article_updated(guide, revision),
            'cover': safe_url(revision.source_payload.get('featuredImage', '')),
            'blocks': blocks, 'toc': toc, 'read_minutes': max(1, len(re.sub(r'\s', '', strip_tags(revision.content_markdown))) // 500),
            'related': catalog_rows(ClassGuide.objects.filter(spec_id=guide.spec_id, archived=False).order_by('title')),
            'talent_url': '/portal/talents/?' + query, 'gear_url': gear_url,
        })
