"""Dashboard API submodule: simc_apl."""
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


from ._shared import _canonical_simc_spec, _simc_class_for_spec


_APL_EDITOR_RATE_BUCKETS = defaultdict(deque)


_APL_EDITOR_RATE_LOCK = threading.Lock()


_APL_EDITOR_RATE_BUCKET_SOFT_LIMIT = 1024


class _EditorSemaphore:
    def __init__(self, value):
        self._semaphore = threading.BoundedSemaphore(value)

    def acquire(self):
        return self._semaphore.acquire(blocking=False)

    def release(self):
        self._semaphore.release()


_APL_EDITOR_SEMAPHORE = _EditorSemaphore(4)


def _editor_error(code, message, status=400):
    return JsonResponse({'success': False, 'error': {'code': code, 'message': message}}, status=status)


def _editor_spec(raw):
    canonical = _canonical_simc_spec(raw)
    if not canonical:
        return None
    class_name = _simc_class_for_spec(canonical)
    return canonical, class_name, canonical[len(class_name) + 1:]


def _catalog_runtime_platform():
    system = str(py_platform.system() or '').lower()
    machine = str(py_platform.machine() or '').lower()
    if 'windows' in system:
        return 'win64'
    if 'linux' in system:
        return 'linuxarm64' if machine in ('aarch64', 'arm64') else 'linux64'
    return None


def _usable_catalog_backend_versions(queryset):
    versions = []
    for value in queryset.exclude(current_version='').values_list('current_version', flat=True):
        value = str(value or '').strip()
        if re.fullmatch(r'[0-9a-f]{40}', value) or re.search(r'(?:^|-)[0-9a-f]{7,39}$', value):
            versions.append(value)
    return sorted(set(versions))


def _latest_catalog_identity():
    configured = getattr(settings, 'SIMC_APL_CURRENT_IDENTITY', None)
    if configured and len(configured) == 2:
        revision, build = (str(value or '').strip() for value in configured)
        if re.fullmatch(r'[0-9a-f]{40}', revision) and build:
            return revision, build
        return None
    # A seeded/placeholder backend has an empty version and is not a catalog
    # identity.  There can be more than one backend per platform, so selecting
    # the first row would hide a valid running backend behind that placeholder.
    active_backends = SimcBackendBinary.objects.filter(is_active=True)
    runtime_platform = _catalog_runtime_platform()
    versions = _usable_catalog_backend_versions(
        active_backends.filter(platform=runtime_platform)
        if runtime_platform else active_backends.none()
    )
    # 开发机或迁移后的旧库可能尚无当前平台行；只在首选平台没有可用
    # revision 时回退到全部活动 Backend，且仍要求最终 revision 唯一。
    if not versions:
        versions = _usable_catalog_backend_versions(active_backends)
    # Several backend rows may report the same runtime revision.  This is one
    # catalog identity, not an ambiguity.  Distinct revisions do remain
    # fail-closed because choosing one could expose the wrong symbol catalog.
    if len(versions) != 1:
        return None
    current = versions[0]
    if not re.fullmatch(r'[0-9a-f]{40}', current):
        return None
    matching = active_backends.filter(current_version=current).exclude(game_build='')
    preferred = matching.filter(platform=runtime_platform) if runtime_platform else matching.none()
    builds = sorted(set((preferred if preferred.exists() else matching).values_list(
        'game_build', flat=True,
    )))
    return (current, builds[0]) if len(builds) == 1 else None


