"""Create SimC reruns by copying the frozen Task request only."""
import copy
import uuid
from typing import Optional

from django.db import transaction

from botend.models import SimcTask



class TaskRerunError(Exception):
    """Raised when task rerun fails."""


@transaction.atomic
def create_rerun(
    source_task_id: int,
    user_id: int,
    overrides: Optional[dict] = None,
) -> SimcTask:
    """Copy a Task request without reading or creating any SimulationRun."""
    overrides = overrides or {}
    unknown = set(overrides)
    if unknown:
        raise TaskRerunError(
            f"Rerun is a frozen Task copy; unsupported overrides: {', '.join(sorted(unknown))}"
        )

    try:
        source = SimcTask.objects.select_for_update().get(pk=source_task_id)
    except SimcTask.DoesNotExist as exc:
        raise TaskRerunError(f"Source task {source_task_id} does not exist") from exc

    if source.user_id != user_id:
        raise TaskRerunError(
            f"Cannot rerun task {source_task_id} belonging to user {source.user_id}"
        )
    if not (
        source.profile_id and source.template_id and source.apl_id
        and source.talent_string_id
        and source.profile_version_id and source.template_version_id and source.apl_version_id
        and source.talent_version_id
    ):
        raise TaskRerunError(
            f"Source task {source_task_id} lacks complete references. "
            "New reruns require profile/template/apl/talent + version FKs; "
            "historical tasks without a frozen talent string remain read-only."
        )

    mode_params = copy.deepcopy(source.mode_params) or {}
    if source.mode != 'normal' and not mode_params.get('initial_candidates'):
        raise TaskRerunError("Source task does not have a complete frozen request")

    return SimcTask.objects.create(
        user_id=user_id,
        name=f"{source.name} (rerun)",
        simc_profile_id=source.simc_profile_id,
        result_file=f"{uuid.uuid4().hex}.html",
        task_type=source.task_type,
        candidate_label=source.candidate_label,
        profile_id=source.profile_id,
        template_id=source.template_id,
        apl_id=source.apl_id,
        talent_string_id=source.talent_string_id,
        profile_version_id=source.profile_version_id,
        template_version_id=source.template_version_id,
        apl_version_id=source.apl_version_id,
        talent_version_id=source.talent_version_id,
        mode=source.mode,
        simulation_params=copy.deepcopy(source.simulation_params) or {},
        mode_params=mode_params,
        source_task=source,
        backend_id=source.backend_id,
        current_status=0,
        analysis_result={},
        is_active=True,
    )
