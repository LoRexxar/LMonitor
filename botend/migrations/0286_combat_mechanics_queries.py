import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0285_combat_review_snapshot'),
    ]

    operations = [
        migrations.CreateModel(
            name='CombatLogBookmark',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100)),
                ('filters', models.JSONField()),
                ('collection', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='bookmarks', to='botend.combatlogcollection')),
            ],
        ),
        migrations.CreateModel(
            name='CombatLogMechanic',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100)),
                ('cohort', models.CharField(max_length=240)),
                ('kind', models.CharField(max_length=12)),
                ('trigger', models.CharField(max_length=24)),
                ('response', models.CharField(max_length=24)),
                ('window', models.PositiveSmallIntegerField()),
                ('collection', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='mechanics', to='botend.combatlogcollection')),
            ],
        ),
    ]
