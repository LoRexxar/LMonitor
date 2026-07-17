"""
Task Resolver Service - Resolve task references to version payloads for execution.

Key changes from previous version:
1. Reads version.payload instead of live resource content
2. Validates version.resource_id matches task.profile_id/template_id/apl_id
3. Allows soft-deleted live resources (is_active=False) if version exists
4. Returns resolved content strings for Worker to compose
"""
from dataclasses import dataclass
from typing import Optional, Dict, Any
from botend.models import SimcTask, SimcResourceVersion


class TaskResolutionError(Exception):
    """Raised when task resolution fails validation."""
    pass


@dataclass
class ResolvedTaskContext:
    """
    Resolved task context - version payloads with content strings.
    Lives only during Worker execution, not persisted.
    """
    profile_content: Optional[str]
    template_content: Optional[str]
    apl_content: Optional[str]
    simulation_params: Dict[str, Any]
    mode_params: Dict[str, Any]
    resource_metadata: Dict[str, Any]
    profile_payload: Dict[str, Any]
    template_payload: Dict[str, Any]
    apl_payload: Dict[str, Any]


def resolve_task(task: SimcTask) -> ResolvedTaskContext:
    """
    Resolve task to version payloads for Worker execution.

    Args:
        task: SimcTask instance with version FKs

    Returns:
        ResolvedTaskContext with content strings from version payloads

    Raises:
        TaskResolutionError: If version is missing or inconsistent
    """
    profile_content = None
    template_content = None
    apl_content = None
    resource_metadata = {}
    profile_payload = {}
    template_payload = {}
    apl_payload = {}

    # Resolve profile version
    if task.profile_version_id:
        try:
            version = SimcResourceVersion.objects.get(pk=task.profile_version_id)
        except SimcResourceVersion.DoesNotExist:
            raise TaskResolutionError(
                f"Profile version {task.profile_version_id} does not exist"
            )

        # Validate version consistency
        if version.resource_type != 'profile':
            raise TaskResolutionError(
                f"Version {version.id} type is {version.resource_type}, expected profile"
            )
        if task.profile_id and version.resource_id != task.profile_id:
            raise TaskResolutionError(
                f"Profile version resource_id {version.resource_id} does not match task.profile_id {task.profile_id}"
            )

        # Extract content from payload
        payload = version.payload or {}
        profile_payload = dict(payload)
        if payload.get('player_config_mode') == 'manual_equipment':
            profile_content = payload.get('player_equipment', '')
        elif payload.get('player_config_mode') == 'battlenet':
            # Build battlenet reference string
            profile_content = f"# Profile: {payload.get('name')}\n# Spec: {payload.get('spec')}\n"
            profile_content += f"# BNet: {payload.get('battlenet_region')}/{payload.get('battlenet_realm')}/{payload.get('battlenet_character')}"

        resource_metadata['profile'] = {
            'version_id': version.id,
            'resource_id': version.resource_id,
            'name': payload.get('name'),
            'spec': payload.get('spec'),
        }

    # Resolve template version
    if task.template_version_id:
        try:
            version = SimcResourceVersion.objects.get(pk=task.template_version_id)
        except SimcResourceVersion.DoesNotExist:
            raise TaskResolutionError(
                f"Template version {task.template_version_id} does not exist"
            )

        if version.resource_type != 'template':
            raise TaskResolutionError(
                f"Version {version.id} type is {version.resource_type}, expected template"
            )
        if task.template_id and version.resource_id != task.template_id:
            raise TaskResolutionError(
                f"Template version resource_id {version.resource_id} does not match task.template_id {task.template_id}"
            )

        payload = version.payload or {}
        template_payload = dict(payload)
        template_content = payload.get('content', '')

        resource_metadata['template'] = {
            'version_id': version.id,
            'resource_id': version.resource_id,
            'name': payload.get('name'),
            'template_type': payload.get('template_type'),
        }

    # Resolve APL version
    if task.apl_version_id:
        try:
            version = SimcResourceVersion.objects.get(pk=task.apl_version_id)
        except SimcResourceVersion.DoesNotExist:
            raise TaskResolutionError(
                f"APL version {task.apl_version_id} does not exist"
            )

        if version.resource_type != 'apl':
            raise TaskResolutionError(
                f"Version {version.id} type is {version.resource_type}, expected apl"
            )
        if task.apl_id and version.resource_id != task.apl_id:
            raise TaskResolutionError(
                f"APL version resource_id {version.resource_id} does not match task.apl_id {task.apl_id}"
            )

        payload = version.payload or {}
        apl_payload = dict(payload)
        apl_content = payload.get('content', '')

        resource_metadata['apl'] = {
            'version_id': version.id,
            'resource_id': version.resource_id,
            'name': payload.get('name'),
            'spec': payload.get('spec'),
            'is_system': payload.get('is_system'),
        }

    return ResolvedTaskContext(
        profile_content=profile_content,
        template_content=template_content,
        apl_content=apl_content,
        simulation_params=task.simulation_params or {},
        mode_params=task.mode_params or {},
        resource_metadata=resource_metadata,
        profile_payload=profile_payload,
        template_payload=template_payload,
        apl_payload=apl_payload,
    )


def is_reference_task(task: SimcTask) -> bool:
    """
    Check if a task is a complete reference-based task.

    Reference tasks must have ALL six FK fields set:
    - profile_id, template_id, apl_id (live resource FKs)
    - profile_version_id, template_version_id, apl_version_id (version FKs)

    Args:
        task: SimcTask instance

    Returns:
        True if task has complete references, False otherwise
    """
    return (
        task.profile_id is not None
        and task.template_id is not None
        and task.apl_id is not None
        and task.profile_version_id is not None
        and task.template_version_id is not None
        and task.apl_version_id is not None
    )
