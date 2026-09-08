"""仅后台权限可访问的攻略管理、审阅与阅读预览。"""

import copy
import json
import re
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction, IntegrityError
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.views import View
from django.views.decorators.cache import never_cache
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie

from botend.dashboard.permissions import DashboardPermissionRequiredMixin
from botend.guide_models import ClassGuide, ClassGuideRevision, ClassGuideFeed, ClassGuideSyncRun, ClassGuideTerm, ClassGuideTag
from botend.services.class_guide_tags import guide_disclaimers, normalize_tags, set_guide_tags
from botend.services.class_guide_authors import normalize_author_profile, author_profile_for
from botend.services.class_guide_content import walk_blocks
from botend.constants.wow import resolve_spec_identity, specialization_catalog, CLASS_CN
from botend.services.class_guide_render import render_blocks, selected_revision
from botend.services.class_guide_service import create_revision, approve_revision, audit_revision, RevisionConflict
from botend.services.class_guide_markdown import compile_markdown
from botend.services.class_guide_monitor import get_guide_monitor_task


class GuideAccess(DashboardPermissionRequiredMixin):
    dashboard_permission = 'content.class-guides'

    def dispatch(self, request, *args, **kwargs):
        try:
            response = super().dispatch(request, *args, **kwargs)
        except RevisionConflict as exc:
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
    return {'author_display_name': author_profile_for(guide)['name'], 'specialization_label': guide.specialization_label, 'tags': [tag.name for tag in guide.tags.all()], **{key: getattr(guide, key) for key in ['id', 'title', 'slug', 'spec_id', 'class_name', 'spec_name',
        'game_version', 'guide_type', 'source_url', 'author', 'archived', 'revision_number', 'published_revision_id']}}


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
        if request.GET.get('archived') != '1':
            query = query.filter(archived=False)
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
        with transaction.atomic():
            guide = ClassGuide(spec_id=spec_id, class_name=class_name, spec_name=spec_name, **fields)
            guide.full_clean(); guide.save()
            set_guide_tags(guide, tags)
            create_revision(guide.id, guide.title, content_markdown=data.get('content_markdown', ''), expected_number=0, user=request.user, note='新建攻略')
        return JsonResponse({'id': guide.id}, status=201)