def _authoritative_action_bindings(parsed_spec, identity):
    """Resolve each visible action token to one authoritative SpellID.

    The visible token set remains global + current class + current spec.  A
    token that has no SpellID there may borrow a binding only from the exact
    same token in another spec of the same class.  Conflicting bindings are
    left unresolved rather than guessed.
    """
    if not parsed_spec or not identity:
        return {}
    _, class_name, spec_name = parsed_spec
    base = SimcAplSymbolScope.objects.filter(
        is_active=True, symbol__is_active=True,
        symbol__symbol_kind=SimcAplSymbol.KIND_ACTION, hero_tree__isnull=True,
    ).exclude(symbol__token='').exclude(symbol__token__in=CONTROL_ACTIONS)
    visible_scope = (
        models.Q(class_name__isnull=True, spec__isnull=True) |
        models.Q(class_name=class_name, spec__isnull=True) |
        models.Q(class_name=class_name, spec=spec_name)
    )
    visible_rows = list(base.filter(visible_scope).values_list(
        'symbol__token', 'spell_id', 'class_name', 'spec',
    ))
    visible_tokens = {token for token, _spell_id, _class, _spec in visible_rows}
    visible_spell_ids = {}
    candidates = {}
    for token, spell_id, row_class, row_spec in visible_rows:
        if not spell_id:
            continue
        visible_spell_ids.setdefault(token, set()).add(spell_id)
        rank = 2 if row_class == class_name and row_spec == spec_name else (
            1 if row_class == class_name and row_spec is None else 0
        )
        current_rank, current_ids = candidates.get(token, (-1, set()))
        if rank > current_rank:
            candidates[token] = (rank, {spell_id})
        elif rank == current_rank:
            current_ids.add(spell_id)

    bindings = {}
    for token in visible_tokens:
        # Different SpellIDs for one visible token are an authoritative
        # conflict even when they occur at different scope ranks.
        if len(visible_spell_ids.get(token, ())) > 1:
            continue
        rank, spell_ids = candidates.get(token, (-1, set()))
        if len(spell_ids) == 1:
            bindings[token] = (next(iter(spell_ids)), 'visible_scope')

    # A visible token with conflicting SpellIDs must remain unresolved; it is
    # not eligible for same-class fallback.
    conflicts = {token for token, spell_ids in visible_spell_ids.items() if len(spell_ids) > 1}
    missing = {token for token in visible_tokens if token not in bindings and token not in conflicts}
    fallback_rows = base.filter(
        class_name=class_name, symbol__token__in=missing, spell_id__isnull=False,
    ).values_list('symbol__token', 'spell_id')
    fallback_ids = {}
    for token, spell_id in fallback_rows:
        if token not in missing:
            continue
        fallback_ids.setdefault(token, set()).add(spell_id)
    for token, spell_ids in fallback_ids.items():
        if len(spell_ids) == 1:
            bindings[token] = (next(iter(spell_ids)), 'same_class_exact_token')
    return bindings


def _readable_action_bindings(parsed_spec):
    """跨版本解析动作中文所需 SpellID，同时保留冲突保护。"""
    if not parsed_spec:
        return {}, set()
    _, class_name, spec_name = parsed_spec
    base = SimcAplSymbolScope.objects.filter(
        is_active=True, symbol__is_active=True,
        symbol__symbol_kind=SimcAplSymbol.KIND_ACTION,
        hero_tree__isnull=True,
    ).exclude(symbol__token='').exclude(symbol__token__in=CONTROL_ACTIONS)
    visible_scope = (
        models.Q(class_name__isnull=True, spec__isnull=True) |
        models.Q(class_name=class_name, spec__isnull=True) |
        models.Q(class_name=class_name, spec=spec_name)
    )
    visible_rows = list(base.filter(visible_scope).values_list('symbol__token', 'spell_id'))
    visible_tokens = {token for token, _spell_id in visible_rows}
    visible_ids = defaultdict(set)
    for token, spell_id in visible_rows:
        if spell_id:
            visible_ids[token].add(spell_id)
    conflicts = {token for token, spell_ids in visible_ids.items() if len(spell_ids) > 1}
    bindings = {
        token: next(iter(spell_ids))
        for token, spell_ids in visible_ids.items() if len(spell_ids) == 1
    }
    missing = visible_tokens - set(bindings) - conflicts
    fallback_ids = defaultdict(set)
    for token, spell_id in base.filter(
            class_name=class_name, symbol__token__in=missing,
            spell_id__isnull=False).values_list('symbol__token', 'spell_id'):
        fallback_ids[token].add(spell_id)
    for token, spell_ids in fallback_ids.items():
        if len(spell_ids) == 1:
            bindings[token] = next(iter(spell_ids))
        elif len(spell_ids) > 1:
            conflicts.add(token)
    return bindings, conflicts


def _authoritative_action_tokens(spell_ids, parsed_spec, identity):
    """Return the visible authoritative tokens grouped by requested SpellID."""
    requested = set(spell_ids or ())
    selected = {}
    for token, (spell_id, _source) in _authoritative_action_bindings(
            parsed_spec, identity).items():
        if spell_id in requested:
            selected.setdefault(spell_id, set()).add(token)
    return {spell_id: tuple(sorted(tokens)) for spell_id, tokens in selected.items()}


