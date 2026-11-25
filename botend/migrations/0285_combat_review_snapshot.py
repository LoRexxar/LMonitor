import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0284_combat_log_workbench'),
    ]

    operations = [
        migrations.CreateModel(
            name='CombatLogReview',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('version', models.UUIDField()),
                ('fight', models.PositiveIntegerField()),
                ('topic', models.CharField(max_length=12)),
                ('anchor', models.CharField(max_length=40)),
                ('findings', models.JSONField(default=list)),
                ('token', models.UUIDField(default=uuid.uuid4)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('report', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='botend.combatlogreport')),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('report', 'version', 'fight', 'topic', 'anchor'), name='combat_review_snapshot')],
            },
        ),
    ]
