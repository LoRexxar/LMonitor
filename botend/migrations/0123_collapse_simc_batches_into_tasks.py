import json

from django.db import migrations, models


STATUS_MAP = {
    0: 'pending',
    1: 'running',
    2: 'completed',
    3: 'failed',
    4: 'failed',
}


def _json_summary(value):
    """Keep legacy text losslessly while using SimulationRun's JSON column."""
    if value in (None, ''):
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {'legacy_value': value}


def _iso(value):
    return value.isoformat() if value is not None else None


def _round_number(mode_params):
    params = mode_params if isinstance(mode_params, dict) else {}
    for key in ('round_number', 'round_no', 'round'):
        value = params.get(key)
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return value
    return 1


def _candidate_key(task, run=None):
    if run is not None and run.candidate_label:
        return run.candidate_label[:200]
    if task.candidate_label:
        return task.candidate_label[:200]
    params = task.mode_params if isinstance(task.mode_params, dict) else {}
    for key in ('candidate_key', 'key', 'candidate'):
        value = params.get(key)
        if value not in (None, '') and not isinstance(value, (dict, list)):
            return str(value)[:200]
    return f'legacy-task-{task.pk}'


def _candidate_params(task):
    # These are the old member Task fields which described one candidate.  Keep
    # the complete JSON parameter snapshots, not merely a reconstructed subset.
    return {
        'legacy_task_id': task.pk,
        'mode': task.mode,
        'task_type': task.task_type,
        'candidate_label': task.candidate_label,
        'simulation_params': task.simulation_params,
        'mode_params': task.mode_params,
        'profile_id': task.profile_id,
        'profile_version_id': task.profile_version_id,
        'template_id': task.template_id,
        'template_version_id': task.template_version_id,
        'apl_id': task.apl_id,
        'apl_version_id': task.apl_version_id,
        'legacy_result_file': task.result_file,
    }


