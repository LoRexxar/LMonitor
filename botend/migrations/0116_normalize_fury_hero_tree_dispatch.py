from django.db import migrations


LEGACY_FURY_DISPATCH = {
    'talent.slayers_dominance': 'hero_tree.slayer',
    'talent.lightning_strikes': 'hero_tree.mountain_thane',
}


def normalize_fury_hero_tree_dispatch(apps, schema_editor):
    SimcApl = apps.get_model('botend', 'SimcApl')
    apls = SimcApl.objects.filter(
        source='simc_upstream',
        spec='warrior_fury',
        is_system=True,
    )
    for apl in apls.iterator():
        content = apl.content or ''
        normalized = content
        for legacy, replacement in LEGACY_FURY_DISPATCH.items():
            normalized = normalized.replace(legacy, replacement)
        if normalized != content:
            apl.content = normalized
            apl.save(update_fields=['content'])


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0115_disable_legacy_upstream_base_templates'),
    ]

    operations = [
        migrations.RunPython(
            normalize_fury_hero_tree_dispatch,
            migrations.RunPython.noop,
        ),
    ]
