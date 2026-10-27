"""Persistent single-character jobs. HTTP enqueues only; a detached CLI does work.

GET is read-only (expired jobs are projected failed). POST/worker persist expiry.
Control metadata never replaces evidence or editorial content. No automatic retry.
"""
import os
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from botend.models import PlayerObservationAnalysis, PlayerSpecTopPlayer, RaiderIOCharacterActivity
from botend.raiderio_identity import activity_identity_digest
from botend.services.player_analysis_management import AnalysisConflict
from botend.services.player_raiderio_activity import player_identity, mplus_modules_ready

ACTIVE = ('queued', 'running')
QUEUE_TTL = timedelta(minutes=5)
RUN_SECONDS = 600
RETRY = timedelta(minutes=30)
PUBLIC_FIELDS = ('state', 'message', 'token', 'phase', 'retry_at', 'queued_at', 'started_at', 'finished_at', 'expires_at', 'collection')


class Blocked(Exception):
    pass


class Deadline(BaseException):
    """Not swallowed by transport/AI exception handlers; CLI owns the alarm."""


def _date(value):
    try:
        stamp = parse_datetime(value) if isinstance(value, str) else None
        return stamp if stamp and timezone.is_aware(stamp) else None
    except (ValueError, TypeError):
        return None


def state(row):
    data = dict(row.recheck_json or {})
    if data.get('state') in ACTIVE and (not _date(data.get('expires_at')) or _date(data['expires_at']) <= timezone.now()):
        data.update(state='failed', message='重查任务超时或工作进程退出；请重试。', phase='expired')
    return {key: data[key] for key in PUBLIC_FIELDS if key in data} or {'state': 'idle', 'message': '尚未发起重查'}


def get_state(pk):
    return state(PlayerObservationAnalysis.objects.only('recheck_json').get(pk=pk))


def _save(row, data):
    row.recheck_json = data
    row.save(update_fields=['recheck_json'])


def _target(row):
    """Re-read persisted identities; same current-season identity, any spec Top20."""
    from botend.services.player_observation import _context
    from botend.services.player_observation_sources import canonical_class_spec
    if not row.player_id:
        raise Blocked('未绑定可靠角色，不能自动重查。')
    fields = ('id', 'season_id', 'region', 'realm', 'character_name', 'profile_url', 'class_name', 'spec_name', 'rank')
    player = PlayerSpecTopPlayer.objects.only(*fields).get(pk=row.player_id)
    activity = RaiderIOCharacterActivity.objects.only('id', 'season_id', 'identity_digest', 'region', 'realm_slug', 'character_name').get(pk=row.activity_id)
    identity = player_identity(player)
    if (not identity or identity['character_name'] == 'anonymous' or player.season_id != activity.season_id
            or activity_identity_digest(**identity) != activity.identity_digest
            or activity_identity_digest(activity.region, activity.realm_slug, activity.character_name) != activity.identity_digest):
        raise Blocked('角色与活动身份或赛季不一致，不能重查。')
    context = _context(player, identity=identity, activity=activity)
    if not context:
        raise Blocked('仅支持当前赛季任意专精 Top20 的可靠角色；人工编辑不受此限制。')
    candidates = PlayerSpecTopPlayer.objects.filter(season_id=player.season_id, region=player.region,
        character_name__iexact=player.character_name, rank__gte=1, rank__lte=20).only(*fields).order_by('pk')
    candidate = next((p for p in candidates if canonical_class_spec(p.class_name, p.spec_name) and player_identity(p) == identity), None)
    if candidate is None:
        raise Blocked('当前角色不在支持的 Top20 范围。')
    return candidate, activity, context[0]


def _binding(row, activity):
    return {'player_id': row.player_id, 'activity_id': activity.pk, 'season_id': activity.season_id, 'identity_digest': activity.identity_digest}


def _live(row, token):
    data = row.recheck_json or {}
    return (data.get('token') == token and data.get('state') in ACTIVE
            and _date(data.get('expires_at')) and _date(data['expires_at']) > timezone.now())


def _protection(row):
    now = timezone.now()
    if row.lease_token and row.lease_expires_at and row.lease_expires_at > now:
        return '已有分析租约正在执行，请稍后重试。', row.lease_expires_at
    if row.next_attempt_at and row.next_attempt_at > now:
        return '分析失败退避尚未结束，请稍后重试。', row.next_attempt_at
    retry = _date((row.recheck_json or {}).get('retry_at'))
    if retry and retry > now:
        return '重查失败退避尚未结束，请稍后重试。', retry
    return None, None


