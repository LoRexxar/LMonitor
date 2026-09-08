"""攻略目录及文章阅读的 Portal 界面。"""
import re
from urllib.parse import urlencode

from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.utils.html import strip_tags
from django.utils.dateparse import parse_date
from django.views import View

from botend.constants.wow import CLASS_CN, CLASS_COLOR, SPEC_ROLE, specialization_catalog
from botend.dashboard.permissions import DashboardPermissionRequiredMixin
from botend.guide_models import ClassGuide
from botend.services.class_guide_content import safe_url, walk_blocks
from botend.services.class_guide_markdown import compile_markdown
from botend.services.class_guide_render import render_blocks
from botend.services.class_guide_service import check_article
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


def article_updated(guide):
    """来源文章沿用原文日期；站内原创使用本地保存时间。"""
    if guide.source_url or guide.source_modified:
        try:
            return parse_date(guide.source_modified[:10])
        except ValueError:
            return None
    return guide.updated_at


def catalog_rows(query):
    query = query.prefetch_related('tags')
    specs = {s['spec_id']: s for s in specialization_catalog()}
    rows = []
    for guide in query:
        rows.append({'guide': guide, 'title': guide.title, 'spec': specs[guide.spec_id],
                     'author_name': author_profile_for(guide)['name'],
                     'tags': list(guide.tags.all()), 'cover': safe_url(guide.source_payload.get('featuredImage', '')),
                     'updated': article_updated(guide), 'role': SPEC_ROLE[(guide.class_name, guide.spec_name)]})
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
        checks = check_article(guide)
        blocks = render_blocks(guide.blocks, checks['references'], guide)
        toc = [{'id': b['id'], 'title': strip_tags(b['title']), 'level': b['data']['level']}
               for b in walk_blocks(blocks) if b['type'] == 'heading']
        spec = next(s for s in specialization_catalog() if s['spec_id'] == guide.spec_id)
        query = urlencode({'class': guide.class_name, 'spec': guide.spec_name, 'version': guide.game_version})
        gear_url = next((b['data']['tool_url'] for b in walk_blocks(blocks)
                         if b['type'] == 'gear' and b.get('data', {}).get('tool_url')), gear_tool_url({}, guide, {}))
        return render(request, 'portal/class_guide_article.html', {
            'title': guide.title, 'guide': guide, 'spec': spec,
            'author_profile': author_profile_for(guide),
            'disclaimers': guide_disclaimers(guide),
            'updated': article_updated(guide),
            'cover': safe_url(guide.source_payload.get('featuredImage', '')),
            'blocks': blocks, 'toc': toc, 'read_minutes': max(1, len(re.sub(r'\s', '', strip_tags(guide.content_markdown))) // 500),
            'related': catalog_rows(ClassGuide.objects.filter(spec_id=guide.spec_id, archived=False).order_by('title')),
            'talent_url': '/portal/talents/?' + query, 'gear_url': gear_url,
        })
