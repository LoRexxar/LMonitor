from django.db import migrations


ATTRIBUTE_FIELDS = (
    'gear_strength',
    'gear_crit',
    'gear_haste',
    'gear_mastery',
    'gear_versatility',
)


def clear_implicit_attribute_overrides(apps, schema_editor):
    SimcProfile = apps.get_model('botend', 'SimcProfile')

    # Before this release, task freezing deliberately ignored every gear_*
    # value on manual/addon player blocks. Those rows therefore have no valid
    # authored execution override to preserve, while their fixed model defaults
    # are exactly what leaked into the edit form. Restore inheritance for that
    # whole legacy mode. Attribute-only and Battle.net rows retain their values.
    SimcProfile.objects.filter(
        player_config_mode__in=('manual_equipment', 'addon_full_export'),
    ).update(**{field: None for field in ATTRIBUTE_FIELDS})


class Migration(migrations.Migration):
    dependencies = [
        ('botend', '0147_simc_profile_ptr_execution'),
    ]

    operations = [
        migrations.RunPython(
            clear_implicit_attribute_overrides,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
