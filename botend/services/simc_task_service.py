"""
SimC Task Service - Create reference-based tasks with immutable version snapshots.

Responsibilities:
1. Validate executable resource state and specialization compatibility
2. Generate or reuse SimcResourceVersion for each resource
3. Normalize simulation_params & mode_params with whitelist
4. Create Task with live FK + version FK, no frozen content
"""
import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from django.db import transaction
from django.db.models import Q
from botend.models import (
    SimcTask,
    SimulationRun,
    SimcProfile,
    SimcContentTemplate,
    SimcApl,
    SimcResourceVersion,
    SimcBackendBinary,
)
from botend.services.simc_apl.publish import (
    content_hash as apl_content_hash,
    current_validation_identity,
    validate_apl_for_profile,
)
from botend.services.simc_composer import validate_simulation_options
from botend.services.simc_player_config import normalize_gear_candidate_value


class TaskCreationError(Exception):
    """Raised when task creation fails validation."""

    def __init__(self, message, *, details=None):
        super().__init__(message)
        self.details = deepcopy(details)


class TaskValidationUnavailable(TaskCreationError):
    """Authoritative validation could not currently produce a reliable verdict."""


class TaskPreparedResourceChanged(TaskCreationError):
    """A valid preflight token became stale because a locked resource changed."""


_PREPARED_SEAL = object()

_VALIDATION_TOP_LEVEL_RETRYABLE = frozenset({
    'validation_context_unavailable',
    'validation_backend_unavailable',
    'validation_failed',
})
_AUTHORITATIVE_CONTENT_FAILURES = frozenset({
    'source_too_large',
    'validation_input_too_large',
    'profile_directive_forbidden',
})
_AUTHORITATIVE_RETRYABLE_FAILURES = frozenset({
    'stale_binary',
    'binary_unavailable',
    'temp_directory_error',
    'timeout',
    'output_too_large',
})


def _validation_failure_is_retryable(validation):
    """Classify a failed validator result using fields, never diagnostic text.

    Definite content/input rejection is permanent. Unknown or malformed failed
    results are retryable so a scheduled slot is not lost merely because a newer or
    broken validator response could not be classified.
    """
    if not isinstance(validation, dict):
        return True

    if validation.get('error') in _VALIDATION_TOP_LEVEL_RETRYABLE:
        return True

    details = validation.get('details')
    if not isinstance(details, dict):
        return True

    # Structural parsing is local and deterministic, so it is authoritative even if
    # the remainder of a malformed response contains contradictory fields.
    if details.get('structural_valid') is False:
        return False

    status = details.get('authoritative_status')
    authoritative_error = details.get('authoritative_error')
    code = authoritative_error.get('code') if isinstance(authoritative_error, dict) else None
    if code in _AUTHORITATIVE_CONTENT_FAILURES:
        return False
    if code in _AUTHORITATIVE_RETRYABLE_FAILURES:
        return True
    if status == 'invalid':
        return False
    if status == 'error':
        # Unknown authoritative errors deliberately fail safe as retryable.
        return True

    # A structural skip is a definite content failure even if structural_valid was
    # omitted by an older validator response.
    if status == 'skipped_structural_errors':
        return False

    return True


@dataclass(frozen=True)
class PreparedTaskCreation:
    """Opaque, process-local proof that executable resources passed preflight.

    Persistence re-locks and re-derives every execution-affecting token. Canonical
    JSON strings keep the prepared payload deeply immutable.
    """
    user_id: int
    backend_id: int
    profile_id: int
    apl_id: int
    template_id: int
    profile_payload_json: str
    apl_payload_json: str
    template_payload_json: str
    resource_token: str
    validation_identity: tuple
    is_admin: bool
    seal: object = field(repr=False, compare=False)


# Whitelist for simulation_params
SIMULATION_PARAMS_WHITELIST = {
    'iterations',
    'target_error',
    'fight_style',
    'max_time',
    'vary_combat_length',
    'enemy_type',
    'desired_targets',
    'raid_buffs',
    'use_class_raid_buff',
    'extra_options',
    'additional_simc_input',
    'profile_overrides',
}

