import django.utils.timezone
from django.db import migrations, models


def backfill_display_updated_on(apps, schema_editor):
    default_route = apps.get_model('botend', 'MythicDungeonDefaultRoute')
    for route in default_route.objects.all().iterator():
        updated_at = route.updated_at
        if updated_at and django.utils.timezone.is_aware(updated_at):
            updated_at = django.utils.timezone.localtime(updated_at)
        route.display_updated_on = (
            updated_at.date()
            if updated_at
            else django.utils.timezone.localdate()
        )
        route.save(update_fields=['display_updated_on'])


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0185_mythic_default_route_applicable_level'),
    ]

    operations = [
        migrations.AddField(
            model_name='mythicdungeondefaultroute',
            name='display_updated_on',
            field=models.DateField(null=True),
        ),
        migrations.RunPython(
            backfill_display_updated_on,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name='mythicdungeondefaultroute',
            name='display_updated_on',
            field=models.DateField(default=django.utils.timezone.localdate),
        ),
    ]
