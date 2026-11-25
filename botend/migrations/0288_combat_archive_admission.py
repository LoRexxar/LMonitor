from django.db import migrations, models


def seed_scheduler(apps, schema):
    apps.get_model("botend","CombatLogScheduler").objects.using(schema.connection.alias).get_or_create(pk=1)


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0287_combat_mechanic_evidence'),
    ]

    operations = [
        migrations.CreateModel(
            name='CombatLogScheduler',
            fields=[
                ('id', models.PositiveSmallIntegerField(primary_key=True, serialize=False)),
            ],
        ),
        migrations.AddField(
            model_name='combatlogreport',
            name='recoveries',
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='combatlogreport',
            name='reserved_bytes',
            field=models.PositiveBigIntegerField(null=True),
        ),
        migrations.RunPython(seed_scheduler,migrations.RunPython.noop),
    ]