# Candidate differences for comparison / attribute-sweep tasks.  Values remain
# structured JSON, but unknown top-level keys are discarded at task creation.
MODE_PARAMS_WHITELIST = {
    'search',
    'request_manifest',
}

CANDIDATE_PARAMS_WHITELIST = {
    'candidate_type', 'is_base', 'gear_swap', 'talent_override',
    'talent_candidate', 'apl_override', 'attribute_ratings', 'search',
    'simc_options', 'equipment_preset', 'option_value', 'enabled',
    'simulation_params',
}


def _compute_content_hash(payload: dict) -> str:
    """Compute SHA256 hash of payload for version deduplication."""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def validate_resource_ownership(
    resource,
    resource_type: str,
    user_id: int,
    is_admin: bool = False,
) -> None:
    """
    Validate resource ownership and active/selectable status.

    Rules:
    - Profile: user's own or an active upstream system default Profile
    - Template: active and selectable
    - APL: personal APLs only need to be active; ownerless system APLs must be selectable
    """
    if resource_type == 'profile':
        if not isinstance(resource, SimcProfile):
            raise TaskCreationError(f"Invalid profile resource")
        is_system_default = (
            resource.user_id is None
            and resource.source == SimcProfile.SOURCE_SIMC_UPSTREAM
        )
        if not is_system_default and resource.user_id != user_id and not is_admin:
            raise TaskCreationError(
                f"Profile {resource.id} belongs to user {resource.user_id}, not {user_id}"
            )
        if not resource.is_active:
            raise TaskCreationError(f"Profile {resource.id} is not active")

    elif resource_type == 'template':
        if not isinstance(resource, SimcContentTemplate):
            raise TaskCreationError(f"Invalid template resource")
        if (resource.owner_user_id is not None and resource.owner_user_id != user_id
                and not is_admin):
            raise TaskCreationError(
                f"Template {resource.id} belongs to user {resource.owner_user_id}, not {user_id}"
            )
        if not resource.is_active:
            raise TaskCreationError(f"Template {resource.id} is not active")
        if not resource.is_selectable:
            raise TaskCreationError(f"Template {resource.id} is not selectable")

    elif resource_type == 'apl':
        if not isinstance(resource, SimcApl):
            raise TaskCreationError(f"Invalid APL resource")
        # An ownerless system APL is globally selectable. A user-owned row must
        # never become globally selectable merely by carrying is_system=True.
        is_system_default = resource.is_system and resource.owner_user_id is None
        if not is_system_default and resource.owner_user_id != user_id and not is_admin:
            raise TaskCreationError(
                f"APL {resource.id} belongs to user {resource.owner_user_id}, not {user_id}"
            )
        if not resource.is_active:
            raise TaskCreationError(f"APL {resource.id} is not active")
        if resource.is_system and resource.owner_user_id is None and not resource.is_selectable:
            raise TaskCreationError(f"APL {resource.id} is not selectable")


# Compatibility for existing imports/call sites. Keep forwarding dynamically so
# monkeypatches and future fixes to the public policy are consistently observed.
def _validate_resource_ownership(resource, resource_type: str, user_id: int,
                                 is_admin: bool = False) -> None:
    return validate_resource_ownership(
        resource, resource_type, user_id, is_admin=is_admin,
    )


def _create_or_reuse_version(
    resource_type: str,
    resource_id: int,
    payload: dict,
) -> SimcResourceVersion:
    """
    Create or reuse SimcResourceVersion based on content_hash.

    Returns existing version if (resource_type, resource_id, content_hash) matches,
    otherwise creates new version. Handles race conditions with get_or_create.
    """
    from django.db import IntegrityError

    content_hash = _compute_content_hash(payload)

    try:
        version, created = SimcResourceVersion.objects.get_or_create(
            resource_type=resource_type,
            resource_id=resource_id,
            content_hash=content_hash,
            defaults={'payload': payload},
        )
        return version
    except IntegrityError:
        # Race condition: another transaction created it between our check and insert
        # Re-read from DB
        version = SimcResourceVersion.objects.get(
            resource_type=resource_type,
            resource_id=resource_id,
            content_hash=content_hash,
        )
        return version


