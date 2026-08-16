from collections import defaultdict

from django.db import migrations


def backfill_benchmark_profile_talent_strings(apps, schema_editor):
    BenchmarkProfile = apps.get_model('botend', 'SimcBenchmarkProfile')
    TalentString = apps.get_model('botend', 'SimcTalentString')

    talent_ids_by_exact_value = defaultdict(list)
    for talent_string in TalentString.objects.all().only('id', 'spec', 'talent').iterator():
        talent_ids_by_exact_value[(talent_string.spec, talent_string.talent)].append(
            talent_string.id
        )

    rows = BenchmarkProfile.objects.filter(talent_string_id__isnull=True).select_related(
        'panel_spec', 'profile'
    )
    for row in rows.iterator():
        talent = row.profile.talent
        if not talent:
            continue
        matches = talent_ids_by_exact_value.get((row.panel_spec.spec_key, talent), ())
        if len(matches) == 1:
            BenchmarkProfile.objects.filter(
                pk=row.pk, talent_string_id__isnull=True
            ).update(talent_string_id=matches[0])


class Migration(migrations.Migration):
    dependencies = [
        ('botend', '0170_simctalentstring_hero_talent_names'),
    ]

    operations = [
        migrations.RunPython(
            backfill_benchmark_profile_talent_strings,
            migrations.RunPython.noop,
        ),
    ]
