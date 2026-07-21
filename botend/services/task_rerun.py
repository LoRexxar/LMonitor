"""Create rerun tasks through the reference-task creation contract."""
from typing import Optional

from django.db import transaction

from botend.models import SimcTask
from botend.services.simc_task_service import create_task, TaskCreationError


class TaskRerunError(Exception):
    """Raised when task rerun fails."""


@transaction.atomic
def create_rerun(
    source_task_id: int,
    user_id: int,
    overrides: Optional[dict] = None,
) -> SimcTask:
    """Create a rerun after revalidating the current persisted resources."""
    overrides = overrides or {}
    allowed_override_keys = {
        'name', 'simulation_params', 'mode_params',
        'profile_id', 'template_id', 'apl_id',
    }
    unknown = set(overrides) - allowed_override_keys
    if unknown:
        raise TaskRerunError(f"Unsupported rerun overrides: {', '.join(sorted(unknown))}")
    for resource_key in ('profile_id', 'template_id', 'apl_id'):
        if resource_key in overrides and overrides[resource_key] in (None, 0, ''):
            raise TaskRerunError(f"Rerun override {resource_key} must be a non-zero resource ID")

    try:
        source = SimcTask.objects.select_for_update().get(pk=source_task_id)
    except SimcTask.DoesNotExist as exc:
        raise TaskRerunError(f"Source task {source_task_id} does not exist") from exc

    if source.user_id != user_id:
        raise TaskRerunError(
            f"Cannot rerun task {source_task_id} belonging to user {source.user_id}"
        )
    if source.current_status not in (2, 3):
        raise TaskRerunError(
            f"Only completed or failed tasks can be rerun (source status: {source.current_status})"
        )
    if not (
        source.profile_id and source.template_id and source.apl_id
        and source.profile_version_id and source.template_version_id and source.apl_version_id
    ):
        raise TaskRerunError(
            f"Source task {source_task_id} lacks complete references. "
            "All tasks must have profile/template/apl + version FKs."
        )

    try:
        rerun = create_task(
            user_id=user_id,
            name=overrides.get('name', f"{source.name} (rerun)"),
            profile_id=overrides.get('profile_id', source.profile_id),
            template_id=overrides.get('template_id', source.template_id),
            apl_id=overrides.get('apl_id', source.apl_id),
            mode='normal',
            simulation_params=overrides.get('simulation_params', source.simulation_params),
            mode_params=overrides.get('mode_params', source.mode_params),
            candidate_label=source.candidate_label,
        )
    except TaskCreationError as exc:
        raise TaskRerunError(str(exc)) from exc

    rerun.source_task_id = source.id
    rerun.save(update_fields=['source_task_id'])
    return rerun
