"""仅后台权限可访问的攻略管理与阅读预览。"""

import copy
import json
import re

from django.core.exceptions import ValidationError
from django.db import transaction, IntegrityError
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.views import View
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie

from botend.dashboard.permissions import DashboardPermissionRequiredMixin
from botend.guide_models import ClassGuide, ClassGuideSyncRun, ClassGuideTag
from botend.services.class_guide_tags import guide_disclaimers, normalize_tags, set_guide_tags
from botend.services.class_guide_authors import normalize_author_profile, author_profile_for
from botend.services.class_guide_content import walk_blocks
from botend.constants.wow import resolve_spec_identity, specialization_catalog, CLASS_CN
from botend.services.class_guide_render import render_blocks
from botend.services.class_guide_service import save_article, check_article, ArticleConflict
from botend.services.class_guide_markdown import compile_markdown


class GuideAccess(DashboardPermissionRequiredMixin):
    dashboard_permission = 'content.class-guides'

    def dispatch(self, request, *args, **kwargs):
        try:
            response = super().dispatch(request, *args, **kwargs)
        except ArticleConflict as exc:
            response = JsonResponse({'error': str(exc)}, status=409)
        except (ValueError, TypeError, KeyError, ValidationError) as exc:
            response = JsonResponse({'error': str(exc)}, status=400)
        except IntegrityError:
            response = JsonResponse({'error': '该标识和版本已存在，请刷新后重试'}, status=409)
        response['Cache-Control'] = 'private, no-store'
        response['X-Robots-Tag'] = 'noindex, nofollow'
        return response


def payload(request):
    if len(request.body) > 4000000:
        raise ValueError('请求超过大小限制')
    value = json.loads(request.body or b'{}')
    if not isinstance(value, dict):
        raise ValueError('请求必须是 JSON 对象')
    return value


def summary(guide):
    return {'is_visible': not guide.archived, 'updated_at': guide.updated_at.isoformat(), 'author_display_name': author_profile_for(guide)['name'], 'specialization_label': guide.specialization_label, 'tags': [tag.name for tag in guide.tags.all()], **{key: getattr(guide, key) for key in ['id', 'title', 'slug', 'spec_id', 'class_name', 'spec_name',
        'game_version', 'guide_type', 'source_url', 'author']}}


@method_decorator(ensure_csrf_cookie, name='dispatch')
class GuidePage(GuideAccess, View):
    def get(self, request, guide_id=None):
        # 旧直达链接保留，编辑和写入统一进入 Dashboard 内容区。
        target = '/dashboard/?section=class-guides'
        if guide_id:
            target += f'&guide={guide_id}'
        return redirect(target)


class GuideCatalogAPI(GuideAccess, View):
    def get(self, request):
        query = ClassGuide.objects.prefetch_related('tags').order_by('class_name', 'spec_name', 'title', 'id')
        visibility = request.GET.get('visible', '')
        if visibility not in ('', '0', '1'):
            raise ValueError('显示筛选值无效')
        if visibility:
            query = query.filter(archived=visibility == '0')
        if request.GET.get('spec_id'):
            spec_id, _, _ = resolve_spec_identity(request.GET['spec_id'])
            query = query.filter(spec_id=spec_id)
        for key in ['class_name', 'spec_name', 'game_version', 'guide_type']:
            if request.GET.get(key):
                lookup = key + '__iexact' if key in ('class_name', 'spec_name') else key
                query = query.filter(**{lookup: request.GET[key]})
        if request.GET.get('q'):
            query = query.filter(Q(title__icontains=request.GET['q']) | Q(author__icontains=request.GET['q']) | Q(author_profile__name__icontains=request.GET['q']))
        for tag in request.GET.getlist('tag'):
            if tag.strip():
                query = query.filter(tags__name=tag.strip())
        query = query.distinct()
        page = max(1, int(request.GET.get('page', 1)))
        latest_run = ClassGuideSyncRun.objects.first()
        return JsonResponse({'records': [summary(g) for g in query[(page - 1) * 100:page * 100]],
            'total': query.count(), 'page': page,
            'versions': list(ClassGuide.objects.order_by('game_version').values_list('game_version', flat=True).distinct()),
            'tags': list(ClassGuideTag.objects.values_list('name', flat=True)),
            'classes': CLASS_CN, 'specializations': specialization_catalog(),
            'coverage': latest_run.coverage if latest_run else {}})

    def post(self, request):
        data = payload(request)
        spec_id, class_name, spec_name = resolve_spec_identity(data.get('spec_id'), data.get('class_name', ''), data.get('spec_name', ''))
        fields = {k: str(data.get(k, '')).strip() for k in ['title', 'slug', 'game_version', 'guide_type']}
        if not fields['title'] or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,199}', fields['slug']):
            raise ValueError('请填写标题和有效标识')
        # 新建文章不要求版本或类型；内部解析环境默认跟随当前天赋数据。
        from botend.models import WowTalentVersion
        active = WowTalentVersion.objects.filter(is_active=True).order_by('-id').first()
        fields['game_version'] = fields['game_version'] or (active.major_version if active else 'current')
        fields['guide_type'] = fields['guide_type'] or 'general'
        tags = normalize_tags(data.get('tags', []))
        visible = data.get('is_visible', True)
        if not isinstance(visible, bool):
            raise ValueError('是否显示必须为布尔值')
        with transaction.atomic():
            guide = ClassGuide(spec_id=spec_id, class_name=class_name, spec_name=spec_name, archived=not visible, **fields)
            guide.full_clean(); guide.save()
            set_guide_tags(guide, tags)
            save_article(guide.id, guide.title, content_markdown=data.get('content_markdown', ''), expected_updated_at=guide.updated_at)
        return JsonResponse({'id': guide.id}, status=201)


