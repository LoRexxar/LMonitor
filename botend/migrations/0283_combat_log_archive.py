import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0282_home_navigation_sections'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='CombatLogReport',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('filename', models.CharField(max_length=240)),
                ('size', models.PositiveBigIntegerField()),
                ('part_size', models.PositiveIntegerField()),
                ('state', models.CharField(default='uploading', max_length=16)),
                ('attempt', models.UUIDField(null=True)),
                ('message', models.CharField(blank=True, max_length=240)),
                ('digest', models.CharField(blank=True, max_length=64)),
                ('summary', models.JSONField(default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='CombatLogPart',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('number', models.PositiveIntegerField()),
                ('digest', models.CharField(max_length=64)),
                ('report', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='parts', to='botend.combatlogreport')),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('report', 'number'), name='combat_log_part_number')],
            },
        ),
    ]
