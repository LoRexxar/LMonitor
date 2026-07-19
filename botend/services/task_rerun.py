"""
Task Rerun Service - Create rerun tasks with version management.

For reference-based tasks:
- Default: copies version FKs (same immutable snapshots)
- Override: generates new version for overridden resource

All tasks must have complete references (profile/template/apl + versions).
"""
from typing import Optional, Dict, Any
import uuid

from django.db import transaction
from botend.models import SimcTask
from botend.services.task_resolver import is_reference_task
from botend.services.simc_task_service import (
    create_task,
    TaskCreationError,
    _build_profile_payload,
    _build_template_payload,
    _build_apl_payload,
    _create_or_reuse_version,
)


class TaskRerunError(Exception):
    """Raised when task rerun fails."""
    pass


@transaction.atomic
def create_rerun(
    source_task_id: int,
    user_id: int,
    overrides: Optional[dict] = None,
) -> SimcTask:
    """
    Create a rerun task from an existing task.

    All tasks must have complete references (profile/template/apl + versions).
    - Copies version FKs by default (same immutable snapshots)
    - If overrides contain profile_id/template_id/apl_id, generates new version for that resource
    - Sets source_task_id to track rerun chain

    Args:
        source_task_id: ID of task to rerun
        user_id: User creating the rerun
        overrides: Optional dict to override specific fields (e.g., {"apl_id": 99})

    Returns:
        New SimcTask instance

    Raises:
        TaskRerunError: If source task doesn't exist, validation fails, or source lacks references
    """
    overrides = overrides or {}
    allowed_override_keys = {'name', 'simulation_params', 'mode_params', 'profile_id', 'template_id', 'apl_id'}
    unknown = set(overrides) - allowed_override_keys
    if unknown:
        raise TaskRerunError(f"Unsupported rerun overrides: {', '.join(sorted(unknown))}")
    for resource_key in ('profile_id', 'template_id', 'apl_id'):
        if resource_key in overrides and overrides[resource_key] in (None, 0, ''):
            raise TaskRerunError(f"Rerun override {resource_key} must be a non-zero resource ID")

    try:
        source = SimcTask.objects.get(pk=source_task_id)
    except SimcTask.DoesNotExist:
        raise TaskRerunError(f"Source task {source_task_id} does not exist")

    # Validate user has permission to rerun
    if source.user_id != user_id:
        raise TaskRerunError(
            f"Cannot rerun task {source_task_id} belonging to user {source.user_id}"
        )

    # Keep the terminal-state invariant in the service so non-HTTP callers
    # cannot bypass the API contract.
    if source.current_status not in (2, 3):
        raise TaskRerunError(
            f"Only completed or failed tasks can be rerun (source status: {source.current_status})"
        )

    # Validate source has complete references
    if not (source.profile_id and source.template_id and source.apl_id and
            source.profile_version_id and source.template_version_id and source.apl_version_id):
        raise TaskRerunError(
            f"Source task {source_task_id} lacks complete references. "
            f"All tasks must have profile/template/apl + version FKs."
        )

    # Create reference-based rerun
    return _create_reference_rerun(source, user_id, overrides)


def _create_reference_rerun(
    source: SimcTask,
    user_id: int,
    overrides: dict,
) -> SimcTask:
    """Create rerun for reference-based task with version management."""
    from botend.models import SimcProfile, SimcContentTemplate, SimcApl
    from botend.services.simc_task_service import (
        _validate_resource_ownership,
        _normalize_params,
        SIMULATION_PARAMS_WHITELIST,
        MODE_PARAMS_WHITELIST,
    )

    # Determine which resources to override
    profile_id = overrides.get('profile_id', source.profile_id)
    template_id = overrides.get('template_id', source.template_id)
    apl_id = overrides.get('apl_id', source.apl_id)

    # Check if any resource was overridden (comparing to source)
    profile_overridden = 'profile_id' in overrides
    template_overridden = 'template_id' in overrides
    apl_overridden = 'apl_id' in overrides

    # For overridden resources, validate ownership/active/selectable and generate new versions
    # For unchanged resources, reuse existing version FKs
    profile_version_id = source.profile_version_id
    if profile_overridden and profile_id:
        try:
            profile = SimcProfile.objects.get(pk=profile_id)
            _validate_resource_ownership(profile, 'profile', user_id)
            payload = _build_profile_payload(profile)
            profile_version = _create_or_reuse_version('profile', profile.id, payload)
            profile_version_id = profile_version.id
        except SimcProfile.DoesNotExist:
            raise TaskRerunError(f"Override profile {profile_id} does not exist")
        except TaskCreationError as e:
            raise TaskRerunError(str(e))

    template_version_id = source.template_version_id
    if template_overridden and template_id:
        try:
            template = SimcContentTemplate.objects.get(pk=template_id)
            _validate_resource_ownership(template, 'template', user_id)
            payload = _build_template_payload(template)
            template_version = _create_or_reuse_version('template', template.id, payload)
            template_version_id = template_version.id
        except SimcContentTemplate.DoesNotExist:
            raise TaskRerunError(f"Override template {template_id} does not exist")
        except TaskCreationError as e:
            raise TaskRerunError(str(e))

    apl_version_id = source.apl_version_id
    if apl_overridden and apl_id:
        try:
            apl = SimcApl.objects.get(pk=apl_id)
            _validate_resource_ownership(apl, 'apl', user_id)
            payload = _build_apl_payload(apl)
            apl_version = _create_or_reuse_version('apl', apl.id, payload)
            apl_version_id = apl_version.id
        except SimcApl.DoesNotExist:
            raise TaskRerunError(f"Override apl {apl_id} does not exist")
        except TaskCreationError as e:
            raise TaskRerunError(str(e))

    # Normalize overridden params with same whitelist as create_task
    simulation_params = overrides.get('simulation_params', source.simulation_params)
    mode_params = overrides.get('mode_params', source.mode_params)

    normalized_simulation_params = _normalize_params(simulation_params, SIMULATION_PARAMS_WHITELIST)
    normalized_mode_params = _normalize_params(mode_params, MODE_PARAMS_WHITELIST)

    # Ordinary reruns are independent normal tasks. Batch orchestration may
    # explicitly attach a rerun to a newly-created/current batch; merely
    # inheriting from a historical candidate never does so.
    rerun_mode = 'normal'
    rerun_batch_id = None
    rerun = SimcTask.objects.create(
        user_id=user_id,
        name=overrides.get('name', f"{source.name} (rerun)"),
        # Keep the legacy profile pointer aligned whenever the live Profile is
        # explicitly replaced. Some old readers still consume this integer.
        simc_profile_id=profile_id if profile_overridden else source.simc_profile_id,
        task_type=source.task_type,

        # Live resource FKs
        profile_id=profile_id,
        template_id=template_id,
        apl_id=apl_id,

        # Version FKs (reuse or regenerate)
        profile_version_id=profile_version_id,
        template_version_id=template_version_id,
        apl_version_id=apl_version_id,

        # Batch membership is opt-in through explicit orchestration overrides.
        mode=rerun_mode,
        simulation_params=normalized_simulation_params,
        mode_params=normalized_mode_params,

        # Task metadata
        batch_id=rerun_batch_id,
        candidate_label=source.candidate_label,
        result_file=f'{uuid.uuid4().hex}.html',
        current_status=0,
        is_active=True,
        source_task_id=source.id,
    )

    return rerun
