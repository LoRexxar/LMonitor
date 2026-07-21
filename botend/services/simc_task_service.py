"""
SimC Task Service - Create reference-based tasks with immutable version snapshots.

Responsibilities:
1. Validate ownership (user_id match or system resources)
2. Validate active & selectable status at creation time
3. Generate or reuse SimcResourceVersion for each resource
4. Normalize simulation_params & mode_params with whitelist
5. Create Task with live FK + version FK, no frozen content
"""
import hashlib
import json
from typing import Optional, Dict, Any
from django.db import transaction
from botend.models import (
    SimcTask,
    SimcProfile,
    SimcContentTemplate,
    SimcApl,
    SimcResourceVersion,
)
from botend.services.simc_apl.publish import (
    content_hash as apl_content_hash,
    current_validation_identity,
    validate_apl_for_profile,
)


class TaskCreationError(Exception):
    """Raised when task creation fails validation."""
    pass


# Whitelist for simulation_params
SIMULATION_PARAMS_WHITELIST = {
    'iterations',
    'target_error',
    'fight_style',
    'max_time',
    'vary_combat_length',
    'enemy_type',
    'desired_targets',
}

# Candidate differences for comparison / attribute-sweep tasks.  Values remain
# structured JSON, but unknown top-level keys are discarded at task creation.
MODE_PARAMS_WHITELIST = {
    'candidate_type',
    'is_base',
    'batch_index',
    'gear_swap',
    'talent_override',
    'talent_candidate',
    'apl_override',
    'attribute_ratings',
    'search',
}


def _compute_content_hash(payload: dict) -> str:
    """Compute SHA256 hash of payload for version deduplication."""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _validate_resource_ownership(
    resource,
    resource_type: str,
    user_id: int,
) -> None:
    """
    Validate resource ownership and active/selectable status.

    Rules:
    - Profile: must belong to user_id, is_active=True
    - Template/APL: allow system (owner_user_id=None) or user's own, is_active=True, is_selectable=True
    """
    if resource_type == 'profile':
        if not isinstance(resource, SimcProfile):
            raise TaskCreationError(f"Invalid profile resource")
        if resource.user_id != user_id:
            raise TaskCreationError(
                f"Profile {resource.id} belongs to user {resource.user_id}, not {user_id}"
            )
        if not resource.is_active:
            raise TaskCreationError(f"Profile {resource.id} is not active")

    elif resource_type == 'template':
        if not isinstance(resource, SimcContentTemplate):
            raise TaskCreationError(f"Invalid template resource")
        if resource.owner_user_id is not None and resource.owner_user_id != user_id:
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
        # Allow system APLs (is_system=True or owner_user_id=None) or user's own
        if not resource.is_system and resource.owner_user_id is not None and resource.owner_user_id != user_id:
            raise TaskCreationError(
                f"APL {resource.id} belongs to user {resource.owner_user_id}, not {user_id}"
            )
        if not resource.is_active:
            raise TaskCreationError(f"APL {resource.id} is not active")
        if not resource.is_selectable:
            raise TaskCreationError(f"APL {resource.id} is not selectable")


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
    # A manual/addon export is already a complete SimC player block. Its
    # legacy secondary-stat columns are UI metadata and may be zero when the
    # values were not entered separately. Passing those zeros to Composer
    # emits gear_*_rating=0 and deletes the ratings encoded by the items.
    manual_export = profile.player_config_mode in ('manual_equipment', 'addon_full_export') and bool(player_equipment)
    return {
        'name': profile.name,
        'spec': profile.spec,
        'player_config_mode': profile.player_config_mode,
        'battlenet_region': profile.battlenet_region,
        'battlenet_realm': profile.battlenet_realm,
        'battlenet_character': profile.battlenet_character,
        'player_equipment': player_equipment,
        'talent': profile.talent,
        # A complete exported/manual player block already carries the primary
        # stat through its equipment.  The legacy UI column must not become a
        # final ``gear_strength=0`` override either.
        'gear_strength': None if manual_export else profile.gear_strength,
        'gear_crit': None if manual_export else profile.gear_crit,
        'gear_haste': None if manual_export else profile.gear_haste,
        'gear_mastery': None if manual_export else profile.gear_mastery,
        'gear_versatility': None if manual_export else profile.gear_versatility,
    }


