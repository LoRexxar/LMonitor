"""Dashboard API submodule: simc_benchmarks."""
#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: api.py
@time: 2024/01/15
@desc: Dashboard API Views
'''

from django.views import View
from django.http import (
    JsonResponse, HttpResponse, FileResponse, HttpResponseRedirect,
)
from django.core.serializers.json import DjangoJSONEncoder
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.contrib.auth.decorators import login_required

import json
import traceback
import hashlib
import math
import time
import re
import requests
import os
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
import platform as py_platform
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode
from django.utils import timezone
from django.template.loader import render_to_string

from django.conf import settings
from utils.log import logger
from botend.common.api import (
    ApiError,
    BaseJsonAPIView,
    parse_bool,
    parse_bounded_int,
    parse_int,
    parse_json_body,
    parse_text,
    require_int,
)
from botend.models import MonitorTask, PlayerSpecTopPlayer, PortalPeakSpecRankRow, SimcApl, SimcAplSymbol, SimcAplSymbolScope, SimcTask, SimcTaskFavorite, SimulationRun, SimcTaskArtifact, SimcProfile, SimcTalentString, SimcSecondaryStatRule, SimcMasteryCoefficient, SimcContentTemplate, SimcBackendBinary, SimcSkillDamageSnapshot, SimcAgent, SimcAgentMaintenanceTask, WclAnalysisTask, SystemAlert, WowDailyReport, WowHotfixReport, WowWagoHotfixEvent, WowWagoMonitorState, WowSpellSnapshot, WowTalentNodeMetadata, WowTalentVersion, WowItemSnapshot, SimcResourceVersion
from botend.alerting import upsert_system_alert
from botend.dashboard.permissions import DashboardPermissionRequiredMixin, has_dashboard_permission
from django.db import IntegrityError, models, transaction
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Length, SHA256, Substr
from core.glm import GLMClient
from botend.monitor_env import is_task_runnable, env_limit_hint
from botend.wow_daily_report.generator import generate_wow_daily_report
from botend.services.simc_attribute_results import parse_attribute_result_filename
from botend.services.simc_player_config import (
    EQUIPMENT_SLOT_ALIASES,
    EQUIPMENT_SLOTS,
    authoritative_player_baseline,
    canonical_simc_profile_identity,
    canonical_simc_profile_key,
    canonical_simc_spec_identity,
    normalize_gear_candidate_value,
    parse_manual_player_config,
    resolve_attribute_player_baseline,
    validate_default_player_baseline,
    validate_player_baseline,
    SUPPORTED_SIMC_SPEC_IDENTITIES,
)
from botend.services.simc_composer import SimcComposer, validate_simulation_options
from botend.services.simc_skill_damage import SimcSkillDamageSnapshotService, reviewed_global_display_effects
from botend.services.simc_skill_source_context import attach_skill_damage_source_context
from botend.services.simc_skill_descriptions import attach_skill_damage_descriptions
from botend.services.simc_skill_snapshot_storage import SkillSnapshotActorStorage
from botend.services.simc_skill_actor_page import (
    ActorPageService, ActorPageError, normalize_query, RESPONSE_BYTES,
)
from botend.wow.talents.service import TalentBuildCodeService
from botend.services.simc_hero_talents import resolve_hero_talent_names
from botend.services.simc_talent_titles import (
    apply_talent_display_names, talent_display_name, frozen_talent_display_names,
)
from botend.services.simc_consumables import simc_consumable_option
from botend.services.simc_benchmark_config import SIMC_RAID_BUFFS
from botend.services.spec_stats_service import SpecStatsService, _hero_subtree_name_from_table
from botend.services.simc_task_service import create_task, create_task_from_request, TaskCreationError
from botend.services.simc_attribute_search import (
    ATTRIBUTE_DPS_TOLERANCE as SIMC_ATTRIBUTE_DPS_TOLERANCE,
    ATTRIBUTE_SEARCH_STEP as SIMC_ATTRIBUTE_SEARCH_STEP,
    ATTRIBUTE_SEARCH_STEPS as SIMC_ATTRIBUTE_SEARCH_STEPS,
    ATTRIBUTE_STATS as SIMC_ATTRIBUTE_STATS,
    attribute_variants,
)
from botend.services.task_rerun import create_rerun, TaskRerunError
from botend.services.battlenet_preflight import fetch_battlenet_character_preflight
from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
from botend.constants.hero_talents import (
    normalize_hero_subtree_names_zh, normalize_hero_subtree_title_zh,
)
from botend.constants.wow import CLASS_SPEC_MAP, CLASS_CN, CLASS_COLOR, SPEC_CN, SPEC_ICON, SPEC_ROLE
from botend.templatetags.wow_tags import wow_icon_oss_url
from botend.services.simc_apl.catalog import query_symbol_catalog, query_visible_symbols
from botend.services.simc_apl.validation import validate_payload
from botend.services.simc_apl.authoritative_validator import RestrictedSimcValidator
from botend.services.simc_apl.publish import validate_apl_for_profile, content_hash, current_validation_identity
from botend.services.simc_composer import SimcComposer, SIMC_EXTRA_OPTIONS
from botend.services.simc_apl.completion import complete_document
from botend.services.simc_apl.translation import (
    extract_translation_demands, resolve_demand_mappings, translate_apl_ranges,
    TranslationDemand, CONTROL_ACTIONS, disambiguate_chinese_labels,
)
from django.core.exceptions import PermissionDenied, SuspiciousOperation, ValidationError
from django.db.models.deletion import ProtectedError
from collections import defaultdict, deque

from botend.models import SimcBenchmarkCase, SimcBenchmarkExecution, SimcBenchmarkPanel, SimcBenchmarkPurgeTask
from botend.constants.wow import SPEC_CN
from botend.services.simc_benchmark_purge import (
    PurgeBlocked,
    PurgeConflict,
    PurgeValidation,
    build_panel_purge_plan,
    panel_has_active_purge,
    queue_panel_purge,
)
from botend.services.simc_benchmark_config import (
    MAX_PROFILES_PER_SPEC, MAX_SCENARIOS, MAX_SPECS, SIMC_FIGHT_STYLES,
    SIMC_RAID_BUFFS, benchmark_resource_querysets,
    build_execution_plan, duplicate_panel_config, replace_panel_config, serialize_panel_config,
)
from botend.services.simc_benchmark_execution import (
    BenchmarkExecutionConflict, cancel_execution, create_execution, reconcile_execution,
    rerun_failed_cases, rerun_case, serialize_incremental_panel_results,
    summarize_execution, summarize_incremental_panel_coverage,
    summarize_panel_coverage_counts, task_progress, _canonical_hash,
)
from botend.services.simc_task_service import TaskValidationUnavailable


from ._shared import SimcCatalogPermissionMixin, _is_simc_admin


def _benchmark_iso(value):
    return value.isoformat() if value is not None else None


def _benchmark_error(code, status, *, fields=None):
    payload = {'success': False, 'error': code}
    if fields:
        payload['fields'] = fields
    return JsonResponse(payload, status=status)


def _benchmark_validation_fields(exc):
    if hasattr(exc, 'message_dict'):
        return {key: [str(item) for item in values]
                for key, values in exc.message_dict.items()}
    return {'non_field_errors': [str(item) for item in exc.messages]}


class _BenchmarkAPIError(Exception):
    def __init__(self, code, status):
        self.code = code
        self.status = status
        super().__init__(code)


def _benchmark_json_object(request, *, empty=False, allowed_fields=None):
    content_type = (request.META.get('CONTENT_TYPE') or '').split(';', 1)[0].strip().lower()
    if content_type != 'application/json':
        raise _BenchmarkAPIError('unsupported_media_type', 415)

    def reject_non_json_constant(value):
        raise ValueError(value)

    try:
        payload = json.loads(
            request.body.decode('utf-8'), parse_constant=reject_non_json_constant,
        )
    except (UnicodeDecodeError, ValueError):
        raise ValidationError({'body': ['请求体必须是有效 JSON 对象']})
    if not isinstance(payload, dict):
        raise ValidationError({'body': ['请求体必须是 JSON 对象']})
    if empty and payload:
        raise _BenchmarkAPIError('unknown_fields', 400)
    if allowed_fields is not None and set(payload).difference(allowed_fields):
        raise _BenchmarkAPIError('unknown_fields', 400)
    return payload


class _BenchmarkReadAPIView(View):
    """Authenticated Benchmark result access for users granted the page capability."""

    @method_decorator(login_required)
    def dispatch(self, request, *args, **kwargs):
        if not has_dashboard_permission(request.user, 'simc.benchmarks'):
            return _benchmark_error('forbidden', 403)
        return super().dispatch(request, *args, **kwargs)


class _BenchmarkAdminAPIView(View):
    """Benchmark Dashboard API access controlled by the page capability."""

    @method_decorator(login_required)
    def dispatch(self, request, *args, **kwargs):
        if not has_dashboard_permission(request.user, 'simc.benchmarks'):
            return _benchmark_error('forbidden', 403)
        try:
            return super().dispatch(request, *args, **kwargs)
        except _BenchmarkAPIError as exc:
            return _benchmark_error(exc.code, exc.status)
        except ValidationError as exc:
            return _benchmark_error('validation_error', 400,
                                    fields=_benchmark_validation_fields(exc))
        except BenchmarkExecutionConflict:
            return _benchmark_error('execution_conflict', 409)
        except TaskValidationUnavailable:
            return _benchmark_error('service_unavailable', 503)
        except PermissionDenied:
            return _benchmark_error('forbidden', 403)
        except ProtectedError:
            return _benchmark_error(
                'protected_resource', 400,
                fields={'panel': ['Panel 被受保护资源引用，无法删除']},
            )
        except Exception:
            logger.exception('Benchmark Dashboard API unexpected failure')
            return _benchmark_error('internal_error', 500)

    def http_method_not_allowed(self, request, *args, **kwargs):
        methods = self._allowed_methods()
        response = _benchmark_error('method_not_allowed', 405)
        response['Allow'] = ', '.join(methods)
        return response

    @staticmethod
    def panel_or_404(panel_id, *, fields=()):
        queryset = SimcBenchmarkPanel.objects.filter(pk=panel_id)
        panel = (queryset.only(*fields) if fields else queryset).first()
        if panel is None:
            return None, _benchmark_error('not_found', 404)
        if panel_has_active_purge(panel.id):
            return None, _benchmark_error('panel_purge_in_progress', 409)
        return panel, None


class _SimcOptionsAPIView(View):
    """资源目录同时要求基准测试页面授权和 SimC 管理身份。"""

    @method_decorator(login_required)
    def dispatch(self, request, *args, **kwargs):
        if not _is_simc_admin(request.user) or not has_dashboard_permission(request.user, 'simc.benchmarks'):
            return _benchmark_error('forbidden', 403)
        return super().dispatch(request, *args, **kwargs)

    @staticmethod
    def panel_or_404(panel_id):
        panel = SimcBenchmarkPanel.objects.filter(pk=panel_id).first()
        if panel is None:
            return None, _benchmark_error('not_found', 404)
        return panel, None

    def http_method_not_allowed(self, request, *args, **kwargs):
        methods = self._allowed_methods()
        response = _benchmark_error('method_not_allowed', 405)
        response['Allow'] = ', '.join(methods)
        return response


def _benchmark_panel_summary(panel, execution=None, panel_coverage=None):
    data = {
        'id': panel.pk, 'slug': panel.slug, 'name': panel.name,
        'is_active': panel.is_active, 'is_public': panel.is_public,
        'schedule_enabled': panel.schedule_enabled,
        'interval_seconds': panel.interval_seconds,
        'next_run_at': _benchmark_iso(panel.next_run_at),
        'last_scheduled_at': _benchmark_iso(panel.last_scheduled_at),
        'published_execution_id': panel.published_execution_id,
        'aggregate_baseline_execution_id': panel.aggregate_baseline_execution_id,
        'counts': {
            'specs': panel.spec_count, 'scenarios': panel.scenario_count,
            'profiles': panel.profile_count, 'candidates': panel.candidate_count,
        },
    }
    data['execution'] = execution
    if panel_coverage is not None:
        data['panel_coverage'] = panel_coverage
    return data


def _benchmark_execution_list_summary(execution, panel, case_counts=None, run_counts=None):
    """Project list progress from aggregate counts only; details stay on execution APIs."""
    if execution is None:
        return None
    case_counts = dict(case_counts or {
        key: 0 for key in ('pending', 'running', 'success', 'partial', 'failed', 'cancelled')
    })
    run_counts = run_counts or {
        key: 0 for key in ('pending', 'running', 'success', 'failed', 'cancelled')
    }
    snapshot = execution.config_snapshot if isinstance(execution.config_snapshot, dict) else {}
    materialized_runs = sum(run_counts.values())
    total_runs = snapshot.get('run_count')
    if type(total_runs) is not int or total_runs < 0:
        total_runs = materialized_runs
    total_cases = snapshot.get('case_count')
    if type(total_cases) is not int or total_cases < 0:
        total_cases = sum(case_counts.values())
    counted_cases = sum(case_counts.values())
    if counted_cases < total_cases:
        case_counts['pending'] += total_cases - counted_cases
    ended_runs = run_counts['success'] + run_counts['failed'] + run_counts['cancelled']
    progress = int(ended_runs * 100 / total_runs) if total_runs else 0
    return {
        'id': execution.pk,
        'panel_id': execution.panel_id,
        'trigger': execution.trigger,
        'status': execution.status,
        'scheduled_slot': _benchmark_iso(execution.scheduled_slot),
        'created_at': _benchmark_iso(execution.created_at),
        'completed_at': _benchmark_iso(execution.completed_at),
        'total_cases': total_cases,
        'total_runs': total_runs,
        'materialized_runs': materialized_runs,
        'progress': progress,
        'counts': case_counts,
        'run_counts': run_counts,
        'current_cases': [],
        'failures': [],
        'metadata': {
            'config_frozen': _benchmark_config_frozen(execution),
            'task_bindings': 0,
            'task_total': total_cases,
            'results_available': False,
        },
        'is_active': panel.active_execution_id == execution.pk,
        'is_published': panel.published_execution_id == execution.pk,
    }


def _benchmark_config_frozen(execution):
    snapshot = execution.config_snapshot
    try:
        return (
            isinstance(snapshot, dict)
            and isinstance(execution.config_hash, str)
            and bool(execution.config_hash)
            and execution.config_hash == _canonical_hash(snapshot)
        )
    except (TypeError, ValueError):
        return False


def _benchmark_execution_metadata(execution, cases, total_cases=None):
    """Expose aggregate lifecycle only; never rebuild results from Task/Run here."""
    expected_tasks = len(cases) if total_cases is None else total_cases
    return {
        'config_frozen': _benchmark_config_frozen(execution),
        'task_bindings': sum(1 for case in cases if case.task_id is not None),
        'task_total': expected_tasks,
        'results_available': (
            len(cases) == expected_tasks
            and execution.status == SimcBenchmarkExecution.STATUS_SUCCESS
            and execution.completed_at is not None
            and execution.results_finalized_at is not None
            and isinstance(execution.result_hash, str)
            and len(execution.result_hash) == 64
        ),
    }


def _benchmark_progress_case_queryset():
    """Load only lifecycle fields used by the live Dashboard projection."""
    return SimcBenchmarkCase.objects.select_related('task').only(
        'id', 'execution_id', 'task_id', 'status', 'error_detail',
        'spec_key', 'scenario_key', 'profile_key',
        'spec_label', 'scenario_label', 'profile_label',
        'task__id', 'task__current_status', 'task__ext', 'task__error_detail',
    ).prefetch_related(
        models.Prefetch('task__simulation_runs', queryset=SimulationRun.objects.only(
            'id', 'task_id', 'status', 'error_detail',
        )),
        models.Prefetch('task__artifacts', queryset=SimcTaskArtifact.objects.filter(
            artifact_type='html_report',
        ).only('id', 'task_id', 'artifact_type')),
    ).order_by('id')


def _benchmark_failure_rows(execution, cases):
    """Project authoritative Case/Task failures for list and history surfaces."""
    failures = []
    for case in cases:
        task = case.task if case.task_id else None
        run_error = ''
        if task is not None:
            failed_run = next((
                run for run in task.simulation_runs.all()
                if run.status == 'failed' and run.error_detail
            ), None)
            run_error = failed_run.error_detail if failed_run is not None else ''
        error = _benchmark_safe_string(
            case.error_detail or (task.error_detail if task is not None else '') or run_error,
        )
        if not error:
            continue
        report_artifact = None
        if task is not None:
            report_artifact = next((
                artifact for artifact in task.artifacts.all()
                if artifact.artifact_type == 'html_report'
            ), None)
        failures.append({
            'case_id': case.pk,
            'task_id': case.task_id,
            'labels': {
                'spec': _benchmark_safe_key(case.spec_label),
                'scenario': _benchmark_safe_key(case.scenario_label),
                'profile': _benchmark_safe_key(case.profile_label),
            },
            'error': error,
            'report_url': (
                f'/api/simc-workbench/artifacts/{report_artifact.id}/preview/'
                if report_artifact is not None else ''
            ),
            'detail_url': f'/dashboard/simc/benchmarks/executions/{execution.pk}/',
        })
    return failures


def _benchmark_execution_progress(execution, cases, *, is_active=False, case_count=None):
    cases = list(cases)
    if case_count is None:
        snapshot = execution.config_snapshot
        declared_cases = snapshot.get('case_count') if isinstance(snapshot, dict) else None
        total_cases = (declared_cases if type(declared_cases) is int and declared_cases >= 0
                       else len(cases))
    else:
        total_cases = _benchmark_safe_count(case_count)
    total_cases = max(total_cases, len(cases))
    statuses = ('pending', 'running', 'success', 'partial', 'failed', 'cancelled')
    counts = {status: 0 for status in statuses}
    progress_values = []
    current_cases = []
    run_counts = {status: 0 for status in ('pending', 'running', 'success', 'failed', 'cancelled')}
    total_runs = 0
    for case in cases:
        status = case.status if case.status in counts else 'failed'
        counts[status] += 1
        for run in case.task.simulation_runs.all() if case.task_id else ():
            run_status = {'completed': 'success'}.get(run.status, run.status)
            if run_status not in run_counts:
                run_status = 'failed'
            run_counts[run_status] += 1
            total_runs += 1
        if status == 'pending':
            progress = 0
        elif status == 'running':
            progress = task_progress(case.task)
            progress = progress if progress is not None else 0
            current_cases.append({
                'task_id': case.task_id,
                'spec': case.spec_label,
                'scenario': case.scenario_label,
                'profile': case.profile_label,
                'progress': progress,
            })
        else:
            progress = 100
        progress_values.append(progress)
    counts['pending'] += total_cases - len(cases)
    progress = round(sum(progress_values) / total_cases) if total_cases else 0
    return {
        'id': execution.pk,
        'status': execution.status,
        'is_active': is_active,
        'progress': progress,
        'case_count': total_cases,
        # Snapshot run_count is the frozen, scheduled workload.  Runs are
        # materialized lazily by comparison Tasks, so a live supplement can have
        # fewer rows than this until every Task has been consumed.
        'total_runs': _benchmark_safe_count(
            (execution.config_snapshot or {}).get('run_count')
            if isinstance(execution.config_snapshot, dict) else total_runs
        ),
        'materialized_runs': total_runs,
        'run_counts': run_counts,
        'counts': counts,
        'current_cases': current_cases[:3],
        'failures': _benchmark_failure_rows(execution, cases),
        'metadata': _benchmark_execution_metadata(execution, cases, total_cases),
        'created_at': _benchmark_iso(execution.created_at),
        'completed_at': _benchmark_iso(execution.completed_at),
    }


def _benchmark_execution_summary(execution, *, published_id=None, case_count=None, cases=None):
    snapshot = execution.config_snapshot if isinstance(execution.config_snapshot, dict) else {}
    snapshot_cases, snapshot_runs = snapshot.get('case_count'), snapshot.get('run_count')
    data = {
        'id': execution.pk, 'panel_id': execution.panel_id, 'trigger': execution.trigger,
        'status': execution.status,
        'scheduled_slot': _benchmark_iso(execution.scheduled_slot),
        'created_at': _benchmark_iso(execution.created_at),
        'completed_at': _benchmark_iso(execution.completed_at),
        'case_count': (snapshot_cases if type(snapshot_cases) is int
                       else (case_count if case_count is not None else 0)),
        'run_count': snapshot_runs if type(snapshot_runs) is int else 0,
        'is_published': execution.pk == published_id,
    }
    if cases is not None:
        cases = list(cases)
        data.update(_benchmark_execution_progress(
            execution, cases, case_count=data['case_count'],
        ))
        data['preflight_failures'] = [{
            'coordinate': {
                'spec_key': _benchmark_safe_key(case.spec_key),
                'scenario_key': _benchmark_safe_key(case.scenario_key),
                'profile_key': _benchmark_safe_key(case.profile_key),
            },
            'labels': {
                'spec': _benchmark_spec_display_name(case.spec_label, case.spec_key),
                'scenario': _benchmark_safe_key(case.scenario_label),
                'profile': _benchmark_safe_key(case.profile_label),
            },
            'error': _benchmark_safe_string(case.error_detail),
        } for case in cases if case.task_id is None and case.status == 'failed']
        data['panel_id'] = execution.panel_id
        data['trigger'] = execution.trigger
        data['scheduled_slot'] = _benchmark_iso(execution.scheduled_slot)
        data['run_count'] = snapshot_runs if type(snapshot_runs) is int else 0
        data['is_published'] = execution.pk == published_id
    return data


def _benchmark_safe_string(value, *, limit=240):
    """Return a bounded scalar with path/traceback details conservatively redacted."""
    if not isinstance(value, str):
        return None
    text = ' '.join(value.split())
    if not text:
        return None
    if re.search(r'(?i)traceback', text):
        return '[redacted]'
    text = re.sub(
        r'(?:[A-Za-z]:[\\/]|/)(?:[^\s;:,]+[\\/])*[^\s;:,]*',
        '[redacted]', text,
    )
    return text[:limit]


def _benchmark_safe_key(value):
    return _benchmark_safe_string(value, limit=200) or ''


def _benchmark_safe_error_log(value, *, limit=12000):
    """Keep useful multiline diagnostics while removing local paths and secrets."""
    if not isinstance(value, str):
        return None
    text = value.replace('\x00', '').replace('\r\n', '\n').replace('\r', '\n').strip()
    if not text:
        return None
    text = re.sub(
        r'(?i)((?:https?|ftp)://)[^\s/@:]+:[^\s/@]+@',
        r'\1[redacted]@', text,
    )
    text = re.sub(
        r'(?i)((?:authorization)\s*[:=]\s*)bearer(?:\s+token)?\s+\S+',
        r'\1Bearer [redacted]', text,
    )
    text = re.sub(
        r'(?i)(\bbearer(?:\s+token)?\s+)\S+',
        r'\1[redacted]', text,
    )
    text = re.sub(
        r'(?i)((?:access[_-]?token|client[_-]?secret|api[_-]?key|password|passwd|secret|token)\s*[:=]\s*)\S+',
        r'\1[redacted]', text,
    )
    text = re.sub(
        r'(?i)((?:authorization)\s*[:=]\s*)\S+',
        r'\1[redacted]', text,
    )
    text = re.sub(
        r'(?:[A-Za-z]:[\\/]|/)(?:[^\s;:,]+[\\/])*[^\s;:,]*',
        '[redacted]', text,
    )
    if len(text) > limit:
        text = text[:limit] + '\n...[truncated]'
    return text


def _benchmark_spec_display_name(value, spec_key=None):
    text = _benchmark_safe_key(value)
    normalized = re.sub(r'[^a-z0-9]', '', text.lower())
    names = {
        re.sub(r'[^a-z0-9]', '', name.lower()): label
        for name, label in SPEC_CN.items()
    }
    direct = names.get(normalized)
    if not direct:
        for name, label in sorted(names.items(), key=lambda item: -len(item[0])):
            if normalized.endswith(name):
                direct = label
                break
    if not direct:
        normalized_key = re.sub(r'[^a-z0-9]', '', _benchmark_safe_key(spec_key).lower())
        for name, label in sorted(names.items(), key=lambda item: -len(item[0])):
            if normalized_key.endswith(name):
                direct = label
                break
    if not direct:
        return text

    normalized_key = re.sub(r'[^a-z0-9]', '', _benchmark_safe_key(spec_key).lower())
    for name, label in CLASS_CN.items():
        if normalized_key.startswith(re.sub(r'[^a-z0-9]', '', name.lower())):
            return f'{direct}-{label}'
    return direct


def _benchmark_safe_count(value):
    return value if type(value) is int and value >= 0 else 0


def _benchmark_safe_dps(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float('inf'), float('-inf')):
        return None
    return value


def _benchmark_safe_detail(summary, execution):
    """Recursively project service output; no nested object is passed through."""
    summary = summary if isinstance(summary, dict) else {}
    cases = []
    source_cases = summary.get('cases')
    if not isinstance(source_cases, list):
        source_cases = []
    case_statuses = frozenset(('pending', 'running', 'success', 'partial', 'failed', 'cancelled'))
    run_statuses = frozenset(('pending', 'running', 'success', 'failed', 'cancelled'))
    for row in source_cases:
        if not isinstance(row, dict):
            continue
        labels = row.get('labels') if isinstance(row.get('labels'), dict) else {}
        source_runs = row.get('runs') if isinstance(row.get('runs'), list) else []
        runs = []
        for run in source_runs:
            if not isinstance(run, dict):
                continue
            status = run.get('status')
            runs.append({
                'key': _benchmark_safe_key(run.get('key')),
                'label': _benchmark_safe_key(run.get('label')),
                'status': status if status in run_statuses else 'failed',
                'dps': _benchmark_safe_dps(run.get('dps')),
                'error': _benchmark_safe_string(run.get('error')),
                'error_log': _benchmark_safe_error_log(run.get('error_log')),
            })
        status = row.get('status')
        task_id = row.get('task_id')
        cases.append({
            'coordinate': {
                'spec_key': _benchmark_safe_key(row.get('spec_key')),
                'scenario_key': _benchmark_safe_key(row.get('scenario_key')),
                'profile_key': _benchmark_safe_key(row.get('profile_key')),
            },
            'labels': {
                'spec': _benchmark_spec_display_name(
                    labels.get('spec'), row.get('spec_key'),
                ),
                'scenario': _benchmark_safe_key(labels.get('scenario')),
                'profile': _benchmark_safe_key(labels.get('profile')),
            },
            'status': status if status in case_statuses else 'failed',
            'task_id': task_id if type(task_id) is int and task_id > 0 else None,
            'task_status': row.get('task_status') if row.get('task_status') in run_statuses else None,
            'task_status_label': _benchmark_safe_key(row.get('task_status_label')) or None,
            'task_progress': row.get('task_progress') if type(row.get('task_progress')) is int and 0 <= row.get('task_progress') <= 100 else None,
            'error': _benchmark_safe_string(row.get('error')),
            'error_log': _benchmark_safe_error_log(row.get('error_log')),
            'runs': runs,
        })
    count_keys = ('pending', 'running', 'success', 'partial', 'failed', 'cancelled')
    run_count_keys = ('pending', 'running', 'success', 'failed', 'cancelled')
    source_run_counts = summary.get('run_counts')
    if not isinstance(source_run_counts, dict):
        source_run_counts = {}
    materialized_runs = _benchmark_safe_count(summary.get('total_runs'))
    snapshot = execution.config_snapshot
    planned_runs = (snapshot.get('run_count') if isinstance(snapshot, dict) else None)
    total_runs = planned_runs if type(planned_runs) is int and planned_runs >= 0 else materialized_runs
    status = summary.get('status')
    declared_cases = snapshot.get('case_count') if isinstance(snapshot, dict) else None
    total_cases = (declared_cases if type(declared_cases) is int and declared_cases >= 0
                   else _benchmark_safe_count(summary.get('total_cases')))
    total_cases = max(total_cases, len(cases))
    progress_values = []
    current_cases = []
    for case in cases:
        case_status = case['status']
        if case_status == 'pending':
            progress = 0
        elif case_status == 'running':
            progress = case['task_progress'] if case['task_progress'] is not None else 0
            current_cases.append({
                'task_id': case['task_id'],
                'spec': case['labels']['spec'],
                'scenario': case['labels']['scenario'],
                'profile': case['labels']['profile'],
                'progress': progress,
            })
        else:
            progress = 100
        progress_values.append(progress)
    progress = round(sum(progress_values) / total_cases) if total_cases else 0
    safe_counts = {
        key: _benchmark_safe_count(summary.get(key)) for key in count_keys
    }
    counted_cases = sum(safe_counts.values())
    if counted_cases < total_cases:
        safe_counts['pending'] += total_cases - counted_cases
    return {
        'id': execution.pk, 'panel_id': execution.panel_id,
        'is_targeted_rerun': isinstance(snapshot, dict) and snapshot.get('result_publication') == 'atomic_targeted',
        'trigger': execution.trigger,
        'status': status if status in case_statuses else 'failed',
        'scheduled_slot': _benchmark_iso(execution.scheduled_slot),
        'created_at': _benchmark_iso(execution.created_at),
        'completed_at': _benchmark_iso(execution.completed_at),
        'total_cases': total_cases,
        'total_runs': total_runs,
        'materialized_runs': materialized_runs,
        'progress': progress,
        'counts': safe_counts,
        'run_counts': {
            key: _benchmark_safe_count(source_run_counts.get(key))
            for key in run_count_keys
        },
        'current_cases': current_cases[:3],
        'metadata': {
            'config_frozen': _benchmark_config_frozen(execution),
            'task_bindings': sum(1 for case in cases if case['task_id'] is not None),
            'task_total': total_cases,
            'results_available': (
                len(cases) == total_cases
                and status == SimcBenchmarkExecution.STATUS_SUCCESS
                and execution.completed_at is not None
                and execution.results_finalized_at is not None
                and isinstance(execution.result_hash, str)
                and len(execution.result_hash) == 64
            ),
        },
        'is_active': execution.panel.active_execution_id == execution.pk,
        'is_published': execution.panel.published_execution_id == execution.pk,
        'cases': cases,
    }


class SimcBenchmarkPanelListAPIView(_BenchmarkAdminAPIView):
    def get(self, request):
        latest_execution = SimcBenchmarkExecution.objects.filter(
            panel_id=models.OuterRef('pk'),
        ).order_by('-created_at', '-id').values('pk')[:1]
        rows = list(SimcBenchmarkPanel.objects.annotate(
            spec_count=models.Count('specs', distinct=True),
            scenario_count=models.Count('scenarios', distinct=True),
            profile_count=models.Count('specs__profiles', distinct=True),
            candidate_count=models.Count('candidates', distinct=True),
            dashboard_latest_execution_id=models.Subquery(latest_execution),
        ).order_by('name', 'id'))
        execution_ids = {
            panel.active_execution_id or panel.dashboard_latest_execution_id
            for panel in rows
            if panel.active_execution_id or panel.dashboard_latest_execution_id
        }
        # This PK lookup only builds a dictionary. Do not filesort large frozen
        # JSON snapshots using the model's default history ordering.
        executions = SimcBenchmarkExecution.objects.filter(pk__in=execution_ids).order_by().only(
            'id', 'panel_id', 'trigger', 'status', 'scheduled_slot',
            'created_at', 'completed_at', 'config_snapshot', 'config_hash',
        )
        execution_by_id = {execution.pk: execution for execution in executions}
        case_count_keys = ('pending', 'running', 'success', 'partial', 'failed', 'cancelled')
        case_counts_by_execution = {
            row['execution_id']: {key: row[key] for key in case_count_keys}
            for row in SimcBenchmarkCase.objects.filter(
                execution_id__in=execution_ids,
            ).values('execution_id').annotate(**{
                key: models.Count('id', filter=models.Q(status=key))
                for key in case_count_keys
            })
        }
        active_execution_ids = {
            panel.active_execution_id for panel in rows if panel.active_execution_id
        }
        task_status_map = {
            0: 'pending', 1: 'running', 2: 'success',
            3: 'failed', 4: 'running', 5: 'cancelled',
        }
        live_task_counts_by_execution = {
            execution_id: {key: 0 for key in case_count_keys}
            for execution_id in active_execution_ids
        }
        for task_row in SimcBenchmarkCase.objects.filter(
            execution_id__in=active_execution_ids,
            task_id__isnull=False,
        ).values('execution_id', 'task__current_status').annotate(total=models.Count('id')):
            status = task_status_map.get(task_row['task__current_status'], 'pending')
            live_task_counts_by_execution[task_row['execution_id']][status] += task_row['total']
        run_count_keys = ('pending', 'running', 'success', 'failed', 'cancelled')
        run_counts_by_execution = {
            row['task__benchmark_case__execution_id']: {key: row[key] for key in run_count_keys}
            for row in SimulationRun.objects.filter(
                task__benchmark_case__execution_id__in=execution_ids,
            ).values('task__benchmark_case__execution_id').annotate(**{
                key: models.Count('id', filter=models.Q(
                    status__in=('completed', 'success') if key == 'success' else (key,),
                ))
                for key in run_count_keys
            })
        }
        return JsonResponse({'success': True, 'data': [
            _benchmark_panel_summary(
                panel,
                _benchmark_execution_list_summary(
                    execution_by_id.get(
                        panel.active_execution_id or panel.dashboard_latest_execution_id,
                    ),
                    panel,
                    live_task_counts_by_execution.get(panel.active_execution_id)
                    if panel.active_execution_id
                    else case_counts_by_execution.get(panel.dashboard_latest_execution_id),
                    run_counts_by_execution.get(
                        panel.active_execution_id or panel.dashboard_latest_execution_id,
                    ),
                ),
            )
            for panel in rows
        ]})

    def post(self, request):
        panel, _plan = replace_panel_config(_benchmark_json_object(request), request.user.id)
        data = serialize_panel_config(panel)
        data['next_run_at'] = _benchmark_iso(data['next_run_at'])
        return JsonResponse({'success': True, 'data': data}, status=201)


class SimcBenchmarkPanelCoverageAPIView(_BenchmarkAdminAPIView):
    """Load lightweight aggregate result coverage for one panel at a time."""

    def get(self, request, panel_id):
        panel, error = self.panel_or_404(panel_id)
        if error:
            return error
        coverage = summarize_panel_coverage_counts([panel])[panel.pk]
        return JsonResponse({
            'success': True,
            'data': coverage,
        })


def _benchmark_spec_options():
    """Project the maintained WoW catalog, constrained by the executable spec set."""
    rows = []
    for class_display, spec_displays in CLASS_SPEC_MAP.items():
        for spec_display in spec_displays:
            spec_input = re.sub(r'(?<!^)(?=[A-Z])', '_', spec_display).lower()
            class_name, spec_name = canonical_simc_spec_identity(
                f'{class_display}_{spec_input}',
            )
            value = f'{class_name}_{spec_name}'
            if (class_name, spec_name) not in SUPPORTED_SIMC_SPEC_IDENTITIES:
                continue
            class_label = CLASS_CN.get(class_display, class_display)
            spec_label = SPEC_CN.get(spec_display, spec_display)
            rows.append({
                'value': value, 'spec_key': value, 'class_name': class_name,
                'class_label': class_label, 'spec_label': spec_label,
                'label': f'{class_label} · {spec_label}',
                'role': SPEC_ROLE[(class_display, spec_display)],
            })
    return rows


def _benchmark_backend_game_versions(backends):
    """读取后端自身记录的 WoW build；字段词典不承担版本映射。"""
    return {
        backend.pk: str(backend.game_build or '').strip()
        for backend in backends if str(backend.game_build or '').strip()
    }


def _benchmark_resource_spec_key(row, *, allow_generic=False):
    """Return the same authoritative class/spec tag used by Profile APIs."""
    raw_spec = str(row.spec or '').strip().lower()
    if allow_generic and raw_spec in {'', 'generic', 'default', 'all', '*'}:
        return ''
    resolved = canonical_simc_profile_identity(raw_spec, getattr(row, 'class_name', ''))
    if resolved not in SUPPORTED_SIMC_SPEC_IDENTITIES:
        return ''
    return f'{resolved[0]}_{resolved[1]}'


def _benchmark_create_defaults(resources, specs):
    """Resolve one authoritative default bundle per spec without browser guesswork."""
    production_backends = [row for row in resources['backends'] if row.identifier == 'production']
    result = {}
    for spec in specs:
        spec_key, class_name = spec['value'], spec['class_name']
        apls = [row for row in resources['apls'] if (
            row.is_system and row.owner_user_id is None
            and _benchmark_resource_spec_key(row) == spec_key
            and (not row.class_name or row.class_name == class_name)
        )]
        exact_templates = [row for row in resources['templates'] if (
            _benchmark_resource_spec_key(row, allow_generic=True) == spec_key
            and (not row.class_name or row.class_name == class_name)
        )]
        generic_templates = [row for row in resources['templates'] if (
            _benchmark_resource_spec_key(row, allow_generic=True) == ''
            and (not row.class_name or row.class_name == class_name)
        )]
        templates = exact_templates if exact_templates else generic_templates
        profiles = [row for row in resources['profiles'] if (
            row.user_id is None and row.source == SimcProfile.SOURCE_SIMC_UPSTREAM
            and bool(row.system_key) and _benchmark_resource_spec_key(row) == spec_key
        )]
        problems = []
        for label, rows in (('正式 Backend', production_backends), ('系统默认 APL', apls),
                            ('基础 Template', templates)):
            if len(rows) != 1:
                problems.append(f'{label}{"缺少" if not rows else "不唯一"}')
        if not profiles:
            problems.append('系统默认 Profile缺少')
        if problems:
            result[spec_key] = {'available': False, 'reason': '、'.join(problems)}
        else:
            default_profile = profiles[0]
            result[spec_key] = {
                'available': True, 'backend_id': production_backends[0].pk,
                'apl_id': apls[0].pk,
                'template_id': templates[0].pk, 'profile_id': default_profile.pk,
                'profile_label': default_profile.name,
            }
    return result


def _benchmark_options_payload(owner_id=None, ownership_context=None):
    # Benchmark resource selection is global. Keep the parameters temporarily
    # for call-site/API compatibility, but never use them to scope content.
    querysets = benchmark_resource_querysets()
    querysets['apls'] = querysets['apls'].defer('content', 'validation_diagnostics')
    querysets['templates'] = querysets['templates'].defer('content')
    resources = {name: list(queryset) for name, queryset in querysets.items()}
    apply_talent_display_names(resources['talent_strings'])
    specs = _benchmark_spec_options()
    backend_game_versions = _benchmark_backend_game_versions(resources['backends'])
    consumable_values = {
        key: {'none'} for key in (
            'flask', 'potion', 'food', 'augmentation',
            'temporary_enchant_main_hand', 'temporary_enchant_off_hand',
        )
    }
    for profile in resources['profiles']:
        for raw_line in str(profile.player_equipment or '').splitlines():
            key, separator, raw_value = raw_line.strip().partition('=')
            value = raw_value.strip()
            if not separator or not value:
                continue
            if key in consumable_values:
                consumable_values[key].add(value)
            elif key == 'temporary_enchant':
                for entry in value.split('/'):
                    hand, hand_separator, enchant = entry.partition(':')
                    target = f'temporary_enchant_{hand.strip().lower()}'
                    if hand_separator and target in consumable_values and enchant.strip():
                        consumable_values[target].add(enchant.strip())
    return {
        'specs': specs,
        'fight_styles': [
            {'value': value, 'label': label} for value, label in SIMC_FIGHT_STYLES
        ],
        'raid_buffs': [
            {
                'value': value,
                'label': label,
                'simc_option': f'override.{value}',
            }
            for value, label in SIMC_RAID_BUFFS
        ],
        'extra_options': [
            {
                'value': option['value'], 'label': option['label'],
                'description': option['description'],
            }
            for option in SIMC_EXTRA_OPTIONS
        ],
        'consumables': {
            key: [
                ({'value': value, 'label': '无'} if value == 'none'
                 else simc_consumable_option(value))
                for value in sorted(values, key=lambda item: (item != 'none', item))
            ]
            for key, values in consumable_values.items()
        },
        'create_defaults': _benchmark_create_defaults(resources, specs),
        'resources': {
            'backends': [{
                'id': row.pk, 'identifier': row.identifier, 'name': row.name,
                'platform': row.platform, 'version': row.current_version,
                'game_version': backend_game_versions.get(row.pk, ''),
                'is_default': row.identifier == 'production',
            } for row in resources['backends']],
            'templates': [{
                'id': row.pk, 'name': row.name, 'spec': row.spec,
                'spec_key': _benchmark_resource_spec_key(row, allow_generic=True),
                'canonical_spec': _benchmark_resource_spec_key(row, allow_generic=True),
                'class_name': row.class_name, 'source': row.source,
                'is_system': row.owner_user_id is None,
            } for row in resources['templates']],
            'apls': [{
                'id': row.pk, 'name': row.name, 'spec': row.spec,
                'spec_key': _benchmark_resource_spec_key(row),
                'canonical_spec': _benchmark_resource_spec_key(row),
                'class_name': row.class_name, 'source': row.source,
                'is_system': bool(row.is_system or row.owner_user_id is None),
                'validation_status': row.validation_status,
            } for row in resources['apls']],
            'profiles': [{
                'id': row.pk, 'name': normalize_hero_subtree_title_zh(row.name),
                'spec': row.spec,
                'spec_key': _benchmark_resource_spec_key(row),
                'canonical_spec': _benchmark_resource_spec_key(row),
                'class_name': row.class_name, 'source': row.source,
                'is_system': row.user_id is None,
                'is_default': row.user_id is None,
            } for row in resources['profiles']],
            'talent_strings': [{
                'id': row.pk, 'name': row.name, 'display_name': row.display_name,
                'build_code': row.talent,
                'spec': row.spec,
                'spec_key': _benchmark_resource_spec_key(row),
                'canonical_spec': _benchmark_resource_spec_key(row),
                'hero_talent_names': normalize_hero_subtree_names_zh(row.hero_talent_names),
                'default_apl_id': row.default_apl_id,
                'is_system': bool(row.is_system or row.owner_user_id is None),
            } for row in resources['talent_strings']],
        },
        'limits': {
            'max_specs': MAX_SPECS,
            'max_profiles_per_spec': MAX_PROFILES_PER_SPEC,
            'max_scenarios': MAX_SCENARIOS,
        },
        'ownership_context': 'benchmark_global',
    }


class SimcBenchmarkOptionsAPIView(_SimcOptionsAPIView):
    def get(self, request):
        return JsonResponse({
            'success': True,
            'data': _benchmark_options_payload(request.user.id, 'current_user'),
        })


@method_decorator(login_required, name='dispatch')
class SimcFightStyleOptionsAPIView(SimcCatalogPermissionMixin, View):
    """Authoritative localized SimC fight-style catalog for every task surface."""

    def get(self, request):
        return JsonResponse({'success': True, 'data': [
            {'value': value, 'label': label} for value, label in SIMC_FIGHT_STYLES
        ]})


@method_decorator(login_required, name='dispatch')
class SimcRaidBuffOptionsAPIView(SimcCatalogPermissionMixin, View):
    """Small shared catalog for regular simulations and benchmark configuration."""

    def get(self, request):
        from botend.services.simc_composer import SIMC_CLASS_RAID_BUFFS
        return JsonResponse({'success': True, 'data': [
            {
                'value': value,
                'label': label,
                'simc_option': f'override.{value}',
                'default_classes': [
                    class_name for class_name, buffs in SIMC_CLASS_RAID_BUFFS.items()
                    if value in buffs
                ],
            }
            for value, label in SIMC_RAID_BUFFS
        ]})


@method_decorator(login_required, name='dispatch')
class SimcExtraOptionsAPIView(SimcCatalogPermissionMixin, View):
    """Authoritative catalog for structured, extensible SimC options."""

    def get(self, request):
        return JsonResponse({'success': True, 'data': [
            {
                'value': option['value'],
                'label': option['label'],
                'description': option['description'],
            }
            for option in SIMC_EXTRA_OPTIONS
        ]})


class SimcBenchmarkPanelOptionsAPIView(_SimcOptionsAPIView):
    def get(self, request, panel_id):
        panel, error = self.panel_or_404(panel_id)
        if error:
            return error
        return JsonResponse({
            'success': True,
            'data': _benchmark_options_payload(panel.created_by_id, 'panel_creator'),
        })


class SimcBenchmarkItemLookupAPIView(_BenchmarkAdminAPIView):
    def get(self, request):
        raw = (request.GET.get('item_id') or '').strip()
        if not raw:
            return JsonResponse({'success': True, 'data': None})
        if not raw.isdigit() or int(raw) <= 0:
            return _benchmark_error('item_id 必须是正整数', 400)
        item = WowItemSnapshot.objects.filter(item_id=int(raw)).only('item_id', 'name', 'name_zh', 'icon').first()
        if item is None:
            return JsonResponse({'success': True, 'data': None})
        return JsonResponse({'success': True, 'data': {
            'item_id': item.item_id,
            'name': item.name_zh or item.name or f'物品 {item.item_id}',
            'icon': item.icon or '',
        }})


class SimcBenchmarkPanelDetailAPIView(_BenchmarkAdminAPIView):
    def get(self, request, panel_id):
        panel, error = self.panel_or_404(panel_id)
        if error:
            return error
        data = serialize_panel_config(panel)
        data['next_run_at'] = _benchmark_iso(data['next_run_at'])
        return JsonResponse({'success': True, 'data': data})

    def put(self, request, panel_id):
        payload = _benchmark_json_object(request)
        panel, error = self.panel_or_404(panel_id)
        if error:
            return error
        if panel_has_active_purge(panel_id):
            return _benchmark_error('panel_purge_in_progress', 409)
        # The creator is immutable and remains the task owner even when another
        # administrator maintains the globally sourced resource configuration.
        panel, _plan = replace_panel_config(payload, panel.created_by_id, panel=panel)
        data = serialize_panel_config(panel)
        data['next_run_at'] = _benchmark_iso(data['next_run_at'])
        return JsonResponse({'success': True, 'data': data})

    def patch(self, request, panel_id):
        payload = _benchmark_json_object(request, allowed_fields={
            'name', 'description', 'is_active', 'is_public',
            'schedule_enabled', 'interval_seconds', 'next_run_at', 'queue_priority',
        })
        with transaction.atomic():
            panel = SimcBenchmarkPanel.objects.select_for_update().filter(pk=panel_id).first()
            if panel is None:
                return _benchmark_error('not_found', 404)
            if panel_has_active_purge(panel_id):
                return _benchmark_error('panel_purge_in_progress', 409)
            for field in ('name', 'description', 'is_active', 'is_public',
                          'schedule_enabled', 'interval_seconds', 'next_run_at', 'queue_priority'):
                if field in payload:
                    setattr(panel, field, payload[field])
            panel.full_clean()
            panel.save()
        data = serialize_panel_config(panel)
        data['next_run_at'] = _benchmark_iso(data['next_run_at'])
        return JsonResponse({'success': True, 'data': data})

    def delete(self, request, panel_id):
        return _benchmark_error('use_panel_purge', 405)


class SimcBenchmarkPanelPurgeAPIView(_BenchmarkAdminAPIView):
    def get(self, request, panel_id):
        if not _is_simc_admin(request.user):
            return _benchmark_error('forbidden', 403)
        try:
            plan = build_panel_purge_plan(panel_id)
        except SimcBenchmarkPanel.DoesNotExist:
            return _benchmark_error('not_found', 404)
        except PurgeBlocked as exc:
            return JsonResponse({
                'success': False,
                'error': 'purge_blocked',
                'message': str(exc),
                'data': exc.data,
            }, status=409)
        return JsonResponse({'success': True, 'data': plan.public_data()})

    def post(self, request, panel_id):
        if not _is_simc_admin(request.user):
            return _benchmark_error('forbidden', 403)
        payload = _benchmark_json_object(request, allowed_fields={'fingerprint'})
        try:
            job = queue_panel_purge(
                panel_id,
                payload.get('fingerprint'),
                request.user.id,
            )
        except SimcBenchmarkPanel.DoesNotExist:
            return _benchmark_error('not_found', 404)
        except PurgeBlocked as exc:
            return JsonResponse({
                'success': False,
                'error': 'purge_blocked',
                'message': str(exc),
                'data': exc.data,
            }, status=409)
        except PurgeValidation as exc:
            return JsonResponse({
                'success': False,
                'error': 'invalid_fingerprint',
                'message': str(exc),
            }, status=400)
        except PurgeConflict as exc:
            return JsonResponse({
                'success': False,
                'error': 'panel_purge_conflict',
                'message': str(exc),
            }, status=409)
        return JsonResponse({'success': True, 'data': {
            'id': job.id,
            'panel_id': job.panel_id_snapshot,
            'panel_name': job.panel_name,
            'status': job.status,
            'fingerprint': job.fingerprint,
            'counts': job.plan.get('counts') or {},
        }}, status=202)


class SimcBenchmarkPurgeTaskDetailAPIView(_BenchmarkAdminAPIView):
    def get(self, request, purge_id):
        if not _is_simc_admin(request.user):
            return _benchmark_error('forbidden', 403)
        job = SimcBenchmarkPurgeTask.objects.filter(pk=purge_id).first()
        if job is None:
            return _benchmark_error('not_found', 404)
        messages = {
            SimcBenchmarkPurgeTask.STATUS_CLEANUP_PENDING: '数据库清理已完成，OSS 隔离副本等待重试清理',
            SimcBenchmarkPurgeTask.STATUS_CLEANING: '正在清理 OSS 隔离副本',
            SimcBenchmarkPurgeTask.STATUS_RESTORE_PENDING: '清理未提交，正在等待恢复 OSS 与 Panel',
            SimcBenchmarkPurgeTask.STATUS_RESTORING: '正在恢复 OSS 与 Panel',
            SimcBenchmarkPurgeTask.STATUS_FAILED: '清理失败，Panel 和 OSS 已恢复',
        }
        return JsonResponse({'success': True, 'data': {
            'id': job.id,
            'panel_id': job.panel_id_snapshot,
            'panel_name': job.panel_name,
            'status': job.status,
            'fingerprint': job.fingerprint,
            'counts': job.plan.get('counts') or {},
            'message': messages.get(job.status, ''),
            'created_at': _benchmark_iso(job.created_at),
            'started_at': _benchmark_iso(job.started_at),
            'completed_at': _benchmark_iso(job.completed_at),
        }})


class SimcBenchmarkPanelDuplicateAPIView(_BenchmarkAdminAPIView):
    def post(self, request, panel_id):
        _benchmark_json_object(request, allowed_fields=set())
        panel, error = self.panel_or_404(panel_id)
        if error:
            return error
        copied, _plan = duplicate_panel_config(panel, request.user.id)
        data = serialize_panel_config(copied)
        data['next_run_at'] = _benchmark_iso(data['next_run_at'])
        return JsonResponse({'success': True, 'data': data}, status=201)


class SimcBenchmarkPanelRunAPIView(_BenchmarkAdminAPIView):
    def get(self, request, panel_id):
        from botend.services.simc_benchmark_targeting import rerun_options
        panel, error = self.panel_or_404(panel_id)
        if error:
            return error
        return JsonResponse({'success': True, 'data': rerun_options(build_execution_plan(panel))})

    def post(self, request, panel_id):
        from botend.services.simc_benchmark_targeting import rerun_preview
        payload = _benchmark_json_object(request, allowed_fields={'mode', 'selection', 'preview', 'plan_hash'})
        mode = payload.get('mode', 'supplement')
        if mode not in {'full', 'supplement', 'targeted'}:
            raise ValidationError({'mode': ['必须是 full、supplement 或 targeted']})
        if mode != 'targeted' and set(payload) - {'mode'}:
            raise ValidationError({'mode': ['只有定向重跑允许选择范围和预览']})
        if 'preview' in payload and type(payload['preview']) is not bool:
            raise ValidationError({'preview': ['必须是布尔值']})
        if mode == 'targeted' and (not isinstance(payload.get('plan_hash'), str)
                                   or len(payload['plan_hash']) != 64):
            raise ValidationError({'plan_hash': ['请先加载当前重跑范围']})
        panel, error = self.panel_or_404(panel_id)
        if error:
            return error
        if panel_has_active_purge(panel_id):
            return _benchmark_error('panel_purge_in_progress', 409)
        if not panel.is_active:
            raise ValidationError({'panel': ['Panel 未启用，无法执行']})
        if mode == 'targeted' and payload.get('preview'):
            preview = rerun_preview(build_execution_plan(panel), payload.get('selection'))
            if preview['plan_hash'] != payload['plan_hash']:
                raise BenchmarkExecutionConflict('面板配置已变化，请重新选择重跑范围')
            return JsonResponse({'success': True, 'data': preview})
        extra = {'selection': payload.get('selection'), 'expected_plan_hash': payload.get('plan_hash')} if mode == 'targeted' else {}
        execution = create_execution(
            panel, requested_by=request.user, execution_mode=mode, **extra,
        )
        return JsonResponse({
            'success': True,
            'data': _benchmark_execution_summary(
                execution, published_id=panel.published_execution_id,
                case_count=execution.cases.count(),
                cases=list(_benchmark_progress_case_queryset().filter(execution=execution)),
            ),
        }, status=202)


class SimcBenchmarkPanelExecutionListAPIView(_BenchmarkAdminAPIView):
    def get(self, request, panel_id):
        panel, error = self.panel_or_404(panel_id, fields=('id', 'published_execution_id'))
        if error:
            return error
        try:
            page, size = int(request.GET.get('page', '1')), int(request.GET.get('size', '20'))
        except (TypeError, ValueError):
            raise ValidationError({'pagination': ['page 和 size 必须是整数']})
        if page < 1 or size < 1:
            raise ValidationError({'pagination': ['page 和 size 必须是正整数']})
        size = min(size, 50)
        case_queryset = _benchmark_progress_case_queryset()
        # Sort/page only IDs. Grouping or filesorting the full frozen JSON row
        # can exceed MySQL sort memory even for one large Execution snapshot.
        queryset = panel.executions.order_by('-created_at', '-id')
        total = queryset.count()
        offset = (page - 1) * size
        execution_ids = list(queryset.values_list('pk', flat=True)[offset:offset + size]) if offset < total else []
        execution_by_id = {
            row.pk: row for row in SimcBenchmarkExecution.objects.filter(
                pk__in=execution_ids,
            ).only(
                'id', 'panel_id', 'trigger', 'status', 'scheduled_slot', 'created_at', 'completed_at',
                'config_snapshot', 'config_hash', 'result_hash', 'results_finalized_at',
            ).order_by().prefetch_related(models.Prefetch(
                'cases', queryset=case_queryset, to_attr='_dashboard_cases',
            ))
        }
        rows = [execution_by_id[pk] for pk in execution_ids if pk in execution_by_id]
        return JsonResponse({'success': True, 'data': {
            'items': [_benchmark_execution_summary(
                row, published_id=panel.published_execution_id,
                case_count=len(row._dashboard_cases),
                cases=row._dashboard_cases,
            ) for row in rows],
            'pagination': {'page': page, 'size': size, 'total': total,
                           'has_next': offset + len(rows) < total},
        }})


class SimcBenchmarkExecutionDetailAPIView(_BenchmarkReadAPIView):
    def get(self, request, execution_id):
        execution = SimcBenchmarkExecution.objects.select_related('panel').filter(
            pk=execution_id,
        ).first()
        if execution is None:
            return _benchmark_error('not_found', 404)
        data = _benchmark_safe_detail(summarize_execution(execution), execution)
        # A retry's frozen snapshot is intentionally smaller than the Panel's full
        # current plan. Surface the independent reusable-result coverage so a
        # historical 65-case execution cannot look like it replaced a 96-case panel.
        try:
            data['panel_coverage'] = summarize_incremental_panel_coverage(execution.panel)
        except ValidationError:
            # Frozen Case detail remains readable if the current Panel configuration
            # is incomplete and cannot form a fresh logical surface.
            data['panel_coverage'] = {
                'aggregate_baseline_execution_id': execution.panel.aggregate_baseline_execution_id,
                'coordinates': 0, 'candidate_runs': 0,
                'available_results': 0, 'missing_results': 0,
                'source_executions': [],
            }
        if execution.panel.benchmark_type == SimcBenchmarkPanel.BENCHMARK_TYPE_OPTION_GAIN:
            aggregate = serialize_incremental_panel_results(execution.panel)
            data.update({
                'benchmark_type': execution.panel.benchmark_type,
                'comparison_option': execution.panel.comparison_option,
                'comparison_config': execution.panel.comparison_config or {},
                'comparison_label': aggregate.get('comparison_label', ''),
                'comparison_option_label': aggregate.get('comparison_option_label', ''),
                'option_gain_rows': aggregate.get('option_gain_rows', []),
                'option_gain_coordinates': aggregate.get('coordinates', []),
            })
        return JsonResponse({'success': True, 'data': data})


class SimcBenchmarkExecutionRerunFailedAPIView(_BenchmarkAdminAPIView):
    def post(self, request, execution_id):
        _benchmark_json_object(request, empty=True)
        execution = SimcBenchmarkExecution.objects.select_related('panel').filter(
            pk=execution_id,
        ).first()
        if execution is None:
            return _benchmark_error('not_found', 404)
        rerun = rerun_failed_cases(execution, requested_by=request.user)
        cases = list(_benchmark_progress_case_queryset().filter(execution=rerun))
        return JsonResponse({
            'success': True,
            'data': _benchmark_execution_summary(
                rerun, published_id=rerun.panel.published_execution_id,
                case_count=rerun.cases.count(), cases=cases,
            ),
        }, status=202)


class SimcBenchmarkCaseRerunAPIView(_BenchmarkAdminAPIView):
    def post(self, request, execution_id, case_id):
        _benchmark_json_object(request, empty=True)
        execution = SimcBenchmarkExecution.objects.select_related('panel').filter(
            pk=execution_id,
        ).first()
        if execution is None:
            return _benchmark_error('not_found', 404)
        rerun = rerun_case(execution, case_id, requested_by=request.user)
        cases = list(_benchmark_progress_case_queryset().filter(execution=rerun))
        return JsonResponse({
            'success': True,
            'data': _benchmark_execution_summary(
                rerun, published_id=rerun.panel.published_execution_id,
                case_count=rerun.cases.count(), cases=cases,
            ),
        }, status=202)


class SimcBenchmarkExecutionCancelAPIView(_BenchmarkAdminAPIView):
    def post(self, request, execution_id):
        _benchmark_json_object(request, empty=True)
        execution = SimcBenchmarkExecution.objects.select_related('panel').filter(
            pk=execution_id,
        ).first()
        if execution is None:
            return _benchmark_error('not_found', 404)
        cancelled = cancel_execution(execution, requested_by=request.user)
        cancelled = SimcBenchmarkExecution.objects.select_related('panel').get(pk=cancelled.pk)
        return JsonResponse({'success': True,
                             'data': _benchmark_safe_detail(
                                 summarize_execution(cancelled), cancelled)})


class SimcBenchmarkExecutionReconcileAPIView(_BenchmarkAdminAPIView):
    def post(self, request, execution_id):
        _benchmark_json_object(request, empty=True)
        execution = SimcBenchmarkExecution.objects.select_related('panel').filter(
            pk=execution_id,
        ).first()
        if execution is None:
            return _benchmark_error('not_found', 404)
        reconciled = reconcile_execution(execution)
        reconciled = SimcBenchmarkExecution.objects.select_related('panel').get(pk=reconciled.pk)
        return JsonResponse({'success': True,
                             'data': _benchmark_safe_detail(
                                 summarize_execution(reconciled), reconciled)})


