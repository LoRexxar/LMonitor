import re

from django.db import migrations


EQUIPMENT_SLOTS = {
    'head', 'neck', 'shoulder', 'shoulders', 'back', 'chest', 'shirt', 'tabard',
    'wrist', 'wrists', 'hands', 'waist', 'legs', 'feet', 'finger1', 'finger2',
    'trinket1', 'trinket2', 'main_hand', 'off_hand',
}


def normalize_current_upstream_profiles(apps, schema_editor):
    SimcProfile = apps.get_model('botend', 'SimcProfile')
    profiles = SimcProfile.objects.filter(
        source='simc_upstream',
        system_key__startswith='simc_upstream:',
    )
    for profile in profiles.iterator():
        changed = False
        normalized = []
        for line in (profile.player_equipment or '').splitlines():
            key, sep, raw_value = line.partition('=')
            normalized_key = key.strip().lower()
            if sep and normalized_key == 'ptr':
                changed = True
                continue
            if sep and normalized_key in EQUIPMENT_SLOTS:
                match = re.fullmatch(r'\s*(\d+)\s*(,.*)?', raw_value)
                if match:
                    suffix = match.group(2) or ''
                    line = f'{key.strip()}=,id={match.group(1)}{suffix}'
                    changed = True
            normalized.append(line)
        if changed:
            profile.player_equipment = '\n'.join(normalized)
            profile.save(update_fields=['player_equipment'])


class Migration(migrations.Migration):
    dependencies = [
        ('botend', '0149_allow_local_benchmark_candidate_icons'),
    ]

    operations = [
        migrations.RunPython(normalize_current_upstream_profiles, migrations.RunPython.noop),
    ]
