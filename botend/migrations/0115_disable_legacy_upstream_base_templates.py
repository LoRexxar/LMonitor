from django.db import migrations


def disable_legacy_upstream_base_templates(apps, schema_editor):
    SimcContentTemplate = apps.get_model('botend', 'SimcContentTemplate')
    SimcContentTemplate.objects.filter(
        template_type='base_template',
        source='simc_upstream',
    ).exclude(
        spec='default',
        name='基础模板 default',
    ).update(is_active=False, is_selectable=False)


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0114_enable_upstream_base_templates'),
    ]

    operations = [
        migrations.RunPython(
            disable_legacy_upstream_base_templates,
            migrations.RunPython.noop,
        ),
    ]