def _build_profile_payload(profile: SimcProfile) -> dict:
    """Build immutable executable payload from SimcProfile.

    A saved manual Profile may retain exporter candidate sections for workbench
    selection. Task versions freeze only the current equipped player block;
    selected candidate differences remain in ``mode_params``.
    """
    player_equipment = profile.player_equipment
    if profile.player_config_mode == 'manual_equipment' and player_equipment:
        from botend.services.simc_player_config import parse_simc_player_profile
        player_equipment = parse_simc_player_profile(player_equipment)['profile']['raw_player_block']
    return {
        'name': profile.name,
        'spec': profile.spec,
        'use_ptr': bool(getattr(profile, 'use_ptr', False)),
        'player_config_mode': profile.player_config_mode,
        'battlenet_region': profile.battlenet_region,
        'battlenet_realm': profile.battlenet_realm,
        'battlenet_character': profile.battlenet_character,
        'player_equipment': player_equipment,
        'talent': profile.talent,
        # Explicit Profile overrides are frozen independently of player source.
        'gear_strength': profile.gear_strength,
        'gear_crit': profile.gear_crit,
        'gear_haste': profile.gear_haste,
        'gear_mastery': profile.gear_mastery,
        'gear_versatility': profile.gear_versatility,
    }


def _build_template_payload(template: SimcContentTemplate) -> dict:
    """Build immutable payload from SimcContentTemplate."""
    return {
        'name': template.name,
        'spec': template.spec,
        'content': template.content,
    }


def _build_apl_payload(apl: SimcApl) -> dict:
    """Build immutable payload from SimcApl."""
    return {
        'name': apl.name,
        'spec': apl.spec,
        'content': apl.content,
        'is_system': apl.is_system,
    }


def _normalize_params(params: Optional[Dict[str, Any]], whitelist: set) -> Optional[Dict[str, Any]]:
    """Normalize params dict by filtering with whitelist."""
    if not params:
        return None
    return {k: v for k, v in params.items() if k in whitelist}


def _normalize_candidates(candidates, round_number=1):
    """Freeze only the controlled candidate fields needed by backend execution."""
    candidates = list(candidates or [])
    if not candidates:
        candidates = [{'candidate_key': 'normal', 'candidate_label': 'normal'}]
    frozen = []
    for index, candidate in enumerate(candidates, 1):
        if not isinstance(candidate, dict):
            raise TaskCreationError('candidate must be an object')
        params = _normalize_params(
            candidate.get('candidate_params') or candidate.get('params') or {},
            CANDIDATE_PARAMS_WHITELIST,
        ) or {}
        if 'simc_options' in params:
            from botend.services.simc_candidate_options import normalize_controlled_simc_options
            try:
                params['simc_options'] = normalize_controlled_simc_options(
                    params['simc_options'], allow_absent=False,
                )
            except ValueError as exc:
                raise TaskCreationError(str(exc)) from exc
        if 'equipment_preset' in params:
            preset = params['equipment_preset']
            if not isinstance(preset, dict) or set(preset) != {'trinket1', 'trinket2'}:
                raise TaskCreationError('benchmark equipment_preset 必须精确包含两个饰品槽')
            if preset['trinket1'] != '' or not isinstance(preset['trinket2'], str):
                raise TaskCreationError('benchmark equipment_preset 必须清空 trinket1 并固定 trinket2')
            try:
                normalize_gear_candidate_value('trinket2', preset['trinket2'])
            except ValueError as exc:
                raise TaskCreationError(str(exc)) from exc
            params['equipment_preset'] = deepcopy(preset)
        key = str(candidate.get('candidate_key') or candidate.get('key') or f'candidate-{index}')[:200]
        frozen.append({
            'candidate_key': key,
            'candidate_label': str(candidate.get('candidate_label') or candidate.get('label') or key)[:200],
            'round_number': max(1, int(candidate.get('round_number') or round_number)),
            'candidate_params': params,
            'display_metadata': {
                'icon_url': str(candidate.get('icon_url') or '')[:500],
            },
        })
    return frozen


