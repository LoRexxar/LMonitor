from django.db import migrations


SECONDARY_DEFAULTS = {
    'crit_per_percent': 46,
    'haste_per_percent': 44,
    'mastery_per_percent': 46,
    'versatility_per_percent': 54,
}

MASTERY_DEFAULTS = {
    'fury': 1.40,
    'devourer': 1.00,
}


def add_missing_rules(apps, schema_editor):
    SecondaryRule = apps.get_model('botend', 'SimcSecondaryStatRule')
    MasteryCoefficient = apps.get_model('botend', 'SimcMasteryCoefficient')

    SecondaryRule.objects.get_or_create(
        class_name='warrior',
        defaults=SECONDARY_DEFAULTS,
    )
    for spec, coefficient in MASTERY_DEFAULTS.items():
        MasteryCoefficient.objects.get_or_create(
            spec=spec,
            defaults={'mastery_coefficient': coefficient},
        )


class Migration(migrations.Migration):
    dependencies = [
        ('botend', '0123_collapse_simc_batches_into_tasks'),
    ]

    operations = [
        migrations.RunPython(add_missing_rules, migrations.RunPython.noop),
    ]