@contextmanager
def admission_lock():
    import fcntl  # 文件锁仅在工作进程操作时载入，管理页读取不依赖平台锁。
    # Serialize short enqueue transactions across web processes on this host.
    # The worker does not hold this lock during network/AI calls.
    root = Path(settings.BASE_DIR) / 'var' / 'player-observation-locks'
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'recheck-admission.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def enqueue(pk, payload):
    if (not isinstance(payload, dict) or set(payload) != {'revision'}
            or type(payload['revision']) is not int or payload['revision'] < 0):
        raise ValidationError('仅允许非负整数 revision。')
    with admission_lock(), transaction.atomic():
        row = PlayerObservationAnalysis.objects.select_for_update().defer('evidence_json', 'result_json').get(pk=pk)
        # Duplicate clicks return the same job, even if an editor changed revision.
        if _live(row, (row.recheck_json or {}).get('token')):
            return state(row)
        if row.revision != payload['revision']:
            raise AnalysisConflict()
        reason, retry = _protection(row)
        try:
            if reason:
                raise Blocked(reason)
            _, activity, _ = _target(row)
            if PlayerObservationAnalysis.objects.exclude(pk=row.pk).filter(
                    recheck_json__state__in=ACTIVE,
                    recheck_json__expires_at__gt=timezone.now().isoformat()).exists():
                raise Blocked('其他角色正在重查，请完成后再试；不会并发启动多个采集进程。')
        except Blocked as exc:
            data = {'state': 'blocked', 'message': str(exc), 'finished_at': timezone.now().isoformat()}
            if retry:
                data['retry_at'] = retry.isoformat()
            _save(row, data)
            return state(row)
        now, token = timezone.now(), uuid.uuid4().hex
        _save(row, {'state': 'queued', 'message': '已排队，等待独立进程重查。', 'token': token,
            'phase': 'queued', 'queued_at': now.isoformat(), 'expires_at': (now + QUEUE_TTL).isoformat(),
            'binding': _binding(row, activity)})
        transaction.on_commit(lambda: _spawn(pk, token))
        return state(row)