def _build_template_payload(template: SimcContentTemplate) -> dict:
    """Build immutable payload from SimcContentTemplate."""
    return {
        'name': template.name,
        'template_type': template.template_type,
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


def create_task_from_request(
    user_id: int,
    profile_fields: Dict[str, Any],
    base_template_id: int,
    selected_apl_id: int,
    simulation_params: Optional[Dict[str, Any]] = None,
    name: Optional[str] = None,
) -> SimcTask:
    """
    Unified entry for homepage "auto-save/update player config and create Task" atomic operation.

    Given user_id and validated profile fields from API, this function:
    1. If simc_profile_id is provided: only update the current user's active Profile
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
                profile = SimcProfile.objects.select_for_update().get(
                    id=simc_profile_id,
                    user_id=user_id,
                    is_active=True,
                )
            except SimcProfile.DoesNotExist:
                raise TaskCreationError(
                    f"Profile {simc_profile_id} does not exist or does not belong to user {user_id}"
                )

            # Update profile fields
            # Only update name if explicitly provided in profile_fields
            if 'name' in profile_fields:
                profile.name = profile_fields['name']
            profile.spec = profile_fields.get('spec', profile.spec)
            profile.player_config_mode = profile_fields.get('player_config_mode', profile.player_config_mode)
            profile.battlenet_region = profile_fields.get('battlenet_region', profile.battlenet_region or '')
            profile.battlenet_realm = profile_fields.get('battlenet_realm', profile.battlenet_realm or '')
            profile.battlenet_character = profile_fields.get('battlenet_character', profile.battlenet_character or '')
            profile.player_equipment = profile_fields.get('player_equipment', profile.player_equipment or '')
            profile.talent = profile_fields.get('talent', profile.talent or '')
            profile.gear_strength = profile_fields.get('gear_strength', profile.gear_strength)
            profile.gear_crit = profile_fields.get('gear_crit', profile.gear_crit)
            profile.gear_haste = profile_fields.get('gear_haste', profile.gear_haste)
            profile.gear_mastery = profile_fields.get('gear_mastery', profile.gear_mastery)
            profile.gear_versatility = profile_fields.get('gear_versatility', profile.gear_versatility)
            profile.save()
        else:
            # Create new profile
            profile_name = profile_fields.get('name', '').strip()
            if not profile_name:
                raise TaskCreationError("Profile name is required when creating new profile")

            profile = SimcProfile.objects.create(
                user_id=user_id,
                name=profile_name,
                spec=profile_fields.get('spec', 'fury'),
                player_config_mode=profile_fields.get('player_config_mode', 'manual_equipment'),
                battlenet_region=profile_fields.get('battlenet_region', ''),
                battlenet_realm=profile_fields.get('battlenet_realm', ''),
                battlenet_character=profile_fields.get('battlenet_character', ''),
                player_equipment=profile_fields.get('player_equipment', ''),
                talent=profile_fields.get('talent', ''),
                gear_strength=profile_fields.get('gear_strength', 0),
                gear_crit=profile_fields.get('gear_crit', 0),
                gear_haste=profile_fields.get('gear_haste', 0),
                gear_mastery=profile_fields.get('gear_mastery', 0),
                gear_versatility=profile_fields.get('gear_versatility', 0),
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
            mode='normal',
            simulation_params=normalized_simulation_params,
        )

        return task


@transaction.atomic
def create_task(
    user_id: int,
    name: str,
    profile_id: Optional[int] = None,
    template_id: Optional[int] = None,
    apl_id: Optional[int] = None,
    mode: str = 'normal',
    simulation_params: Optional[Dict[str, Any]] = None,
    mode_params: Optional[Dict[str, Any]] = None,
    candidate_label: str = '',
    batch_id: Optional[int] = None,
) -> SimcTask:
    """
    Create a reference-based SimC task with immutable version snapshots.

    Args:
        user_id: User creating the task
        name: Task name
        profile_id: SimcProfile FK
        template_id: SimcContentTemplate FK
        apl_id: SimcApl FK
        mode: Task mode (normal/comparison/attribute_sweep)
        simulation_params: Simulation options (will be normalized)
        mode_params: Mode-specific params (will be normalized)
        candidate_label: Label for comparison tasks
        batch_id: Optional batch FK

    Returns:
        Created SimcTask with version FKs set

    Raises:
        TaskCreationError: If validation fails
    """
    # Validate mode
    allowed_modes = {'normal', 'comparison', 'attribute_sweep'}
    if mode not in allowed_modes:
        raise TaskCreationError(f"Invalid mode '{mode}'. Allowed: {allowed_modes}")

    # Every executable mode is a complete reference task. Candidate-specific
    # differences live only in mode_params; resources and immutable versions
    # are still mandatory for comparison and attribute-sweep tasks.
    if not profile_id or not template_id or not apl_id:
        raise TaskCreationError(
            f"Mode '{mode}' requires complete references: profile_id, template_id, and apl_id must all be provided"
        )

    if mode != 'normal' and not batch_id:
        raise TaskCreationError(f"Mode '{mode}' requires batch_id")

    # Resolve and validate resources
    profile = None
    profile_version = None
    if profile_id:
        try:
            profile = SimcProfile.objects.select_for_update().get(pk=profile_id)
        except SimcProfile.DoesNotExist:
            raise TaskCreationError(f"Profile {profile_id} does not exist")

        _validate_resource_ownership(profile, 'profile', user_id)
        payload = _build_profile_payload(profile)
        profile_version = _create_or_reuse_version('profile', profile.id, payload)

    from botend.services.simc_player_config import canonical_simc_spec_identity
    profile_class, profile_spec = canonical_simc_spec_identity(profile.spec if profile else '')
    canonical_resource_spec = f'{profile_class}_{profile_spec}' if profile_class and profile_spec else ''

    template = None
    template_version = None
    if template_id:
        try:
            template = SimcContentTemplate.objects.get(pk=template_id)
        except SimcContentTemplate.DoesNotExist:
            raise TaskCreationError(f"Template {template_id} does not exist")

        _validate_resource_ownership(template, 'template', user_id)
        template_class, template_spec = canonical_simc_spec_identity(template.spec)
        template_is_generic = str(template.spec or '').strip().lower() in ('', 'default', 'all', '*')
        if canonical_resource_spec and not template_is_generic and (
            template_spec != profile_spec
            or (profile_class and template_class and template_class != profile_class)
        ):
            raise TaskCreationError('基础模板专精与玩家配置专精不一致')
        payload = _build_template_payload(template)
        template_version = _create_or_reuse_version('template', template.id, payload)

    apl = None
    apl_version = None
    if apl_id:
        try:
            apl = SimcApl.objects.select_for_update().get(pk=apl_id)
        except SimcApl.DoesNotExist:
            raise TaskCreationError(f"APL {apl_id} does not exist")

        _validate_resource_ownership(apl, 'apl', user_id)
        identity = current_validation_identity()
        stale_reason = apl.validation_staleness(identity)
        if stale_reason:
            raise TaskCreationError(f'APL validation is stale: {stale_reason}')
        # is_selectable and stored metadata are only a publication cache. Every
        # new Task gets a final authoritative check against the actual persisted
        # Profile; no client-provided validation result is accepted.
        validation = validate_apl_for_profile(profile, apl)
        final_identity = current_validation_identity()
        current_apl = SimcApl.objects.select_for_update().get(pk=apl.pk)
        current_profile = SimcProfile.objects.select_for_update().get(pk=profile.pk)
        if (_build_profile_payload(current_profile) != _build_profile_payload(profile)
                or current_apl.spec != apl.spec
                or current_apl.content != apl.content
                or final_identity != identity):
            raise TaskCreationError('Profile or APL changed during authoritative validation')
        profile = current_profile
        apl = current_apl
        if (not validation.get('valid')
                or validation.get('content_hash') != apl_content_hash(apl.content)
                or validation.get('revision') != identity[0]
                or validation.get('game_build') != identity[1]):
            raise TaskCreationError('APL failed authoritative validation for the selected Profile')
        apl_class, apl_spec = canonical_simc_spec_identity(apl.spec)
        if canonical_resource_spec and (
            apl_spec != profile_spec
            or (profile_class and apl_class and apl_class != profile_class)
        ):
            raise TaskCreationError('APL 专精与玩家配置专精不一致')
        payload = _build_apl_payload(apl)
        apl_version = _create_or_reuse_version('apl', apl.id, payload)

    # Normalize params
    normalized_simulation_params = _normalize_params(simulation_params, SIMULATION_PARAMS_WHITELIST)
    normalized_mode_params = _normalize_params(mode_params, MODE_PARAMS_WHITELIST)

    # Generate result_file name (UUID-based for reference tasks)
    import uuid
    result_file = f'{uuid.uuid4().hex}.html'

    # Create task with live FK + version FK
    task = SimcTask.objects.create(
        user_id=user_id,
        name=name,
        simc_profile_id=profile.id if profile else 0,  # Set to actual profile.id for reference tasks
        task_type=1,

        # Live resource FKs
        profile=profile,
        template=template,
        apl=apl,

        # Version FKs
        profile_version=profile_version,
        template_version=template_version,
        apl_version=apl_version,

        # Mode and params
        mode=mode,
        simulation_params=normalized_simulation_params,
        mode_params=normalized_mode_params,

        # Task metadata
        candidate_label=candidate_label,
        batch_id=batch_id,
        result_file=result_file,
        current_status=0,  # pending
        is_active=True,
    )

    return task
