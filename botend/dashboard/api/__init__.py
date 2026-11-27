"""Dashboard API package (split by domain from monolithic api.py)."""
import sys
import types

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


from . import _shared as _mod__shared
from ._shared import *  # noqa: F401,F403
from ._shared import _accessible_simc_profile_q, _simc_spec_options, _canonical_simc_spec, _simc_spec_label, _simc_class_label, _resolve_simc_talent_hero_names, _detect_simc_talent_spec, _validate_simc_talent_hero_tree, _simc_spec_visual, _simc_class_for_spec, _is_simc_admin, _fmt_dt, _static_root, _safe_join_static, _portal_report_url_from_path, _admission_busy_response  # noqa: F401
from . import system as _mod_system
from .system import *  # noqa: F401,F403
from .system import _lookup_wow_daily_report  # noqa: F401
from . import simc_apl as _mod_simc_apl
from .simc_apl import *  # noqa: F401,F403
from .simc_apl import _APL_EDITOR_RATE_BUCKETS, _APL_EDITOR_RATE_LOCK, _APL_EDITOR_RATE_BUCKET_SOFT_LIMIT, _EditorSemaphore, _APL_EDITOR_SEMAPHORE, _editor_error, _editor_spec, _catalog_runtime_platform, _usable_catalog_backend_versions, _latest_catalog_identity, _authoritative_action_bindings, _readable_action_bindings, _authoritative_action_tokens, _catalog_item  # noqa: F401
from . import simc_benchmarks as _mod_simc_benchmarks
from .simc_benchmarks import *  # noqa: F401,F403
from .simc_benchmarks import _benchmark_iso, _benchmark_error, _benchmark_validation_fields, _BenchmarkAPIError, _benchmark_json_object, _BenchmarkReadAPIView, _BenchmarkAdminAPIView, _SimcOptionsAPIView, _benchmark_panel_summary, _benchmark_execution_list_summary, _benchmark_config_frozen, _benchmark_execution_metadata, _benchmark_progress_case_queryset, _benchmark_failure_rows, _benchmark_execution_progress, _benchmark_execution_summary, _benchmark_safe_string, _benchmark_safe_key, _benchmark_safe_error_log, _benchmark_spec_display_name, _benchmark_safe_count, _benchmark_safe_dps, _benchmark_safe_detail, _benchmark_spec_options, _benchmark_backend_game_versions, _benchmark_resource_spec_key, _benchmark_create_defaults, _benchmark_options_payload  # noqa: F401
from . import simc_resources as _mod_simc_resources
from .simc_resources import *  # noqa: F401,F403
from .simc_resources import _normalize_simc_token, _get_active_simc_content, _list_selectable_apl_for_spec, _resolve_home_creation_defaults, _get_simc_content_by_id, _get_unique_default_apl_for_spec  # noqa: F401
from . import simc_tasks as _mod_simc_tasks
from .simc_tasks import *  # noqa: F401,F403
from . import simc_analysis as _mod_simc_analysis
from .simc_analysis import *  # noqa: F401,F403
from . import simc_workbench as _mod_simc_workbench
from .simc_workbench import *  # noqa: F401,F403
from .simc_workbench import _SkillDamageSnapshotSpool  # noqa: F401
from . import wcl as _mod_wcl
from .wcl import *  # noqa: F401,F403

# Wire runtime-only cross-domain View references after all submodules are loaded
_mod_simc_tasks.SimcWorkbenchAPIView = _mod_simc_workbench.SimcWorkbenchAPIView
_mod_simc_analysis.SimcWorkbenchAPIView = _mod_simc_workbench.SimcWorkbenchAPIView

_SUBMODULES = (
    _mod__shared,
    _mod_system,
    _mod_simc_apl,
    _mod_simc_benchmarks,
    _mod_simc_resources,
    _mod_simc_tasks,
    _mod_simc_analysis,
    _mod_simc_workbench,
    _mod_wcl,
)


class _DashboardApiPackageModule(types.ModuleType):
    """Mirror attribute patches on botend.dashboard.api.* onto submodules for mock.patch compatibility."""

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        for mod in _SUBMODULES:
            if name in mod.__dict__:
                setattr(mod, name, value)

    def __delattr__(self, name):
        super().__delattr__(name)
        for mod in _SUBMODULES:
            if name in mod.__dict__:
                delattr(mod, name)


sys.modules[__name__].__class__ = _DashboardApiPackageModule
