"""持久化阵容查询，提交事务完成后由独立进程执行。"""
import logging
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from botend.models import MythicCompositionJob, SeasonMeta
from botend.mythic_planner.rio_routes import RouteSource, route_session
from botend.services.mythic_composition import candidates, export_route, resolve_run, selected_specs

logger = logging.getLogger(__name__)
LEASE = timedelta(minutes=15)


def expire_interrupted(owner):
    now = timezone.now()
    MythicCompositionJob.objects.filter(owner=owner, active_slot=1, updated_at__lt=now - LEASE).update(
        state='failed', active_slot=None, updated_at=now, message='工作进程长时间未更新，可重新提交。')


def dispatch(pk):
    try:
        subprocess.Popen([sys.executable, str(Path(settings.BASE_DIR) / 'manage.py'),
                          'collect_mythic_composition', str(pk)],
                         cwd=settings.BASE_DIR, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=os.name != 'nt',
                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    except OSError:
        logger.exception('阵容查询工作进程启动失败')
        MythicCompositionJob.objects.filter(pk=pk, state='queued').update(
            active_slot=None, state='failed', updated_at=timezone.now(), message='无法启动工作进程。')


def enqueue(owner, season, slots, complete):
    expire_interrupted(owner)
    job, created = MythicCompositionJob.objects.defer('results').get_or_create(
        owner=owner, active_slot=1, defaults=dict(season=season, slots=slots, complete_only=complete))
    if created:
        transaction.on_commit(lambda: dispatch(job.pk))
    return job, created


def collect(pk):
    active = MythicCompositionJob.objects.filter(pk=pk, active_slot=1)
    if not active.filter(state='queued').update(state='running', updated_at=timezone.now()):
        return
    running = active.filter(state='running')
    seen = set()
    results = []
    try:
        job = active.get()
        season = SeasonMeta.objects.get(rio_season=job.season, is_active=True)
        required = selected_specs(job.slots)
        with route_session() as session:
            source = RouteSource(session)
            source.dungeons(job.season)
            offset = 0
            while offset is not None and len(results) < 5:
                if not running.update(updated_at=timezone.now()):
                    break
                page = candidates(source, job.season, required, offset, job.complete_only)
                offset = page['next']
                for run_id in page['ids']:
                    if run_id in seen:
                        continue
                    if not running.update(updated_at=timezone.now()):
                        break
                    try:
                        run = resolve_run(source, season, required, run_id)
                    except requests.HTTPError as exc:
                        if exc.response is None or exc.response.status_code not in (404, 410):
                            raise
                        run = None
                    seen.add(run_id)
                    if run:
                        route = run.pop('route')
                        if job.complete_only and not run['wcl'] and run['wcl_status'] == 'WCL 暂时无法查询':
                            raise RuntimeError('WCL 暂时无法查询，请稍后重试。')
                        if route and (run['wcl'] or not job.complete_only):
                            try:
                                run['mdt'] = export_route(source, *route)
                            except requests.RequestException as exc:
                                missing = exc.response is not None and exc.response.status_code in (404, 410)
                                if job.complete_only and not missing:
                                    raise
                                run['route_status'] = '路线已不可用' if missing else '路线获取失败'
                        if not job.complete_only or run.get('mdt') and run['wcl']:
                            results.append(run)
                    if not running.update(checked=len(seen), found=len(results), results=results,
                                          updated_at=timezone.now()) or len(results) == 5:
                        break
        running.update(state='done', active_slot=None, updated_at=timezone.now())
    except Exception as exc:
        logger.exception('阵容任务 %s 抓取失败', pk)
        message = str(exc) if isinstance(exc, RuntimeError) else '抓取失败，请稍后重新提交。'
        running.update(state='failed', message=message[:300], active_slot=None, updated_at=timezone.now())
    finally:
        active.filter(state='stopping').update(state='stopped', active_slot=None, updated_at=timezone.now())