def initialize_task_runs(task, expected_started_at=None):
    """Create initial Runs when backend processing actually starts a Task."""
    with transaction.atomic():
        locked = SimcTask.objects.select_for_update().get(pk=task.pk)
        if expected_started_at is not None and (
            locked.current_status != 1 or locked.started_at != expected_started_at
        ):
            raise ValueError('SimC Task 执行租约已失效')
        existing = list(SimulationRun.objects.filter(task=locked).order_by('sequence'))
        if existing:
            return existing
        mode_params = locked.mode_params if isinstance(locked.mode_params, dict) else {}
        candidates = _normalize_candidates(mode_params.get('initial_candidates'))
        rows = [SimulationRun(
            task=locked,
            sequence=index,
            candidate_key=candidate['candidate_key'],
            candidate_label=candidate['candidate_label'],
            round_number=candidate['round_number'],
            candidate_params=candidate['candidate_params'],
            display_metadata=candidate['display_metadata'],
            status='pending',
        ) for index, candidate in enumerate(candidates, 1)]
        SimulationRun.objects.bulk_create(rows)
        return rows


def append_candidate_runs(task, candidates, round_number=1, expected_started_at=None):
    """Append later-round candidate executions to a Task already being processed."""
    candidates = _normalize_candidates(candidates, round_number=round_number)
    with transaction.atomic():
        locked = SimcTask.objects.select_for_update().get(pk=task.pk)
        if expected_started_at is not None and (
            locked.current_status != 1 or locked.started_at != expected_started_at
        ):
            raise ValueError('属性寻优执行租约已失效')
        next_sequence = (SimulationRun.objects.filter(task=locked).order_by('-sequence')
                         .values_list('sequence', flat=True).first() or 0) + 1
        rows = []
        for offset, candidate in enumerate(candidates):
            rows.append(SimulationRun(
                task=locked, sequence=next_sequence + offset,
                candidate_key=candidate['candidate_key'],
                candidate_label=candidate['candidate_label'],
                round_number=candidate['round_number'],
                candidate_params=candidate['candidate_params'],
                display_metadata=candidate['display_metadata'], status='pending',
            ))
        SimulationRun.objects.bulk_create(rows)
        if locked.current_status in (2, 3):
            locked.current_status = 0
            locked.started_at = locked.completed_at = None
            locked.error_detail = None
            locked.save(update_fields=['current_status', 'started_at', 'completed_at', 'error_detail', 'modified_time'])
    return rows