def _spawn(pk, token):
    try:
        subprocess.Popen([sys.executable, str(Path(settings.BASE_DIR) / 'manage.py'),
            'recheck_player_analysis', '--analysis-id', str(pk), '--token', token,
            '--settings', os.environ.get('DJANGO_SETTINGS_MODULE', 'LMonitor.settings')],
            cwd=str(settings.BASE_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
    except (OSError, ValueError):
        finish(pk, token, 'failed', '无法启动重查进程，请稍后重试。', phase='dispatch')


def finish(pk, token, outcome, message, *, phase=None):
    with transaction.atomic():
        row = PlayerObservationAnalysis.objects.select_for_update().defer('evidence_json', 'result_json').get(pk=pk)
        data = row.recheck_json or {}
        if data.get('token') != token or data.get('state') not in ACTIVE:
            return state(row)
        now = timezone.now()
        data = {**data, 'state': outcome, 'message': message, 'finished_at': now.isoformat()}
        if phase:
            data['phase'] = phase
        if outcome == 'failed':
            data['retry_at'] = (now + RETRY).isoformat()
        _save(row, data)
        return state(row)


def phase(pk, token, name):
    with transaction.atomic():
        row = PlayerObservationAnalysis.objects.select_for_update().defer('evidence_json', 'result_json').get(pk=pk)
        if not _live(row, token):
            raise Blocked('任务已失效或被替代。')
        messages = {'sources': '正在更新角色活动与阵容。', 'raid_sources': '正在核验团本来源。',
                    'analysis': '正在生成角色分析。', 'verify': '正在核对新分析与当前证据。'}
        _save(row, {**row.recheck_json, 'phase': name,
                    'message': messages.get(name, '正在重查。')})


@contextmanager
def character_lock(activity):
    import fcntl
    root = Path(settings.BASE_DIR) / 'var' / 'player-observation-locks'
    root.mkdir(parents=True, exist_ok=True)
    with (root / f'{activity.season_id}-{activity.identity_digest}.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Blocked('同一角色的命令行采集或分析正在执行。') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def validate_publication(row, token, evidence):
    """Used before AND inside generator publication, never portal visibility."""
    if not _live(row, token):
        raise Blocked('任务已失效或被替代。')
    _, activity, season = _target(row)
    if row.recheck_json.get('binding') != _binding(row, activity):
        raise Blocked('重查期间角色绑定已变化。')
    now = timezone.now()
    if not evidence or evidence.get('activity_id') != activity.pk or evidence.get('source_failed'):
        raise Blocked('当前证据来源失败，已保留上次有效分析。')
    if not mplus_modules_ready(activity):
        raise Blocked('角色活动来源未完整就绪或已过期。')
    from botend.services.player_raid_observation_sources import TTL as RAID_TTL
    raid = activity.payload_json.get('raid_first_kills') or {}
    stamp = _date(raid.get('fetched_at'))
    if (raid.get('status') != 'ready' or raid.get('last_error') or raid.get('visible') is not True
            or raid.get('identity_digest') != activity.identity_digest or raid.get('rio_season') != season.rio_season
            or not stamp or not timedelta(0) <= now - stamp <= RAID_TTL):
        raise Blocked('团本来源未完整就绪或已过期。')
    # Evidence can reuse last-good score facts. They must not conceal a failed refresh.
    ids = [item['activity_id'] for item in evidence.get('source_stamps', {}).get('scores', [])]
    if RaiderIOCharacterActivity.objects.filter(pk__in=ids).exclude(last_error='').exists():
        raise Blocked('参与证据的评分来源存在失败，已保留上次分析。')


def _bounded_investigation_complete(sources):
    """Only explicit successful investigation terminals relax *unattempted* gaps."""
    trace = sources.get('investigation') or {}
    requests = sources.get('followup_api_requests')
    budget = sources.get('followup_api_budget')
    return (sources.get('collection_mode') == 'progressive'
        and trace.get('version') == 1 and trace.get('triggered') is True
        and trace.get('bounded_complete') is True
        and trace.get('stop_reason') in ('completed', 'source_missing', 'budget_completed')
        and trace.get('stop_reason') == sources.get('followup_budget_reason')
        and type(requests) is int and type(budget) is int and 0 <= requests <= budget <= 2
        and trace.get('api_requests') == requests and trace.get('api_budget') == budget
        and isinstance(trace.get('outcomes'), list) and isinstance(trace.get('requests'), list)
        and all(item.get('status') in ('answered', 'not_needed', 'source_missing', 'identity_unresolved', 'budget_pending')
                or (item.get('status') == 'observed_partial'
                    and item.get('kind') == 'scored_history'
                    and item.get('request_status') in ('ready', 'fresh'))
                for item in trace['outcomes'])
        and all(item.get('status') in ('ready', 'fresh') for item in trace['requests']))


def require_sources(sources):
    """Validate bounded progressive work, not an all-teammates census.

    The collector deliberately reports partial after a completed follow-up,
    for private rosters, and when discovery exceeds the scoring candidate cap.
    Those are known sample limits, not failed requests. Actual unfinished work
    and failed requests still block; publication independently checks central facts.
    """
    if sources.get('source_failed'):
        raise Blocked('当前证据来源失败，已保留上次有效分析。')
    if sources.get('status') not in ('ready', 'fresh', 'partial'):
        raise Blocked('角色来源检查未完成：' + str(sources.get('status', 'unknown'))[:40])
    if sources.get('activity', {}).get('status') not in ('ready', 'fresh'):
        raise Blocked('角色活动来源未完整就绪或处于退避。')
    if sources.get('invalid_references') or sources.get('identity_conflicts'):
        raise Blocked('通关引用或队友身份冲突，不能生成新分析。')
    bounded = _bounded_investigation_complete(sources)
    runs = sources.get('runs', [])
    allowed_runs = ('ready', 'fresh', 'unavailable', 'not_needed', 'budget_exhausted') if bounded else ('ready', 'fresh', 'unavailable', 'not_needed')
    bad_runs = [item.get('status', 'unknown') for item in runs if item.get('status') not in allowed_runs]
    if sources.get('runs_truncated') or bad_runs:
        raise Blocked('阵容来源未完成（' + ', '.join(map(str, bad_runs[:3] or ['sample_truncated'])) + '）；本轮预算或退避结束后请重试。')
    bad_scores = [item.get('status', 'unknown') for item in sources.get('characters', [])
                  if item.get('status') not in ('ready', 'fresh', 'pending')]
    if bad_scores:
        raise Blocked('评分来源未完成（' + ', '.join(map(str, bad_scores[:3])) + '）；已保留上次分析。')
    followup = sources.get('followup_triggered')
    if followup and not bounded and (sources.get('investigation') is not None
                                    or sources.get('followup_budget_reason') != 'completed'):
        raise Blocked('所需评分补采尚未完成（' + str(sources.get('followup_budget_reason', 'unknown'))[:40] + '）；请稍后再次检查。')
    known_partial = (followup or sources.get('characters_truncated')
                     or any(item.get('status') == 'unavailable' for item in runs))
    if sources.get('status') == 'partial' and not (
            sources.get('collection_mode') == 'progressive' and known_partial):
        raise Blocked('来源未完整就绪，不能用旧快照冒充本次成功。')


def record_sources(pk, token, sources):
    """Persist literal collection counts and question outcomes under the job fence."""
    with transaction.atomic():
        row = PlayerObservationAnalysis.objects.select_for_update().defer('evidence_json', 'result_json').get(pk=pk)
        if not _live(row, token):
            raise Blocked('任务已失效或被替代。')
        trace = sources.get('investigation') or {}
        collection = {key: sources.get(key) for key in (
            'status', 'api_requests', 'api_budget', 'basic_api_requests', 'basic_api_budget',
            'basic_roster_api_budget', 'followup_api_requests', 'followup_api_budget',
            'followup_triggered', 'followup_reason', 'followup_budget_reason')}
        collection.update(stop_reason=trace.get('stop_reason'),
                          remaining_question_ids=trace.get('remaining_question_ids', []),
                          remaining_run_ids=trace.get('remaining_run_ids', []))
        _save(row, {**row.recheck_json, 'sources': sources, 'investigation': trace, 'collection': collection})


def run(pk, token):
    from botend.services.player_observation import build_evidence, generate_observation, PROMPT_VERSION, _validate_frozen_publication
    from botend.services.player_observation_sources import refresh_observation_sources
    from botend.services.player_raid_observation_sources import refresh_raid_sources
    import json
    try:
        with transaction.atomic():
            row = PlayerObservationAnalysis.objects.select_for_update().defer('evidence_json', 'result_json').get(pk=pk)
            if not _live(row, token) or row.recheck_json['state'] != 'queued':
                if row.recheck_json.get('token') == token and row.recheck_json.get('state') in ACTIVE and not _live(row, token):
                    return finish(pk, token, 'failed', '重查任务已过期，请稍后重试。', phase='expired')
                return state(row)
            player, activity, _ = _target(row)
            if row.recheck_json.get('binding') != _binding(row, activity):
                raise Blocked('排队期间角色绑定已变化。')
            reason, _ = _protection(row)
            if reason:
                raise Blocked(reason)
            now = timezone.now()
            _save(row, {**row.recheck_json, 'state': 'running', 'message': '正在重查来源与分析。',
                'started_at': now.isoformat(), 'expires_at': (now + timedelta(seconds=RUN_SECONDS)).isoformat()})
        with character_lock(activity):
            phase(pk, token, 'sources')
            sources = refresh_observation_sources(player, progressive=True, refresh_recent=True,
                max_basic_roster_requests=5, max_followup_requests=2, max_runs=18, max_characters=40)
            record_sources(pk, token, sources)
            require_sources(sources)
            phase(pk, token, 'raid_sources')
            raid = refresh_raid_sources(player)
            if raid.get('status') not in ('ready', 'fresh'):
                raise Blocked('团本来源未完成或处于退避；已保留上次有效分析。')
            row.refresh_from_db()
            validate_publication(row, token, build_evidence(player))
            phase(pk, token, 'analysis')
            result = generate_observation(player, force=True, skip_uninformative=True, recheck_token=token)
            if result.get('status') != 'ready':
                return finish(pk, token, 'failed', '分析未完成：' + str(result.get('status')), phase='analysis')
            phase(pk, token, 'verify')
            row.refresh_from_db()
            evidence = build_evidence(player)
            validate_publication(row, token, evidence)
            if (row.status != 'ready' or row.last_error or row.prompt_version != PROMPT_VERSION
                    or row.evidence_hash != evidence['evidence_hash'] or row.evidence_json != evidence or not row.result_json):
                raise Blocked('中央分析与当前证据不一致，不能记为重查成功。')
            _validate_frozen_publication(row)
            trace = sources.get('investigation') or {}
            unfinished = (trace.get('remaining_question_ids') or trace.get('remaining_run_ids')
                          or any(h.get('status') == 'unknown' or h.get('unknown_ids')
                                 for h in trace.get('hypotheses', [])))
            message = ('本次检查完成，仍有待验证假设；中央分析已更新，人工编辑与发布设置保持不变。'
                       if trace.get('triggered') and unfinished else
                       '重查完成；中央分析已更新，人工编辑与发布设置保持不变。')
            return finish(pk, token, 'succeeded', message, phase='complete')
    except Blocked as exc:
        return finish(pk, token, 'blocked', str(exc))
    except Deadline:
        return finish(pk, token, 'failed', '重查超过时间预算，已停止；请稍后重试。', phase='timeout')
    except Exception:
        return finish(pk, token, 'failed', '重查工作进程失败，已保留上次有效分析。')
