from django.db import migrations, models


def backfill_applicable_level(apps, schema_editor):
    default_route = apps.get_model('botend', 'MythicDungeonDefaultRoute')
    for route in default_route.objects.filter(applicable_level='').iterator():
        route.applicable_level = f'{route.dungeon_level} 层'
        route.save(update_fields=['applicable_level'])


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0184_mythic_dungeon_default_routes'),
    ]

    operations = [
        migrations.AddField(
            model_name='mythicdungeondefaultroute',
            name='applicable_level',
            field=models.CharField(blank=True, default='', max_length=80),
        ),
        migrations.RunPython(
            backfill_applicable_level,
            migrations.RunPython.noop,
        ),
    ]