def create_task_from_request(
    user_id: int,
    profile_fields: Dict[str, Any],
    base_template_id: int,
    selected_apl_id: int,
    simulation_params: Optional[Dict[str, Any]] = None,
    name: Optional[str] = None,
    mode: str = 'normal',
    mode_params: Optional[Dict[str, Any]] = None,
    candidates: Optional[list] = None,
    backend_id: Optional[int] = None,
    is_admin: bool = False,
) -> SimcTask:
    """
    Unified entry for homepage "auto-save/update player config and create Task" atomic operation.

    Given user_id and validated profile fields from API, this function:
    1. If simc_profile_id is provided: use the current user's active Profile or
       freeze an active upstream system default without mutating it
    2. If simc_profile_id is not provided: create a new SimcProfile
    3. Validate base_template_id and selected_apl_id resource ownership
    4. Create reference normal task with complete resource FKs

    Profile save and Task creation are in the same transaction; on failure, rollback.

    Args:
        user_id: User creating the task
        profile_fields: Dict containing:
            - simc_profile_id (optional): existing profile to update
            - name: profile name (required if creating new)
            - spec: spec key
            - player_config_mode: one of battlenet/manual_equipment/attribute_only
            - battlenet_region/realm/character: for battlenet mode
            - player_equipment: for manual_equipment/attribute_only
            - talent: talent build code
            - gear_strength/crit/haste/mastery/versatility: secondary stats
        base_template_id: SimcContentTemplate FK (required)
        selected_apl_id: SimcApl FK (required)
        simulation_params: Simulation options (fight_style, max_time, desired_targets)
        name: Optional task name override

    Returns:
        Created SimcTask with profile FK and version FKs set

    Raises:
        TaskCreationError: If validation fails or resources are missing
    """
    from django.db import transaction
    import uuid

    with transaction.atomic():
        # Step 1: Resolve or create Profile
        simc_profile_id = profile_fields.get('simc_profile_id')

        if simc_profile_id:
            # Update existing profile
            try:
                profile_query = SimcProfile.objects.select_for_update().filter(
                    id=simc_profile_id, is_active=True,
                )
                profile = profile_query.get()
            except SimcProfile.DoesNotExist:
                raise TaskCreationError(
                    f"Profile {simc_profile_id} does not exist or does not belong to user {user_id}"
                )

            # Upstream defaults are shared immutable snapshots. They may be used
            # as a task input, but never rewritten by the requesting user.
            is_system_default = (
                profile.user_id is None
                and profile.source == SimcProfile.SOURCE_SIMC_UPSTREAM
            )
            can_update_profile = is_admin or profile.user_id in (None, user_id)
            if not is_system_default and can_update_profile:
                # Only update name if explicitly provided in profile_fields
                if 'name' in profile_fields:
                    profile.name = profile_fields['name']
                profile.spec = profile_fields.get('spec', profile.spec)
                profile.player_config_mode = profile_fields.get('player_config_mode', profile.player_config_mode)
                if 'use_ptr' in profile_fields:
                    if type(profile_fields['use_ptr']) is not bool:
                        raise TaskCreationError('use_ptr must be a boolean')
                    profile.use_ptr = profile_fields['use_ptr']
                profile.battlenet_region = profile_fields.get('battlenet_region', profile.battlenet_region or '')
                profile.battlenet_realm = profile_fields.get('battlenet_realm', profile.battlenet_realm or '')
                profile.battlenet_character = profile_fields.get('battlenet_character', profile.battlenet_character or '')
                profile.player_equipment = profile_fields.get('player_equipment', profile.player_equipment or '')
                profile.talent = profile_fields.get('talent', profile.talent or '')
                override_fields = (
                    'gear_strength', 'gear_crit', 'gear_haste',
                    'gear_mastery', 'gear_versatility',
                )
                for field in override_fields:
                    if field in profile_fields:
                        setattr(profile, field, profile_fields[field])
                profile.save()
        else:
            # Create new profile
            profile_name = profile_fields.get('name', '').strip()
            if not profile_name:
                raise TaskCreationError("Profile name is required when creating new profile")

            profile_mode = profile_fields.get('player_config_mode', 'manual_equipment')
            override_values = {
                field: profile_fields.get(field)
                for field in (
                    'gear_strength', 'gear_crit', 'gear_haste',
                    'gear_mastery', 'gear_versatility',
                )
            }
            profile = SimcProfile.objects.create(
                user_id=user_id,
                name=profile_name,
                spec=profile_fields.get('spec', 'fury'),
                player_config_mode=profile_mode,
                use_ptr=profile_fields.get('use_ptr') is True,
                battlenet_region=profile_fields.get('battlenet_region', ''),
                battlenet_realm=profile_fields.get('battlenet_realm', ''),
                battlenet_character=profile_fields.get('battlenet_character', ''),
                player_equipment=profile_fields.get('player_equipment', ''),
                talent=profile_fields.get('talent', ''),
                **override_values,
                is_active=True,
            )

        # Step 2: Create task using unified create_task
        task_name = name or f"{profile.name} 常规模拟"

        # Normalize simulation_params
        normalized_simulation_params = simulation_params or {}
        if 'time' in normalized_simulation_params:
            normalized_simulation_params['max_time'] = normalized_simulation_params.pop('time')
        if 'target_count' in normalized_simulation_params:
            normalized_simulation_params['desired_targets'] = normalized_simulation_params.pop('target_count')

        task = create_task(
            user_id=user_id,
            name=task_name,
            profile_id=profile.id,
            template_id=base_template_id,
            apl_id=selected_apl_id,
            mode=mode,
            simulation_params=normalized_simulation_params,
            mode_params=mode_params,
            candidates=candidates,
            backend_id=backend_id,
            is_admin=is_admin,
        )

        return task


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _resource_token(user_id, backend, profile, apl, template, identity):
    """Bind all fields which can change executable content or selection policy."""
    return _compute_content_hash({
        'user_id': user_id,
        'backend': {
            'id': backend.pk, 'active': backend.is_active,
            'identifier': backend.identifier, 'platform': backend.platform,
            'path': backend.simc_path, 'version': backend.current_version,
        },
        'profile': {
            'id': profile.pk, 'owner': profile.user_id, 'active': profile.is_active,
            'payload': _build_profile_payload(profile),
        },
        'apl': {
            'id': apl.pk, 'owner': apl.owner_user_id, 'system': apl.is_system,
            'active': apl.is_active, 'selectable': apl.is_selectable,
            'payload': _build_apl_payload(apl),
            'validation_status': apl.validation_status,
            'validated_content_hash': apl.validated_content_hash,
            'validation_revision': apl.validation_revision,
            'validation_game_build': apl.validation_game_build,
        },
        'template': {
            'id': template.pk, 'owner': template.owner_user_id,
            'active': template.is_active, 'selectable': template.is_selectable,
            'payload': _build_template_payload(template),
        },
        'validation_identity': list(identity),
    })