def _catalog_item(item):
    scope = 'hero_tree' if item.hero_tree else ('spec' if item.spec else ('class' if item.class_name else 'global'))
    return {
        'token': item.token, 'kind': item.kind, 'scope': scope, 'spell_id': item.spell_id,
        'name_zh': item.name, 'name_en': item.name_en, 'description_zh': item.description,
        'icon': item.icon, 'insertable': item.insertable, 'reason': item.reason,
        'source': item.source,
        'availability': item.availability, 'actor': item.actor,
        'expression_template': item.expression_template,
    }


class SimcAplEditorAPIView(DashboardPermissionRequiredMixin, View):
    """Authenticated, bounded JSON protocol shared by editor endpoints."""
    dashboard_permission = 'simc.workflow'

    def dispatch(self, request, *args, **kwargs):
        if not getattr(request, 'user', None) or not request.user.is_authenticated:
            return _editor_error('authentication_required', 'Authentication required.', 401)
        return super().dispatch(request, *args, **kwargs)

    def http_method_not_allowed(self, request, *args, **kwargs):
        return _editor_error('method_not_allowed', 'HTTP method is not allowed.', 405)

    @staticmethod
    def payload(request):
        try:
            value = json.loads(request.body or b'{}')
        except (TypeError, ValueError, UnicodeDecodeError):
            raise SuspiciousOperation('invalid_json')
        if not isinstance(value, dict):
            raise SuspiciousOperation('invalid_json')
        return value

    @staticmethod
    def content(payload):
        value = payload.get('content', '')
        if not isinstance(value, str):
            return None, _editor_error('invalid_content', 'content must be a string.')
        maximum = int(getattr(settings, 'SIMC_APL_EDITOR_MAX_CONTENT_LENGTH', 200000))
        if len(value) > maximum:
            return None, _editor_error('content_too_large', 'Document exceeds the size limit.', 413)
        return value, None

    def rate_limit(self, request):
        limit = int(getattr(settings, 'SIMC_APL_EDITOR_RATE_LIMIT', 60))
        window = float(getattr(settings, 'SIMC_APL_EDITOR_RATE_WINDOW', 60))
        now = time.monotonic()
        key = (request.user.id, self.__class__.__name__)
        with _APL_EDITOR_RATE_LOCK:
            for stale_key, stale_bucket in list(_APL_EDITOR_RATE_BUCKETS.items()):
                while stale_bucket and stale_bucket[0] <= now - window:
                    stale_bucket.popleft()
                if not stale_bucket:
                    del _APL_EDITOR_RATE_BUCKETS[stale_key]
            if len(_APL_EDITOR_RATE_BUCKETS) >= _APL_EDITOR_RATE_BUCKET_SOFT_LIMIT:
                return _editor_error('rate_limited', 'Request rate limit exceeded.', 429)
            bucket = _APL_EDITOR_RATE_BUCKETS[key]
            if len(bucket) >= limit:
                return _editor_error('rate_limited', 'Request rate limit exceeded.', 429)
            bucket.append(now)
        return None


