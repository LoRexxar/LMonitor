"""五人阵容查询的任务提交、进度与历史结果。"""
import json

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from botend.constants.wow import SPEC_ROLE, specialization_catalog
from botend.dashboard.permissions import has_dashboard_permission
from botend.models import MythicCompositionJob, SeasonMeta
from botend.services.mythic_composition import ROLES, selected_specs
from botend.services.mythic_composition_jobs import enqueue, expire_interrupted


def summary(job):
    return dict(id=job.pk, state=job.state, season=job.season, slots=job.slots, complete=job.complete_only,
                found=job.found, checked=job.checked, message=job.message, created=job.created_at)


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
def composition_search(request):
    if not has_dashboard_permission(request.user, 'tools.mythic-composition'):
        return HttpResponseForbidden('无权查询大秘境阵容。')
    jobs = MythicCompositionJob.objects.filter(owner=request.user).defer('results')
    if request.method == 'GET':
        expire_interrupted(request.user)
        if request.GET.get('view') == 'status':
            pk = request.GET.get('job')
            if pk is not None and (not pk.isascii() or not pk.isdigit() or len(pk) > 18):
                return JsonResponse({'error': '任务编号无效。'}, status=400)
            job = jobs.filter(pk=int(pk)).first() if pk else jobs.order_by('-pk').first()
            if pk and not job:
                return JsonResponse({'error': '任务不存在。'}, status=404)
            data = summary(job) if job else None
            if job and job.active_slot is None:
                data['results'] = job.results
            return JsonResponse({'job': data})
        specs = specialization_catalog()
        labels = ('坦克', '治疗', '输出一', '输出二', '输出三')
        slots = [(label, [s for s in specs if SPEC_ROLE[s['class_name'], s['spec_name']] == role])
                 for label, role in zip(labels, ROLES)]
        return render(request, 'dashboard/mythic_composition.html',
                      {'slots': slots, 'jobs': jobs.order_by('-pk')[:30]})
    try:
        body = json.loads(request.body)
        if not isinstance(body, dict):
            raise ValueError('查询参数无效。')
        if body.get('action') == 'stop':
            pk = body.get('id')
            if type(pk) is not int or not 0 < pk < 2**63:
                raise ValueError('任务编号无效。')
            if not jobs.filter(pk=pk).exists():
                return JsonResponse({'error': '任务不存在。'}, status=404)
            if not jobs.filter(pk=pk, state='queued').update(state='stopped', active_slot=None, updated_at=timezone.now()):
                jobs.filter(pk=pk, state='running').update(state='stopping', updated_at=timezone.now())
            return JsonResponse({'ok': True})
        if body.get('action') != 'submit' or type(body.get('complete', False)) is not bool:
            raise ValueError('查询条件无效。')
        selected_specs(body.get('slots'))
    except (TypeError, ValueError) as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    season = SeasonMeta.objects.filter(is_active=True).first()
    if not season or not season.rio_season:
        return JsonResponse({'error': '暂无生效的 Raider.IO 赛季。'}, status=409)
    job, created = enqueue(request.user, season.rio_season, body['slots'], body.get('complete', False))
    return JsonResponse({'job': summary(job), 'created': created}, status=202 if created else 200)