def _check_resource_specs(profile, apl, template):
    from botend.services.simc_player_config import canonical_simc_profile_identity, canonical_simc_spec_identity
    profile_class, profile_spec = canonical_simc_profile_identity(profile.spec, profile.class_name)
    canonical_spec = f'{profile_class}_{profile_spec}' if profile_class and profile_spec else ''
    template_class, template_spec = canonical_simc_spec_identity(template.spec)
    generic = str(template.spec or '').strip().lower() in ('', 'default', 'all', '*')
    if canonical_spec and not generic and (
            template_spec != profile_spec
            or (profile_class and template_class and template_class != profile_class)):
        raise TaskCreationError('基础模板专精与玩家配置专精不一致')
    apl_class, apl_spec = canonical_simc_spec_identity(apl.spec)
    if canonical_spec and (
            apl_spec != profile_spec
            or (profile_class and apl_class and apl_class != profile_class)):
        raise TaskCreationError('APL 专精与玩家配置专精不一致')


def _load_resources(user_id, profile_id, template_id, apl_id, backend_id, *,
                    lock=False, is_admin=False):
    if not profile_id or not template_id or not apl_id:
        raise TaskCreationError(
            'Complete references required: profile_id, template_id, and apl_id must all be provided'
        )
    backend_qs = SimcBackendBinary.objects
    profile_qs = SimcProfile.objects
    apl_qs = SimcApl.objects
    template_qs = SimcContentTemplate.objects
    if lock:
        # Global task-persistence lock order. Do not use select_related here: on
        # PostgreSQL that can implicitly lock joined resources out of order.
        backend_qs = backend_qs.select_for_update()
        profile_qs = profile_qs.select_for_update()
        apl_qs = apl_qs.select_for_update()
        template_qs = template_qs.select_for_update()
    if backend_id:
        backend = backend_qs.filter(pk=backend_id, is_active=True).first()
        backend_error = 'Selected SimC backend does not exist or is disabled'
    else:
        backend = backend_qs.filter(identifier='production', is_active=True).first()
        backend_error = 'Default production SimC backend is unavailable'
    if backend is None:
        raise TaskCreationError(backend_error)
    try:
        profile = profile_qs.get(pk=profile_id)
    except SimcProfile.DoesNotExist:
        raise TaskCreationError(f'Profile {profile_id} does not exist')
    try:
        apl = apl_qs.get(pk=apl_id)
    except SimcApl.DoesNotExist:
        raise TaskCreationError(f'APL {apl_id} does not exist')
    try:
        template = template_qs.get(pk=template_id)
    except SimcContentTemplate.DoesNotExist:
        raise TaskCreationError(f'Template {template_id} does not exist')
    # Resource ownership controls which resources appear in configuration
    # lists. Executing a simulation is intentionally not an authorization
    # boundary; callers already provide explicit resource IDs and we only
    # enforce executable state and specialization compatibility here.
    _validate_executable_resource_state(profile, 'profile')
    _validate_executable_resource_state(apl, 'apl')
    _validate_executable_resource_state(template, 'template')
    _check_resource_specs(profile, apl, template)
    return backend, profile, apl, template