class GuideDetailAPI(GuideAccess, View):
    def get(self, request, guide_id):
        guide = get_object_or_404(ClassGuide, pk=guide_id)
        revision_id = request.GET.get('revision')
        revision = get_object_or_404(guide.revisions, pk=revision_id) if revision_id else selected_revision(guide)
        data = summary(guide)
        data.update(author_profile=guide.author_profile, source_author_profile=guide.source_author_profile,
                    display_author_profile=author_profile_for(guide))
        data['available_tags'] = list(ClassGuideTag.objects.values_list('name', flat=True))
        data['revisions'] = list(guide.revisions.values('id', 'number', 'title', 'origin', 'note', 'created_at', 'source_modified'))
        if revision:
            data['revision'] = {'id': revision.id, 'number': revision.number, 'title': revision.title,
                'content_markdown': revision.content_markdown, 'source_markdown': revision.source_markdown, 'audit': audit_revision(revision)}
        return JsonResponse(data)

    def post(self, request, guide_id):
        """用相同的阅读渲染器预览未保存正文，不创建修订。"""
        guide = get_object_or_404(ClassGuide, pk=guide_id)
        data = payload(request)
        blocks = compile_markdown(data['content_markdown'])
        revision = ClassGuideRevision(guide=guide, blocks=blocks)
        audit = audit_revision(revision)
        rendered = render_blocks(blocks, audit['references'], guide)
        return JsonResponse({'html': render_to_string('dashboard/class_guide_blocks.html', {'blocks': rendered}),
            'toc': [{'id': b['id'], 'title': b.get('title', ''), 'level': b['data']['level'], 'line': b['data']['source_line']}
                    for b in walk_blocks(rendered) if b['type'] == 'heading']})

    def patch(self, request, guide_id):
        guide = get_object_or_404(ClassGuide, pk=guide_id)
        data = payload(request)
        expected = data.get('expected_number')
        if any(key in data for key in ('spec_id', 'class_name', 'spec_name')):
            spec_id, _, _ = resolve_spec_identity(data.get('spec_id'), data.get('class_name', ''), data.get('spec_name', ''))
            if spec_id != guide.spec_id:
                raise ValueError('文章已绑定专精；其他专精请新建对应攻略')
        if isinstance(expected, bool) or not isinstance(expected, int):
            raise ValueError('保存必须携带读取时的文章修订序号')
        action = data.get('action', 'save')
        if action == 'approve':
            revision = approve_revision(guide.id, data['revision_id'], expected)
            return JsonResponse({'approved_revision_id': revision.id})
        if action == 'archive':
            if not ClassGuide.objects.filter(pk=guide.id, revision_number=expected).update(archived=bool(data.get('archived', True))):
                raise RevisionConflict('文章已变化，请刷新后重试')
            return JsonResponse({'success': True})
        base = get_object_or_404(guide.revisions, pk=data.get('base_revision_id'))
        if action == 'restore':
            markdown, title = base.content_markdown, base.title
            audit = copy.deepcopy(base.audit)
        elif action == 'save':
            markdown = data['content_markdown']
            compile_markdown(markdown)
            title = str(data.get('title', '')).strip()
            if not title or len(title) > 255:
                raise ValueError('标题必须为 1 至 255 字符')
            audit = copy.deepcopy(base.audit)
            # 翻译检查跟随原文片段，标题增删引起的目录编号变化不会清除检查项。
            normalized = re.sub(r'\s+', ' ', markdown)
            audit['untranslated'] = [r for r in audit.get('untranslated', [])
                if not r.get('source_text') or re.sub(r'\s+', ' ', r['source_text']) in normalized
                or not re.search(r'[\u3400-\u9fff]', markdown)]
            audit['manual_conflict'] = False
        else:
            raise ValueError('不支持的操作')
        tags = normalize_tags(data['tags']) if action == 'save' and 'tags' in data else None
        author_change = action == 'save' and 'author_profile' in data
        author_profile = normalize_author_profile(data['author_profile']) if author_change and data['author_profile'] is not None else None
        with transaction.atomic():
            revision = create_revision(guide.id, title, content_markdown=markdown, expected_number=expected, user=request.user,
                source_markdown=base.source_markdown,
                source_blocks=base.source_blocks, source_payload=base.source_payload, source_hash=base.source_hash,
                source_modified=base.source_modified, audit=audit, note=str(data.get('note', '人工编辑'))[:500])
            if tags is not None:
                set_guide_tags(guide, tags)
            if author_change:
                ClassGuide.objects.filter(pk=guide.pk).update(author_profile=author_profile)
        return JsonResponse({'revision_id': revision.id, 'revision_number': revision.number})


class GuidePreviewPage(GuideAccess, View):
    def get(self, request, guide_id):
        guide = get_object_or_404(ClassGuide, pk=guide_id)
        revision = get_object_or_404(guide.revisions, pk=request.GET['revision']) if request.GET.get('revision') else selected_revision(guide)
        if not revision:
            raise ValueError('文章尚无内容')
        audit = audit_revision(revision)
        blocks = render_blocks(revision.blocks, audit['references'], guide)
        return render(request, 'dashboard/class_guide_preview.html', {'guide': guide, 'revision': revision,
            'author_profile': author_profile_for(guide),
            'disclaimers': guide_disclaimers(guide),
            'blocks': blocks, 'audit': audit,
            'toc': [b for b in walk_blocks(blocks) if b['type'] == 'heading']})


