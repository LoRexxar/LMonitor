import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def publish_existing(apps, schema):
    apps.get_model("botend","CombatLogReport").objects.using(schema.connection.alias).filter(
        state="ready").update(published=models.F("attempt"))


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0283_combat_log_archive'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='combatlogreport',
            name='published',
            field=models.UUIDField(null=True),
        ),
        migrations.AddField(
            model_name='combatlogreport',
            name='storage_tier',
            field=models.CharField(default='hot', max_length=8),
        ),
        migrations.CreateModel(
            name='CombatLogCollection',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=100)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
                ('readers', models.ManyToManyField(blank=True, related_name='combat_log_collections', to=settings.AUTH_USER_MODEL)),
                ('reports', models.ManyToManyField(blank=True, related_name='collections', to='botend.combatlogreport')),
            ],
        ),
        migrations.RunPython(publish_existing,migrations.RunPython.noop),
    ]