def _validate_executable_resource_state(resource, resource_type: str) -> None:
    """Validate state required to execute a selected resource.

    Ownership is deliberately excluded. Visibility/selection policy belongs
    to the resource-list APIs; task creation must also support administrators
    and explicit cross-owner simulation requests.
    """
    if resource_type == 'profile':
        if not resource.is_active:
            raise TaskCreationError(f"Profile {resource.id} is not active")
    elif resource_type == 'apl':
        if not resource.is_active:
            raise TaskCreationError(f"APL {resource.id} is not active")
        # Personal APLs are executable after the editor's structural check.
        # is_selectable remains a publication/listing flag for system resources.
        if resource.is_system and not resource.is_selectable:
            raise TaskCreationError(f"APL {resource.id} is not selectable")
    elif resource_type == 'template':
        if not resource.is_active:
            raise TaskCreationError(f"Template {resource.id} is not active")
        if not resource.is_selectable:
            raise TaskCreationError(f"Template {resource.id} is not selectable")
    else:
        raise TaskCreationError(f"Invalid resource type: {resource_type}")


def prepare_task_creation(user_id: int, profile_id: int, template_id: int,
                          apl_id: int, backend_id: Optional[int] = None,
                          is_admin: bool = False):
    """Prepare a structurally valid simulation without a publication gate.

    APL validation is performed when the editor saves the resource.  Running a
    simulation must be allowed to reach SimC so runtime errors and results are
    visible to the user instead of being hidden behind a separate publication
    workflow.
    """
    is_admin = bool(is_admin)
    backend, profile, apl, template = _load_resources(
        user_id, profile_id, template_id, apl_id, backend_id,
        lock=False, is_admin=is_admin,
    )
    identity = current_validation_identity(backend=backend)
    before = _resource_token(user_id, backend, profile, apl, template, identity)

    # Re-read after loading the resources. The definitive check occurs under
    # locks in create_task_from_prepared.
    current = _load_resources(
        user_id, profile_id, template_id, apl_id, backend.pk,
        lock=False, is_admin=is_admin,
    )
    final_identity = current_validation_identity(backend=current[0])
    if not final_identity:
        raise TaskPreparedResourceChanged(
            'Profile, APL, Template, or Backend changed during authoritative validation'
        )
    after = _resource_token(user_id, *current, final_identity)
    if before != after or final_identity != identity:
        raise TaskPreparedResourceChanged(
            'Profile, APL, Template, or Backend changed during authoritative validation'
        )
    backend, profile, apl, template = current
    return PreparedTaskCreation(
        user_id=user_id, backend_id=backend.pk, profile_id=profile.pk,
        apl_id=apl.pk, template_id=template.pk,
        profile_payload_json=_canonical_json(_build_profile_payload(profile)),
        apl_payload_json=_canonical_json(_build_apl_payload(apl)),
        template_payload_json=_canonical_json(_build_template_payload(template)),
        resource_token=after, validation_identity=tuple(identity),
        is_admin=is_admin, seal=_PREPARED_SEAL,
    )