def collapse_batches(apps, schema_editor):
    SimcTaskBatch = apps.get_model('botend', 'SimcTaskBatch')
    SimcTask = apps.get_model('botend', 'SimcTask')
    SimulationRun = apps.get_model('botend', 'SimulationRun')
    SimcTaskArtifact = apps.get_model('botend', 'SimcTaskArtifact')
    alias = schema_editor.connection.alias

    batches = SimcTaskBatch.objects.using(alias).order_by('id')
    initial_run_count = SimulationRun.objects.using(alias).count()
    initial_artifact_count = SimcTaskArtifact.objects.using(alias).count()

    # A run-scoped artifact has two ownership pointers.  A mismatch can only be
    # repaired safely when both old tasks are members of the same batch.
    mismatches = (
        SimcTaskArtifact.objects.using(alias)
        .exclude(run_id=None)
        .exclude(task_id=models.F('run__task_id'))
        .select_related('task', 'run__task')
    )
    for artifact in mismatches.iterator():
        artifact_batch = artifact.task.batch_id
        run_batch = artifact.run.task.batch_id
        if artifact_batch is None or artifact_batch != run_batch:
            raise RuntimeError(
                'Unsafe SimC artifact mapping: artifact %s points to task %s '
                'but run %s belongs to task %s'
                % (artifact.pk, artifact.task_id, artifact.run_id, artifact.run.task_id)
            )

    for batch in batches.iterator():
        members = list(
            SimcTask.objects.using(alias).filter(batch_id=batch.pk).order_by('id')
        )
        if not members:
            raise RuntimeError(
                f'Cannot preserve SimcTaskBatch {batch.pk}: it has no member task'
            )
        if any(member.user_id != batch.user_id for member in members):
            raise RuntimeError(
                f'Cannot safely map SimcTaskBatch {batch.pk}: member user differs'
            )

        base_members = [
            member for member in members
            if isinstance(member.mode_params, dict)
            and member.mode_params.get('is_base') is True
        ]
        representative = min(base_members or members, key=lambda member: member.pk)
        member_ids = [member.pk for member in members]

        batch_metadata = {
            'id': batch.pk,
            'user_id': batch.user_id,
            'name': batch.name,
            'batch_type': batch.batch_type,
            # request_manifest intentionally remains the original TextField
            # value.  It is not parsed/reformatted by this migration.
            'request_manifest': batch.request_manifest,
            'status': batch.status,
            'error_detail': batch.error_detail,
            'completed_at': _iso(batch.completed_at),
            'created_at': _iso(batch.created_at),
            'updated_at': _iso(batch.updated_at),
            'is_active': batch.is_active,
        }
        member_results = [
            {
                'task_id': member.pk,
                'result_file': member.result_file,
                'result_summary': member.result_summary,
                'error_detail': member.error_detail,
                'current_status': member.current_status,
                'started_at': _iso(member.started_at),
                'completed_at': _iso(member.completed_at),
            }
            for member in members
        ]
        representative.analysis_result = {
            'legacy_batch': batch_metadata,
            'legacy_member_results': member_results,
        }
        representative.save(update_fields=['analysis_result'])

        # Assign unique temporary sequences before changing task_id.  This
        # avoids collisions with the existing (task, sequence) constraint even
        # when several member tasks all used sequence=1.
        runs = list(
            SimulationRun.objects.using(alias)
            .filter(task_id__in=member_ids)
            .select_related('task')
            .order_by('task_id', 'sequence', 'created_at', 'id')
        )
        minimum_sequence = min((run.sequence for run in runs), default=0)
        temporary_start = min(0, minimum_sequence) - len(runs) - len(members) - 1
        for position, run in enumerate(runs, start=1):
            SimulationRun.objects.using(alias).filter(pk=run.pk).update(
                sequence=temporary_start - position,
                candidate_key=_candidate_key(run.task, run),
                round_number=_round_number(run.task.mode_params),
                candidate_params=_candidate_params(run.task),
            )
        if runs:
            SimulationRun.objects.using(alias).filter(
                pk__in=[run.pk for run in runs]
            ).update(task_id=representative.pk)

        # A member with no old run still represented a historical candidate.
        # Materialize one run carrying its status/result rather than dropping
        # that candidate when its task row is removed.
        member_ids_with_runs = {run.task_id for run in runs}
        for member in members:
            if member.pk in member_ids_with_runs:
                continue
            synthetic = SimulationRun.objects.using(alias).create(
                task_id=representative.pk,
                sequence=temporary_start - len(runs) - member_ids.index(member.pk) - 1,
                candidate_key=_candidate_key(member),
                candidate_label=member.candidate_label,
                round_number=_round_number(member.mode_params),
                candidate_params=_candidate_params(member),
                status=STATUS_MAP.get(member.current_status, 'failed'),
                result_summary=_json_summary(member.result_summary),
                error_detail=member.error_detail,
                started_at=member.started_at,
                completed_at=member.completed_at,
            )
            runs.append(synthetic)

        # Both task-level artifacts (run=NULL) and run-scoped artifacts move to
        # the request task.  The run pointer itself remains unchanged.
        SimcTaskArtifact.objects.using(alias).filter(
            task_id__in=member_ids
        ).update(task_id=representative.pk)

        # Final stable, collision-free sequence order.  Synthetic runs sort
        # after real historical runs because they were appended above.
        for position, run in enumerate(runs, start=1):
            SimulationRun.objects.using(alias).filter(pk=run.pk).update(
                sequence=position
            )

        # Preserve self-references which pointed at a member that is about to
        # disappear.  Mapping to the representative is unambiguous here.
        SimcTask.objects.using(alias).filter(
            source_task_id__in=member_ids
        ).exclude(pk__in=member_ids).update(source_task_id=representative.pk)
        SimcTask.objects.using(alias).filter(
            pk__in=member_ids, source_task_id__in=member_ids
        ).update(source_task_id=None)

        # The surviving row is the request itself, not the former baseline
        # candidate. Candidate-specific values have already been frozen on
        # each Run above; promote Batch lifecycle and identity to Task level.
        try:
            request_manifest = json.loads(batch.request_manifest) if batch.request_manifest else {}
        except (TypeError, ValueError):
            request_manifest = {'legacy_raw': batch.request_manifest}
        SimcTask.objects.using(alias).filter(pk=representative.pk).update(
            name=batch.name,
            mode=batch.batch_type,
            mode_params={
                'request_type': batch.batch_type,
                'request_manifest': request_manifest,
                'legacy_batch_id': batch.pk,
            },
            candidate_label='',
            current_status=batch.status,
            error_detail=batch.error_detail,
            completed_at=batch.completed_at,
            is_active=batch.is_active,
        )

        SimcTask.objects.using(alias).filter(
            pk__in=member_ids
        ).exclude(pk=representative.pk).delete()

    final_run_count = SimulationRun.objects.using(alias).count()
    final_artifact_count = SimcTaskArtifact.objects.using(alias).count()
    if final_run_count < initial_run_count:
        raise RuntimeError(
            f'SimC run count decreased: {initial_run_count} -> {final_run_count}'
        )
    if final_artifact_count != initial_artifact_count:
        raise RuntimeError(
            'SimC artifact count changed: %s -> %s'
            % (initial_artifact_count, final_artifact_count)
        )
    inconsistent = (
        SimcTaskArtifact.objects.using(alias)
        .exclude(run_id=None)
        .exclude(task_id=models.F('run__task_id'))
        .exists()
    )
    if inconsistent:
        raise RuntimeError('SimC artifact.task does not match artifact.run.task')


def reverse_schema_only(apps, schema_editor):
    # Deliberately do not attempt to reconstruct deleted member tasks/batches.
    # Django can reverse the surrounding schema operations for migration tests,
    # but historical data folding itself is explicitly irreversible.
    pass


class Migration(migrations.Migration):
    dependencies = [('botend', '0122_simcaplsymbol_trait_id')]

    operations = [
        migrations.AddField(
            model_name='simctask',
            name='analysis_result',
            field=models.JSONField(blank=True, default=dict, help_text='请求级分析结果'),
        ),
        migrations.AddField(
            model_name='simulationrun',
            name='candidate_key',
            field=models.CharField(blank=True, default='', help_text='候选稳定标识', max_length=200),
        ),
        migrations.AddField(
            model_name='simulationrun',
            name='round_number',
            field=models.PositiveIntegerField(default=1, help_text='候选轮次'),
        ),
        migrations.AddField(
            model_name='simulationrun',
            name='candidate_params',
            field=models.JSONField(blank=True, default=dict, help_text='候选参数快照'),
        ),
        migrations.RunPython(collapse_batches, reverse_schema_only),
        migrations.RemoveIndex(
            model_name='simctask',
            name='simc_task_batch_i_58f508_idx',
        ),
        migrations.RemoveField(model_name='simctask', name='batch'),
        migrations.DeleteModel(name='SimcTaskBatch'),
    ]
