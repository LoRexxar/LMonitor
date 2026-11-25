import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0286_combat_mechanics_queries'),
    ]

    operations = [
        migrations.CreateModel(
            name='CombatLogMechanicReview',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('fingerprint', models.CharField(max_length=64)),
                ('findings', models.JSONField(default=list)),
                ('token', models.UUIDField(default=uuid.uuid4)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('mechanic', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='botend.combatlogmechanic')),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('mechanic', 'fingerprint'), name='combat_mechanic_review_key')],
            },
        ),
    ]
