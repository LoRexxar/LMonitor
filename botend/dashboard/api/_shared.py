"""Dashboard API submodule: _shared."""
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




def _accessible_simc_profile_q(user):
    """Profiles usable in read/execute flows under the product-admin policy."""
    if _is_simc_admin(user):
        return models.Q()
    return (
        models.Q(user_id=user.id)
        | models.Q(
            user_id__isnull=True,
            source=SimcProfile.SOURCE_SIMC_UPSTREAM,
        )
    )


def _simc_spec_options():
    """统一返回所有 SimC 资源使用的 class_spec 专精标识。"""
    rows = []
    for class_name, specs in CLASS_SPEC_MAP.items():
        class_key = re.sub(r'(?<!^)(?=[A-Z])', '_', class_name).lower()
        class_key = {'death_knight': 'deathknight', 'demon_hunter': 'demonhunter'}.get(class_key, class_key)
        for spec_name in specs:
            spec_key = re.sub(r'(?<!^)(?=[A-Z])', '_', spec_name).lower()
            rows.append({
                'value': f'{class_key}_{spec_key}',
                'class_name': class_key,
                'class_label': CLASS_CN.get(class_name, class_name),
                'spec_label': SPEC_CN.get(spec_name, spec_name),
                'label': f'{SPEC_CN.get(spec_name, spec_name)} · {CLASS_CN.get(class_name, class_name)}',
            })
    return rows


SIMC_SPEC_OPTIONS = _simc_spec_options()


SIMC_SPEC_VALUES = frozenset(row['value'] for row in SIMC_SPEC_OPTIONS)


SIMC_SPEC_LABELS = {row['value']: row['spec_label'] for row in SIMC_SPEC_OPTIONS}


SIMC_SPEC_CLASS_NAMES = {row['value']: row['class_name'] for row in SIMC_SPEC_OPTIONS}


SIMC_SPEC_CLASS_LABELS = {row['value']: row['class_label'] for row in SIMC_SPEC_OPTIONS}


SIMC_CLASS_DB_NAMES = {
    re.sub(r'(?<!^)(?=[A-Z])', '_', class_name).lower()
    .replace('death_knight', 'deathknight')
    .replace('demon_hunter', 'demonhunter'): class_name
    for class_name in CLASS_SPEC_MAP
}


SIMC_SPEC_DB_IDENTITIES = {
    f'{class_key}_{re.sub(r"(?<!^)(?=[A-Z])", "_", spec_name).lower()}': (db_class_name, spec_name)
    for class_key, db_class_name in SIMC_CLASS_DB_NAMES.items()
    for spec_name in CLASS_SPEC_MAP[db_class_name]
}


def _canonical_simc_spec(value):
    normalized = str(value or '').strip().lower()
    return normalized if normalized in SIMC_SPEC_VALUES else None


def _simc_spec_label(spec, class_name=''):
    """Return a Chinese display label without changing the stable SimC key."""
    normalized = canonical_simc_profile_key(spec, class_name) or str(spec or '').strip().lower()
    if normalized in ('', 'default', 'all', '*'):
        return '通用' if normalized else '未标记'
    if normalized in SIMC_SPEC_LABELS:
        return SIMC_SPEC_LABELS[normalized]
    suffix_labels = {
        label
        for key, label in SIMC_SPEC_LABELS.items()
        if key.endswith(f'_{normalized}')
    }
    if len(suffix_labels) == 1:
        return suffix_labels.pop()
    return str(spec or '').strip() or '未标记'


def _simc_class_label(spec, class_name=''):
    """Return the canonical Chinese class label for a SimC resource identity."""
    normalized = canonical_simc_profile_key(spec, class_name) or str(spec or '').strip().lower()
    if normalized in ('', 'default', 'all', '*'):
        return '通用职业'
    if normalized in SIMC_SPEC_CLASS_LABELS:
        return SIMC_SPEC_CLASS_LABELS[normalized]
    canonical_class = str(class_name or '').strip().lower().replace('_', '')
    for key, db_name in SIMC_CLASS_DB_NAMES.items():
        if canonical_class in (key.replace('_', ''), db_name.lower()):
            return CLASS_CN.get(db_name, db_name)
    return str(class_name or '').strip() or '通用职业'