class SimcAplValidationAPIView(SimcAplEditorAPIView):
    def post(self, request):
        try:
            payload = self.payload(request)
        except SuspiciousOperation:
            return _editor_error('invalid_json', 'Request body must be a JSON object.')
        content, error = self.content(payload)
        if error:
            return error
        limited = self.rate_limit(request)
        if limited:
            return limited
        spec = _editor_spec(payload.get('spec'))
        if not spec:
            return _editor_error('invalid_spec', 'Unknown specialization.')
        profile_id = payload.get('profile_id')
        profile = None
        if profile_id is not None:
            profile = SimcProfile.objects.filter(id=profile_id, user_id=request.user.id, is_active=True).first()
            if not profile:
                return _editor_error('profile_not_found', 'Profile not found.', 404)
            profile_spec = _editor_spec(profile.spec)
            if not profile_spec or profile_spec[0] != spec[0]:
                return _editor_error('profile_spec_mismatch', 'Profile specialization does not match the validation context.')
        mode = payload.get('mode', 'structural')
        if mode not in ('structural', 'authoritative', 'both'):
            return _editor_error('invalid_mode', 'Unknown validation mode.')
        try:
            diagnostic_page = max(1, int(payload.get('diagnostic_page', 1)))
            page_size = max(1, min(100, int(payload.get('page_size', 50))))
        except (TypeError, ValueError):
            return _editor_error('invalid_pagination', 'diagnostic_page and page_size must be integers.')
        if not _APL_EDITOR_SEMAPHORE.acquire():
            return _editor_error('concurrency_limited', 'Validation capacity is busy.', 429)
        started = time.monotonic()
        try:
            validator = None
            validation_context = None
            if mode in ('authoritative', 'both'):
                if profile is None:
                    profiles = list(SimcProfile.objects.filter(
                        user_id=request.user.id, spec=spec[1], is_active=True)[:2])
                    if len(profiles) == 1:
                        profile = profiles[0]
                identity = _latest_catalog_identity()
                platform = 'linuxarm64' if 'aarch64' in py_platform.machine().lower() else 'linux64'
                backend = SimcBackendBinary.objects.filter(platform=platform).first()
                if profile is not None and identity and backend:
                    composer = SimcComposer(request.user.id)
                    try:
                        validation_input = composer.compose_validation_input(profile, content)
                    except (ValueError, TypeError, AttributeError):
                        validation_input = None
                    if validation_input is not None:
                        validation_context = SimcComposer.validation_context(
                            profile, catalog_revision=identity[0],
                            binary_revision=backend.current_version,
                            validation_input=validation_input)
                        validator = RestrictedSimcValidator(
                            backend.simc_path, catalog_revision=identity[0],
                            binary_revision=backend.current_version,
                            temp_root=getattr(settings, 'SIMC_APL_VALIDATION_TEMP_ROOT', None))
            data = validate_payload(content, mode=mode,
                                    authoritative_validator=validator,
                                    validation_context=validation_context)
        finally:
            _APL_EDITOR_SEMAPHORE.release()
        total = len(data['diagnostics'])
        start = (diagnostic_page - 1) * page_size
        data['diagnostics'] = data['diagnostics'][start:start + page_size]
        data['pagination'] = {'page': diagnostic_page, 'page_size': page_size, 'total': total,
                              'total_pages': (total + page_size - 1) // page_size}
        data['document_version'] = payload.get('document_version')
        data['context'] = {'spec': spec[0], 'profile_id': profile.id if profile else None}
        data['elapsed_ms'] = int((time.monotonic() - started) * 1000)
        return JsonResponse({'success': True, 'data': data})


class SimcAplSymbolsAPIView(SimcAplEditorAPIView):
    def get(self, request):
        limited = self.rate_limit(request)
        if limited:
            return limited
        spec = _editor_spec(request.GET.get('spec'))
        if not spec:
            return _editor_error('invalid_spec', 'Unknown specialization.')
        try:
            page = int(request.GET.get('page', 1))
            page_size = int(request.GET.get('page_size', 50))
        except (TypeError, ValueError):
            return _editor_error('invalid_pagination', 'Invalid pagination parameters.')
        if page < 1 or page_size < 1:
            return _editor_error('invalid_pagination', 'Invalid pagination parameters.')
        page_size = min(100, page_size)
        kind = (request.GET.get('kind') or '').strip()
        kinds = {
            value.strip() for value in (request.GET.get('kinds') or '').split(',')
            if value.strip()
        }
        if kind:
            kinds = {kind}
        valid_kinds = {value for value, _label in SimcAplSymbol.SYMBOL_KIND_CHOICES}
        if not kinds.issubset(valid_kinds):
            return _editor_error('invalid_kind', 'Unknown symbol kind.')
        query = (request.GET.get('query') or '').strip()[:200]
        branch = request.GET.get('branch') or 'retail'
        if branch not in ('retail', 'ptr'):
            return _editor_error('invalid_branch', '仅支持正式服或测试服。')
        if not _APL_EDITOR_SEMAPHORE.acquire():
            return _editor_error('concurrency_limited', 'Symbol query capacity is busy.', 429)
        try:
            # 编辑器目录来自无版本字段词典；版本不会进入字段或归属查询。
            items = query_symbol_catalog(
                None, None, spec[1], spec[2], search=query or None, branch=branch,
            )
        finally:
            _APL_EDITOR_SEMAPHORE.release()
        if kinds:
            items = [item for item in items if item.kind in kinds]
        total = len(items)
        total_pages = (total + page_size - 1) // page_size
        start = (page - 1) * page_size
        return JsonResponse({'success': True, 'data': {
            'items': [_catalog_item(item) for item in items[start:start + page_size]],
            'pagination': {'page': page, 'page_size': page_size, 'total': total, 'total_pages': total_pages},
        }})


class SimcAplSpellsAPIView(SimcAplEditorAPIView):
    """Bilingual actions for the current authoritative spec catalog."""

    def get(self, request):
        spec = _editor_spec(request.GET.get('spec'))
        if not spec:
            return _editor_error('invalid_spec', 'Unknown specialization.')
        try:
            page = int(request.GET.get('page', 1))
            page_size = int(request.GET.get('page_size', 50))
        except (TypeError, ValueError):
            return _editor_error('invalid_pagination', 'Invalid pagination parameters.')
        if page < 1 or page_size < 1:
            return _editor_error('invalid_pagination', 'Invalid pagination parameters.')
        page_size = min(100, page_size)
        identity = _latest_catalog_identity()
        if not identity:
            return _editor_error('catalog_unavailable', 'The current symbol catalog is unavailable.', 503)

        revision, build = identity
        bindings = _authoritative_action_bindings(spec, identity)
        spell_ids = {spell_id for spell_id, _source in bindings.values()}
        localized = {
            row['spell_id']: row
            for row in WowSpellSnapshot.objects.filter(
                branch='wow', locale='zhCN', snapshot_build=build,
                spell_id__in=spell_ids,
            ).exclude(name='').exclude(name_zh='').values(
                'spell_id', 'name', 'name_zh',
            ).order_by('spell_id', '-updated_at', '-id')
        }
        items = []
        for token, (spell_id, binding_source) in bindings.items():
            names = localized.get(spell_id)
            if not names:
                continue
            items.append({
                'english': names['name'], 'chinese': names['name_zh'],
                'token': token, 'spell_id': spell_id,
                'token_source': 'simc_symbol',
                'binding_source': binding_source, 'authoritative': True,
            })

        query = (request.GET.get('query') or '').strip()[:200].casefold()
        if query:
            items = [item for item in items if query in item['token'].casefold()
                     or query in item['english'].casefold()
                     or query in item['chinese'].casefold()]
        items.sort(key=lambda item: (item['english'].casefold(), item['token']))
        total = len(items)
        if request.GET.get('all') == '1':
            page = 1
            page_size = max(total, 1)
            total_pages = 1
            selected_items = items
        else:
            total_pages = (total + page_size - 1) // page_size
            start = (page - 1) * page_size
            selected_items = items[start:start + page_size]
        return JsonResponse({'success': True, 'data': {
            'items': selected_items,
            'pagination': {'page': page, 'page_size': page_size, 'total': total, 'total_pages': total_pages},
        }})


class SimcAplCompletionsAPIView(SimcAplEditorAPIView):
    def post(self, request):
        try:
            payload = self.payload(request)
        except SuspiciousOperation:
            return _editor_error('invalid_json', 'Request body must be a JSON object.')
        content, error = self.content(payload)
        if error:
            return error
        limited = self.rate_limit(request)
        if limited:
            return limited
        spec = _editor_spec(payload.get('spec'))
        position = payload.get('position')
        if not spec:
            return _editor_error('invalid_spec', 'Unknown specialization.')
        if not isinstance(position, dict):
            return _editor_error('invalid_position', 'position must be an object.')
        try:
            line, column = int(position['line']), int(position['column'])
        except (KeyError, TypeError, ValueError):
            return _editor_error('invalid_position', 'position requires integer line and column.')
        lines = content.splitlines() or ['']
        if line < 1 or line > len(lines) or column < 1 or column > len(lines[line - 1]) + 1:
            return _editor_error('invalid_position', 'position is outside the document.')
        if not _APL_EDITOR_SEMAPHORE.acquire():
            return _editor_error('concurrency_limited', 'Completion capacity is busy.', 429)
        try:
            items = complete_document(content, line, column)[:100]
        finally:
            _APL_EDITOR_SEMAPHORE.release()
        return JsonResponse({'success': True, 'data': {
            'document_version': payload.get('document_version'),
            'items': items,
        }})


@method_decorator([csrf_exempt, login_required], name='dispatch')
class ConvertTextAPIView(View):
    """
    SimC APL文本转换API
    """
    
    def post(self, request):
        try:
            # 解析请求数据
            data = json.loads(request.body)
            text = data.get('text', '')
            conversion_type = data.get('conversion_type', '')
            spec = data.get('spec', '')

            if not isinstance(text, str) or not text.strip():
                return JsonResponse({
                    'success': False,
                    'error': '输入文本不能为空'
                })
            maximum = int(getattr(settings, 'SIMC_APL_EDITOR_MAX_CONTENT_LENGTH', 200000))
            if len(text) > maximum:
                return JsonResponse({
                    'success': False,
                    'error': f'输入文本不能超过 {maximum} 个字符'
                }, status=413)
            
            if conversion_type not in ['apl_to_cn', 'cn_to_apl']:
                return JsonResponse({
                    'success': False,
                    'error': '无效的转换类型'
                })
            
            # 执行转换
            if conversion_type == 'apl_to_cn':
                result = self.convert_apl_to_cn(text, spec)
            else:
                result = self.convert_cn_to_apl(text, spec)
            
            return JsonResponse({
                'success': True,
                'result': result
            })
            
        except Exception as e:
            logger.error(f"一键模拟SimC配置失败: {str(e)}")
            return JsonResponse({
                'success': False,
                'error': '一键模拟SimC配置失败'
            })
        except Exception as e:
            logger.error(f"文本转换API错误: {str(e)}\n{traceback.format_exc()}")
            return JsonResponse({
                'success': False,
                'error': f'获取APL详情失败: {str(e)}'
            })
    
    def bilingual_pairs(self, spec='', text=''):
        """Build a typed catalog, then scope visible names to the submitted document."""
        parsed_spec = _editor_spec(spec) if spec else None
        if not parsed_spec:
            return [], []
        class_key, class_name, spec_name = parsed_spec
        demands = list(extract_translation_demands(text))
        # The legacy editor accepts action-list lines that the strict parser
        # rejects (notably a standalone ``actions+=/...``).  Extract only the
        # action slot on those lines; do not broaden into a catalog scan.
        parsed_action_tokens = {demand.token.casefold() for demand in demands if demand.kind == 'action'}
        action_slot = re.compile(
            r'(?m)^\s*actions(?:\.[A-Za-z0-9_]+)?\s*\+?=\s*/\s*'
            r'([A-Za-z][A-Za-z0-9_]*(?:[ _][A-Za-z0-9_]+)*)'
        )
        for match in action_slot.finditer(text):
            source_token = match.group(1)
            token_key = source_token.replace(' ', '_').casefold()
            if token_key not in parsed_action_tokens:
                demands.append(TranslationDemand('action', token_key, token_key in CONTROL_ACTIONS))
                parsed_action_tokens.add(token_key)
        visible_symbols = query_visible_symbols(class_name, spec_name)
        catalog_facts = [
            {
                'symbol_kind': row.symbol_kind, 'token': row.token,
                'spell_id': row.spell_id, 'trait_id': row.trait_id,
                'hero_tree': row.hero_tree, 'class_name': row.class_name,
                'spec': row.spec, 'name_zh': row.name_zh,
            }
            for row in visible_symbols if not row.hero_tree
        ]
        class_scope_key = class_name.casefold()
        spec_scope_key = spec_name.casefold()

        def visible_scope_rank(row):
            row_class = str(row.get('class_name') or '').casefold()
            row_spec = str(row.get('spec') or '').casefold()
            if row_class == class_scope_key and row_spec == spec_scope_key:
                return 2
            if row_class == class_scope_key and not row_spec:
                return 1
            if not row_class and not row_spec:
                return 0
            return -1

        action_bindings, action_conflicts = _readable_action_bindings(parsed_spec)
        facts = [
            row for row in catalog_facts if row['symbol_kind'] != 'action'
        ] + [
            {
                'symbol_kind': 'action', 'token': token,
                'spell_id': spell_id, 'trait_id': None, 'hero_tree': None,
            }
            for token, spell_id in action_bindings.items()
        ]
        spell_ids = {row['spell_id'] for row in facts if row['spell_id']}
        trait_ids = {row['trait_id'] for row in facts if row['trait_id']}
        localized = {}
        identity = _latest_catalog_identity()
        snapshot_rows = WowSpellSnapshot.objects.filter(
            branch='wow', locale='zhCN', spell_id__in=spell_ids,
        ).exclude(name_zh='')
        ordered_snapshots = []
        if identity:
            ordered_snapshots.extend(snapshot_rows.filter(
                snapshot_build=identity[1],
            ).values('spell_id', 'name_zh').order_by('spell_id', '-updated_at', '-id'))
            snapshot_rows = snapshot_rows.exclude(snapshot_build=identity[1])
        ordered_snapshots.extend(snapshot_rows.values(
            'spell_id', 'name_zh',
        ).order_by('spell_id', '-updated_at', '-id'))
        for row in ordered_snapshots:
            localized.setdefault(('spell', row['spell_id']), row['name_zh'])
        # 快照是首选来源；快照缺失时才使用随 APL 数据包固化的 Wowhead 中文。
        packaged_names = defaultdict(set)
        for row in catalog_facts:
            if row.get('spell_id') and str(row.get('name_zh') or '').strip():
                packaged_names[row['spell_id']].add(row['name_zh'])
        for spell_id, names in packaged_names.items():
            if len(names) == 1:
                localized.setdefault(('spell', spell_id), next(iter(names)))
        for row in WowTalentNodeMetadata.objects.filter(
            talent_version__branch='retail', talent_version__is_active=True,
            name_zh__gt='', talent_id__in=trait_ids,
        ).values('talent_id', 'name_zh').order_by('talent_id', '-last_updated', '-id'):
            localized.setdefault(('trait', row['talent_id']), row['name_zh'])
        mapping, _failures = resolve_demand_mappings(demands, facts, localized)
        scoped_facts = {}
        for fact in facts:
            kind = str(fact.get('symbol_kind') or '')
            token = str(fact.get('token') or '')
            if not token or kind not in {'action', 'buff', 'debuff', 'dot', 'cooldown', 'talent'}:
                continue
            scoped_facts.setdefault((kind, token.casefold()), []).append(fact)
        for (kind, token_key), token_facts in scoped_facts.items():
            if len(token_facts) != 1:
                continue
            fact = token_facts[0]
            identity_type = 'trait' if kind == 'talent' else 'spell'
            identity_id = fact.get('trait_id') if identity_type == 'trait' else fact.get('spell_id')
            chinese = localized.get((identity_type, identity_id))
            if isinstance(identity_id, int) and not isinstance(identity_id, bool) and identity_id > 0 and chinese:
                mapping.setdefault((kind, token_key), chinese)
        # A token that resolves to multiple localized names is not reversible.
        token_names = {}
        for key, chinese in mapping.items():
            token_names.setdefault(key, set()).add(chinese.casefold())
        mapping = {key: value for key, value in mapping.items() if len(token_names[key]) == 1}
        mapping = {
            key: value for key, value in mapping.items()
            if key[0] != 'action' or key[1] not in CONTROL_ACTIONS
        }
        # Rows without a spell/trait ID still carry useful human-readable
        # names from the built-in localization package.  Add them only after
        # authoritative ID mappings, and only when the most specific visible
        # scope has one unambiguous Chinese label.  This also intentionally
        # permits catalog-defined control action names without treating those
        # DSL controls as game spells.
        scoped_names = defaultdict(list)
        for row in catalog_facts:
            kind = str(row.get('symbol_kind') or '')
            token = str(row.get('token') or '').strip()
            chinese = str(row.get('name_zh') or '').strip()
            if not token or not chinese or kind not in {
                'action', 'buff', 'debuff', 'dot', 'cooldown', 'talent',
            }:
                continue
            if kind == 'action' and token in action_conflicts:
                continue
            scope_rank = visible_scope_rank(row)
            if scope_rank < 0:
                continue
            scoped_names[(kind, token.casefold())].append((scope_rank, chinese))
        for key, candidates in scoped_names.items():
            highest_rank = max(rank for rank, _chinese in candidates)
            highest_names = {
                chinese.casefold(): chinese
                for rank, chinese in candidates if rank == highest_rank
            }
            if len(highest_names) == 1:
                mapping.setdefault(key, next(iter(highest_names.values())))
        active_keys = {(demand.kind, demand.token.casefold()) for demand in demands}
        # Scope disambiguation to the submitted document: unrelated runtime
        # aliases stay plain and cannot make a normal single-token edit verbose.
        pairs = disambiguate_chinese_labels(
            [(kind, token, chinese) for (kind, token), chinese in mapping.items()],
            active_keys=active_keys if text else None,
            include_plain_alias=not bool(text),
        )
        forward_pairs = list(pairs)
        reverse_pairs = list(pairs)
        return forward_pairs, reverse_pairs


    def convert_apl_to_cn(self, text, spec=''):
        """
        将APL关键字转换为中文
        """
        try:
            keyword_pairs = sorted(
                self.bilingual_pairs(spec, text=text)[0], key=lambda pair: len(pair[1]), reverse=True,
            )
            pair_by_token = {
                (kind, apl_keyword.casefold()): cn_keyword
                for kind, apl_keyword, cn_keyword in keyword_pairs
            }
            document_demands = list(extract_translation_demands(text))
            parsed_action_tokens = {demand.token.casefold() for demand in document_demands if demand.kind == 'action'}
            for token in re.findall(r'(?:^|\n)\s*actions(?:\.[A-Za-z0-9_]+)?\s*[+]?=/\s*([A-Za-z][A-Za-z0-9_]*)', text):
                if token.casefold() not in parsed_action_tokens:
                    document_demands.append(TranslationDemand('action', token, token.casefold() in CONTROL_ACTIONS))
                    parsed_action_tokens.add(token.casefold())
            mapping = {
                (demand.kind, demand.token.casefold()): pair_by_token[(demand.kind, demand.token.casefold())]
                for demand in document_demands
                if (demand.kind, demand.token.casefold()) in pair_by_token
            }
            translated = translate_apl_ranges(text, mapping)
            # Legacy editor input also accepts spaces in an action token. Such
            # input is outside canonical SimC token grammar, so apply a narrow
            # fallback only at the parser-defined action slot, never to
            # comments, expressions, option values, or arbitrary text.
            for kind, apl_keyword, cn_keyword in keyword_pairs:
                if kind != 'action':
                    continue
                token_pattern = r'[ _]+'.join(re.escape(part) for part in apl_keyword.split('_'))
                pattern = (
                    r'(?m)^(?P<prefix>\s*actions(?:\.[A-Za-z0-9_]+)?\+?=\/?)'
                    + token_pattern + r'(?=[,\s#]|$)'
                )
                translated = re.sub(
                    pattern,
                    lambda match, value=cn_keyword: match.group('prefix') + value,
                    translated,
                )
            return translated
        except Exception as e:
            logger.error(f"APL2CN错误: {str(e)}")
            raise e
    
    def convert_cn_to_apl(self, text, spec=''):
        """
        将中文关键字转换为APL
        """
        try:
            keyword_pairs = sorted(
                self.bilingual_pairs(spec)[1], key=lambda pair: len(pair[2]), reverse=True,
            )
            lines = text.splitlines(keepends=True)
            # Reverse only known SimC slots. Chinese input cannot be parsed by
            # the English parser, so do not run a document-wide substitution:
            # action names occur after ``actions...=/`` and expression names
            # occur after one of the typed prefixes.
            for kind, apl_keyword, cn_keyword in keyword_pairs:
                value_pattern = r'\s*'.join(
                    re.escape(char) for char in cn_keyword if not char.isspace()
                )
                action_pattern = (
                    r'(?m)^(?P<prefix>\s*actions(?:\.[A-Za-z0-9_]+)?\+?=\/?)'
                    + value_pattern + r'(?=[,\s#]|$)'
                ) if kind == 'action' else r'(?!x)x'
                expression_pattern = (
                    rf'(?P<prefix>\b{re.escape(kind)}\.)'
                    + value_pattern + r'(?=\.|[\s<>=!,)&|+*/%@^~?-]|$)'
                ) if kind in {'buff', 'debuff', 'dot', 'cooldown', 'talent'} else r'(?!x)x'
                lines = [
                    re.sub(
                        expression_pattern,
                        lambda match, value=apl_keyword: match.group('prefix') + value,
                        re.sub(
                            action_pattern,
                            lambda match, value=apl_keyword: match.group('prefix') + value,
                            line,
                        ),
                    ) if not line.lstrip().startswith('#') else line
                    for line in lines
                ]
            return ''.join(lines)
        except Exception as e:
            logger.error(f"CN2APL错误: {str(e)}")
            raise e


