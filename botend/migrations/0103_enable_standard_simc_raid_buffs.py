from django.db import migrations


def enable_standard_raid_buffs(apps, schema_editor):
    SimcContentTemplate = apps.get_model('botend', 'SimcContentTemplate')
    templates = SimcContentTemplate.objects.filter(template_type='base_template')
    for template in templates.iterator():
        lines = template.content.splitlines()
        changed = False
        for index, line in enumerate(lines):
            if line.strip() == 'optimal_raid=0':
                lines[index] = 'optimal_raid=1'
                changed = True
        if changed:
            template.content = '\n'.join(lines)
            template.save(update_fields=['content', 'updated_at'])


def disable_standard_raid_buffs(apps, schema_editor):
    SimcContentTemplate = apps.get_model('botend', 'SimcContentTemplate')
    templates = SimcContentTemplate.objects.filter(template_type='base_template')
    for template in templates.iterator():
        lines = template.content.splitlines()
        changed = False
        for index, line in enumerate(lines):
            if line.strip() == 'optimal_raid=1':
                lines[index] = 'optimal_raid=0'
                changed = True
        if changed:
            template.content = '\n'.join(lines)
            template.save(update_fields=['content', 'updated_at'])


class Migration(migrations.Migration):
    dependencies = [
        ('botend', '0102_simc_profile_player_config'),
    ]

    operations = [
        migrations.RunPython(enable_standard_raid_buffs, disable_standard_raid_buffs),
    ]