def _resolve_simc_talent_hero_names(spec, talent):
    """Resolve hero-tree titles through the authoritative talent parser."""
    return resolve_hero_talent_names(
        talent,
        spec,
        build_api_view=TalentBuildCodeService.build_api_view,
        hero_name_resolver=_hero_subtree_name_from_table,
    )


def _detect_simc_talent_spec(talent):
    """Detect the canonical spec by parsing the import string against the spec catalog."""
    for candidate in SIMC_SPEC_OPTIONS:
        spec = candidate['value']
        try:
            if _resolve_simc_talent_hero_names(spec, talent):
                return spec
        except Exception:
            continue
    return ''


class SimcTalentHeroTreeValidationError(ValueError):
    pass


def _validate_simc_talent_hero_tree(spec, talent):
    try:
        hero_names = _resolve_simc_talent_hero_names(spec, talent)
    except Exception as exc:
        logger.exception('保存独立天赋字符串时解析英雄天赋树失败: spec=%s', spec)
        raise SimcTalentHeroTreeValidationError('无法获取英雄天赋树，请检查专精和天赋字符串') from exc
    if not hero_names:
        raise SimcTalentHeroTreeValidationError('无法获取英雄天赋树，请检查专精和天赋字符串')
    return hero_names


def _simc_spec_visual(spec, class_name=''):
    """Resolve the authoritative specialization icon and Blizzard class color."""
    canonical = canonical_simc_profile_key(spec, class_name)
    identity = SIMC_SPEC_DB_IDENTITIES.get(canonical or '')
    if identity:
        db_class_name, db_spec_name = identity
    else:
        db_class_name = db_spec_name = None
    icon_url = SPEC_ICON.get((db_class_name, db_spec_name), '') if db_class_name and db_spec_name else ''
    return {
        'spec_icon_url': icon_url,
        'class_color': CLASS_COLOR.get(db_class_name, '#94A3B8') if db_class_name else '#94A3B8',
    }


def _simc_class_for_spec(spec):
    return SIMC_SPEC_CLASS_NAMES.get(spec)


def _is_simc_admin(user):
    return bool(user.is_staff or user.is_superuser)


def _fmt_dt(dt):
    if not dt:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_default_timezone())
    return timezone.localtime(dt).strftime('%Y-%m-%d %H:%M:%S')


def _static_root():
    base_dir = str(getattr(settings, "BASE_DIR", "") or "")
    if base_dir:
        return os.path.join(base_dir, "static")
    return os.path.join(os.getcwd(), "static")


def _safe_join_static(rel_path):
    raw = str(rel_path or '').replace('\\', '/')
    if not raw or raw.startswith('/'):
        return None
    try:
        root = Path(_static_root()).resolve(strict=True)
        full = (root / raw).resolve(strict=False)
        if os.path.commonpath((str(root), str(full))) != str(root):
            return None
        return str(full)
    except (OSError, RuntimeError, ValueError):
        return None


def _portal_report_url_from_path(content_html_path, fallback_url=''):
    rel_path = str(content_html_path or '').strip().lstrip('/')
    if rel_path.startswith('static/'):
        rel_path = rel_path[len('static/'):]
    if rel_path.startswith('portal/reports/'):
        rel_path = rel_path[len('portal/reports/'):]
    if rel_path:
        return f'/portal/reports/{rel_path}'

    url = str(fallback_url or '').strip()
    if url.startswith('/static/portal/reports/'):
        return '/portal/reports/' + url[len('/static/portal/reports/'):]
    return url


def _admission_busy_response(exc):
    data = {'success': False, 'error': str(exc), 'code': exc.code, 'executed': False}
    if exc.env_limit is not None:
        data['env_limit'] = exc.env_limit
    return JsonResponse(data, status=200 if exc.code == 'env_limit_blocked' else 409)


WOW_SIMC_CLASS_NAMES = {
    'deathknight', 'death_knight', 'demonhunter', 'demon_hunter', 'druid', 'evoker',
    'hunter', 'mage', 'monk', 'paladin', 'priest', 'rogue', 'shaman', 'warlock', 'warrior'
}


class SimcCatalogPermissionMixin(DashboardPermissionRequiredMixin):
    def get_dashboard_permissions(self, request):
        return ('simc.workflow', 'simc.advanced', 'simc.benchmarks', 'frontend.simulations')


