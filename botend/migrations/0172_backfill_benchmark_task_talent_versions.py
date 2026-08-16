import hashlib
import json
from collections import defaultdict

from django.db import migrations


def _talent_payload(talent_string):
    return {
        'name': talent_string.name,
        'spec': talent_string.spec,
        'talent': talent_string.talent,
        'hero_talent_names': list(talent_string.hero_talent_names or []),
    }


def _content_hash(payload):
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def backfill_benchmark_task_talent_versions(apps, schema_editor):
    BenchmarkCase = apps.get_model('botend', 'SimcBenchmarkCase')
    ResourceVersion = apps.get_model('botend', 'SimcResourceVersion')
    SimcTask = apps.get_model('botend', 'SimcTask')
    TalentString = apps.get_model('botend', 'SimcTalentString')

    talents_by_exact_value = defaultdict(list)
    talent_rows = TalentString.objects.all().only(
        'id', 'name', 'spec', 'talent', 'hero_talent_names',
    )
    for talent_string in talent_rows.iterator():
        talents_by_exact_value[(talent_string.spec, talent_string.talent)].append(
            talent_string
        )

    version_ids_by_talent = {}
    cases = BenchmarkCase.objects.filter(
        status='success',
        task__current_status=2,
        task__talent_string_id__isnull=True,
        task__talent_version_id__isnull=True,
        task__profile_version_id__isnull=False,
    ).select_related('task__profile_version')
    for case in cases.iterator(chunk_size=500):
        profile_payload = case.task.profile_version.payload
        if not isinstance(profile_payload, dict):
            continue
        frozen_talent = str(profile_payload.get('talent') or '')
        if not frozen_talent:
            continue
        matches = talents_by_exact_value.get((case.spec_key, frozen_talent), ())
        if len(matches) != 1 or not matches[0].hero_talent_names:
            continue

        talent_string = matches[0]
        version_id = version_ids_by_talent.get(talent_string.id)
        if version_id is None:
            payload = _talent_payload(talent_string)
            version, _ = ResourceVersion.objects.get_or_create(
                resource_type='talent',
                resource_id=talent_string.id,
                content_hash=_content_hash(payload),
                defaults={'payload': payload},
            )
            version_id = version.id
            version_ids_by_talent[talent_string.id] = version_id
        SimcTask.objects.filter(
            pk=case.task_id,
            talent_string_id__isnull=True,
            talent_version_id__isnull=True,
        ).update(
            talent_string_id=talent_string.id,
            talent_version_id=version_id,
        )


class Migration(migrations.Migration):
    dependencies = [
        ('botend', '0171_backfill_simc_benchmark_profile_talent_strings'),
    ]

    operations = [
        migrations.RunPython(
            backfill_benchmark_task_talent_versions,
            migrations.RunPython.noop,
        ),
    ]