def create_task_from_prepared(*, prepared, user_id: int, name: str,
                              profile_id: int, template_id: int, apl_id: int,
                              mode='normal', simulation_params=None, mode_params=None,
                              candidates=None, backend_id=None, is_admin=False):
    """Persist a preflighted Task in a short transaction, failing closed if stale."""
    if not isinstance(prepared, PreparedTaskCreation) or prepared.seal is not _PREPARED_SEAL:
        raise TaskCreationError('Invalid prepared task creation token')
    requested_ids = (user_id, backend_id or prepared.backend_id, profile_id, apl_id, template_id)
    prepared_ids = (prepared.user_id, prepared.backend_id, prepared.profile_id,
                    prepared.apl_id, prepared.template_id)
    if requested_ids != prepared_ids:
        raise TaskCreationError('Prepared task creation token does not match request')
    if bool(is_admin) != prepared.is_admin:
        raise TaskCreationError('Prepared task creation token does not match request')
    allowed_modes = {'normal', 'comparison', 'attribute_sweep'}
    if mode not in allowed_modes:
        raise TaskCreationError(f"Invalid mode '{mode}'. Allowed: {allowed_modes}")
    normalized_simulation_params = _normalize_params(simulation_params, SIMULATION_PARAMS_WHITELIST)
    options_error = validate_simulation_options(normalized_simulation_params or {})
    if options_error:
        raise TaskCreationError(options_error)
    normalized_mode_params = _normalize_params(mode_params, MODE_PARAMS_WHITELIST) or {}
    normalized_mode_params['initial_candidates'] = _normalize_candidates(candidates)

    with transaction.atomic():
        try:
            backend, profile, apl, template = _load_resources(
                user_id, profile_id, template_id, apl_id, prepared.backend_id,
                lock=True, is_admin=is_admin,
            )
        except TaskCreationError as exc:
            # The opaque token proves these resources were valid at preflight time.
            # A locked re-read failure is therefore concurrent drift, not bad input.
            raise TaskPreparedResourceChanged('Prepared task creation is stale') from exc
        identity = current_validation_identity(backend=backend)
        if not identity:
            raise TaskPreparedResourceChanged('Prepared task creation is stale')
        token = _resource_token(user_id, backend, profile, apl, template, identity)
        payloads = (
            _canonical_json(_build_profile_payload(profile)),
            _canonical_json(_build_apl_payload(apl)),
            _canonical_json(_build_template_payload(template)),
        )
        if (token != prepared.resource_token
                or tuple(identity) != prepared.validation_identity
                or payloads != (prepared.profile_payload_json, prepared.apl_payload_json,
                                prepared.template_payload_json)):
            raise TaskPreparedResourceChanged('Prepared task creation is stale')
        profile_payload, apl_payload, template_payload = map(json.loads, payloads)
        profile_version = _create_or_reuse_version('profile', profile.pk, profile_payload)
        apl_version = _create_or_reuse_version('apl', apl.pk, apl_payload)
        template_version = _create_or_reuse_version('template', template.pk, template_payload)
        import uuid
        return SimcTask.objects.create(
            user_id=user_id, name=name, simc_profile_id=profile.pk, task_type=1,
            profile=profile, template=template, apl=apl, backend=backend,
            profile_version=profile_version, template_version=template_version,
            apl_version=apl_version, mode=mode,
            simulation_params=deepcopy(normalized_simulation_params),
            mode_params=deepcopy(normalized_mode_params), candidate_label='',
            result_file=f'{uuid.uuid4().hex}.html', current_status=0, is_active=True,
        )


def create_task(user_id: int, name: str, profile_id: Optional[int] = None,
                template_id: Optional[int] = None, apl_id: Optional[int] = None,
                mode: str = 'normal', simulation_params=None, mode_params=None,
                candidates=None, backend_id: Optional[int] = None,
                prepared=None, is_admin: bool = False) -> SimcTask:
    """Compatibility entry point for structural resource validation."""
    if prepared is None:
        prepared = prepare_task_creation(
            user_id, profile_id, template_id, apl_id, backend_id=backend_id,
            is_admin=is_admin,
        )
    return create_task_from_prepared(
        prepared=prepared, user_id=user_id, name=name, profile_id=profile_id,
        template_id=template_id, apl_id=apl_id, backend_id=backend_id,
        mode=mode, simulation_params=simulation_params, mode_params=mode_params,
        candidates=candidates, is_admin=is_admin,
    )