class GuideDisclaimerAPI(GuideAccess, View):
    def get(self, request):
        tag = ClassGuideTag.objects.filter(name__iexact='maxroll').first()
        return JsonResponse({'tag': 'maxroll', 'text': tag.disclaimer if tag else ''})

    def patch(self, request):
        text = payload(request).get('text')
        if not isinstance(text, str) or len(text) > 3000:
            raise ValueError('免责声明必须为文本，最多 3000 字符')
        tag = ClassGuideTag.objects.filter(name__iexact='maxroll').first()
        if tag is None:
            tag, _ = ClassGuideTag.objects.get_or_create(name='maxroll')
        tag.disclaimer = text.strip()
        tag.save(update_fields=['disclaimer'])
        return JsonResponse({'success': True, 'text': tag.disclaimer})


class GuideFeedAPI(GuideAccess, View):
    def get(self, request):
        feed, _ = ClassGuideFeed.objects.get_or_create(key='maxroll')
        task = get_guide_monitor_task()
        return JsonResponse({'enabled': task.is_active, 'interval_minutes': task.wait_time // 60,
            'monitor_task_id': task.id, 'monitor_task_name': task.name,
            'authorization_note': feed.authorization_note, 'last_checked_at': feed.last_checked_at,
            'next_check_at': task.last_scan_time + timedelta(seconds=task.wait_time) if task.is_active else None,
            'lease_until': feed.lease_until,
            'runs': list(ClassGuideSyncRun.objects.values('id', 'status', 'started_at', 'finished_at', 'coverage', 'results', 'error')[:10])})

    def patch(self, request):
        data = payload(request)
        if not isinstance(data.get('enabled'), bool) or type(data.get('interval_minutes')) is not int or not 15 <= data['interval_minutes'] <= 10080:
            raise ValueError('请填写监控开关及 15 至 10080 分钟的检查间隔')
        note = str(data.get('authorization_note', '')).strip()
        if data['enabled'] and not note:
            raise ValueError('启用来源监控前必须登记授权说明')
        with transaction.atomic():
            feed, _ = ClassGuideFeed.objects.get_or_create(key='maxroll')
            feed.authorization_note = note[:10000]
            feed.save(update_fields=['authorization_note'])
            task = get_guide_monitor_task()
            type(task).objects.filter(pk=task.pk).update(is_active=data['enabled'], wait_time=data['interval_minutes'] * 60)
        return JsonResponse({'success': True})


class GuideTermsAPI(GuideAccess, View):
    def get(self, request):
        records = ClassGuideTerm.objects.filter(game_version=request.GET.get('version', '')).order_by('kind', 'object_id')
        query = request.GET.get('q', '').strip()
        if query:
            condition = Q(name_en__icontains=query) | Q(name_zh__icontains=query)
            if query.isdecimal():
                condition |= Q(object_id=int(query))
            records = records.filter(condition)
        if request.GET.get('kind'):
            records = records.filter(kind=request.GET['kind'])
        page = max(1, min(int(request.GET.get('page', 1)), 10000))
        return JsonResponse({'records': list(records.values()[(page-1)*100:page*100]), 'total':records.count(), 'page':page})

    def post(self, request):
        data = payload(request)
        fields = {k: data.get(k, '') for k in ['game_version', 'kind', 'object_id', 'name_en', 'name_zh', 'icon', 'evidence']}
        if fields['kind'] not in {'spell', 'item', 'talent', 'phrase', 'macro'} or not re.search(r'[\u3400-\u9fff]', str(fields['name_zh'])):
            raise ValueError('引用类型或中文名无效')
        if fields['kind'] in {'phrase', 'macro'}:
            if not str(fields['name_en']).strip():
                raise ValueError('专有名词必须填写英文原名')
            fields['object_id'] = ClassGuideTerm.phrase_identifier(fields['name_en'])
        term = ClassGuideTerm(**fields)
        term.full_clean(validate_unique=False, validate_constraints=False)
        record, _ = ClassGuideTerm.objects.update_or_create(game_version=fields.pop('game_version'),
            kind=fields.pop('kind'), object_id=fields.pop('object_id'), defaults=fields)
        return JsonResponse({'id': record.id})