class GuideDetailAPI(GuideAccess, View):
    def get(self, request, guide_id):
        guide = get_object_or_404(ClassGuide, pk=guide_id)
        data = summary(guide)
        data.update(author_profile=guide.author_profile, source_author_profile=guide.source_author_profile,
                    display_author_profile=author_profile_for(guide),
                    content_markdown=guide.content_markdown, source_markdown=guide.source_markdown,
                    checks=check_article(guide), classes=CLASS_CN, specializations=specialization_catalog())
        data['available_tags'] = list(ClassGuideTag.objects.values_list('name', flat=True))
        return JsonResponse(data)

    def post(self, request, guide_id):
        """预览未保存正文，目录与引用均从 Markdown 实时解析。"""
        guide = get_object_or_404(ClassGuide, pk=guide_id)
        data = payload(request)
        if 'spec_id' in data:
            guide.spec_id, guide.class_name, guide.spec_name = resolve_spec_identity(data['spec_id'])
        blocks = compile_markdown(data['content_markdown'])
        checks = check_article(guide, blocks)
        rendered = render_blocks(blocks, checks['references'], guide)
        return JsonResponse({'html': render_to_string('dashboard/class_guide_blocks.html', {'blocks': rendered}),
            'toc': [{'id': b['id'], 'title': b.get('title', ''), 'level': b['data']['level'], 'line': b['data']['source_line']}
                    for b in walk_blocks(rendered) if b['type'] == 'heading']})

    def patch(self, request, guide_id):
        guide = get_object_or_404(ClassGuide, pk=guide_id)
        data = payload(request)
        expected = parse_datetime(str(data.get('expected_updated_at', '')))
        if expected is None:
            raise ValueError('保存必须携带读取时的文章更新时间')
        extra = {}
        if any(key in data for key in ('spec_id', 'class_name', 'spec_name')):
            spec_id, class_name, spec_name = resolve_spec_identity(data.get('spec_id'), data.get('class_name', ''), data.get('spec_name', ''))
            extra.update(spec_id=spec_id, class_name=class_name, spec_name=spec_name)
        if 'is_visible' in data:
            if not isinstance(data['is_visible'], bool):
                raise ValueError('是否显示必须为布尔值')
            extra['archived'] = not data['is_visible']
        action = data.get('action', 'save')
        if action != 'save':
            raise ValueError('不支持的操作')
        markdown = data['content_markdown']
        checks = copy.deepcopy(guide.check_data)
        normalized = re.sub(r'\s+', ' ', markdown)
        checks['untranslated'] = [r for r in checks.get('untranslated', [])
            if not r.get('source_text') or re.sub(r'\s+', ' ', r['source_text']) in normalized
            or not re.search(r'[\u3400-\u9fff]', markdown)]
        if 'author_profile' in data:
            extra['author_profile'] = normalize_author_profile(data['author_profile']) if data['author_profile'] is not None else None
        tags = normalize_tags(data['tags']) if 'tags' in data else None
        with transaction.atomic():
            saved = save_article(guide.id, data.get('title', ''), content_markdown=markdown,
                expected_updated_at=expected, check_data=checks, **extra)
            if tags is not None:
                set_guide_tags(saved, tags)
        return JsonResponse({'id': saved.id, 'updated_at': saved.updated_at.isoformat()})


class GuidePreviewPage(GuideAccess, View):
    def get(self, request, guide_id):
        guide = get_object_or_404(ClassGuide, pk=guide_id)
        checks = check_article(guide)
        blocks = render_blocks(guide.blocks, checks['references'], guide)
        return render(request, 'dashboard/class_guide_preview.html', {'guide': guide,
            'author_profile': author_profile_for(guide), 'disclaimers': guide_disclaimers(guide),
            'blocks': blocks, 'checks': checks,
            'toc': [b for b in walk_blocks(blocks) if b['type'] == 'heading']})


class GuideDisclaimerAPI(GuideAccess, View):
    def get(self, request):
        tags = list(ClassGuideTag.objects.annotate(guide_count=Count('guides')).order_by('name'))
        requested = request.GET.get('tag', '').strip()
        tag = next((row for row in tags if row.name.casefold() == requested.casefold()), None) if requested else None
        if requested and tag is None:
            raise ValueError('攻略标签不存在')
        if tag is None:
            tag = next((row for row in tags if row.name.casefold() == 'maxroll'), tags[0] if tags else None)
        return JsonResponse({
            'tag': tag.name if tag else '',
            'text': tag.disclaimer if tag else '',
            'tags': [{'name': row.name, 'guide_count': row.guide_count} for row in tags],
        })

    def patch(self, request):
        data = payload(request)
        tag_name = data.get('tag')
        text = data.get('text')
        if not isinstance(tag_name, str) or not tag_name.strip() or len(tag_name.strip()) > 60:
            raise ValueError('请选择有效的攻略标签')
        if not isinstance(text, str) or len(text) > 3000:
            raise ValueError('免责声明必须为文本，最多 3000 字符')
        tag = ClassGuideTag.objects.filter(name__iexact=tag_name.strip()).first()
        if tag is None:
            raise ValueError('攻略标签不存在')
        tag.disclaimer = text.strip()
        tag.save(update_fields=['disclaimer'])
        return JsonResponse({'success': True, 'tag': tag.name, 'text': tag.disclaimer})
